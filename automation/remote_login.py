import io
import os
import pickle
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from automation.chromedriver import build_chrome_driver
from automation.fb_login import (
    HOME_SCREEN_XPATHS,
    LOGIN_FIELD_LOCATORS,
    MANUAL_VERIFICATION_WAIT_SECONDS,
    MAX_ACCOUNTS,
    PASSWORD_FIELD_LOCATORS,
    _first_visible_element,
    _is_home_screen_visible,
    _safe_session_filename,
    _type_text,
)
from automation.runtime_paths import sessions_dir
from automation.session_health import HEALTHY, set_session_health

SESSION_DIR = sessions_dir()
PHONE_LOGIN_TIMEOUT_SECONDS = MANUAL_VERIFICATION_WAIT_SECONDS
SCREENSHOT_MAX_WIDTH = 960


def _build_login_driver():
    options = Options()
    options.add_argument("--window-size=1280,960")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/116.0.5845.111 Safari/537.36"
    )
    return build_chrome_driver(options)


def _resolve_key(name: str):
    key_map = {
        "ENTER": Keys.ENTER,
        "TAB": Keys.TAB,
        "BACKSPACE": Keys.BACKSPACE,
        "ESCAPE": Keys.ESCAPE,
        "SPACE": Keys.SPACE,
    }
    return key_map.get(str(name or "").strip().upper())


@dataclass
class RemoteLoginSession:
    email: str
    password: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "starting"
    message: str = "Launching Chrome..."
    account_name: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    driver: object = None
    error: Optional[str] = None
    screenshot_revision: int = 0
    driver_lock: threading.RLock = field(default_factory=threading.RLock)
    worker: Optional[threading.Thread] = None

    def start(self):
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _set_status(self, status: str, message: str, account_name: Optional[str] = None):
        self.status = status
        self.message = message
        if account_name:
            self.account_name = account_name
        if status in {"success", "failed", "expired", "cancelled"}:
            self.completed_at = time.time()

    def _run(self):
        existing_sessions = [f for f in os.listdir(SESSION_DIR) if f.endswith(".pkl")]
        if len(existing_sessions) >= MAX_ACCOUNTS:
            self._set_status("failed", f"Maximum {MAX_ACCOUNTS} accounts already added.")
            return

        driver = None
        try:
            driver = _build_login_driver()
            with self.driver_lock:
                self.driver = driver
                driver.get("https://www.facebook.com/")
                if driver.current_url.startswith("chrome-error://") or "ERR_" in (driver.title or ""):
                    self._set_status("failed", f"Network/SSL error while opening Facebook: {driver.title}")
                    return

                wait = WebDriverWait(driver, 8)
                email_input = _first_visible_element(wait, LOGIN_FIELD_LOCATORS)
                password_input = _first_visible_element(wait, PASSWORD_FIELD_LOCATORS)
                if not email_input or not password_input:
                    self._set_status(
                        "failed",
                        "Facebook login fields not found. Check that facebook.com opens normally in Chrome.",
                    )
                    return

                _type_text(email_input, self.email)
                _type_text(password_input, self.password)
                password_input.send_keys(Keys.ENTER)

            self._set_status(
                "awaiting_verification",
                "Complete Facebook verification on this phone. The PC browser session is mirrored here.",
            )

            deadline = time.time() + PHONE_LOGIN_TIMEOUT_SECONDS
            while time.time() < deadline:
                with self.driver_lock:
                    if not self.driver:
                        self._set_status("cancelled", "Login session was closed.")
                        return
                    if _is_home_screen_visible(self.driver):
                        cookies = self.driver.get_cookies()
                        if not cookies:
                            self._set_status("failed", "Login finished but no session cookies were captured.")
                            return
                        filename = os.path.join(SESSION_DIR, _safe_session_filename(self.email))
                        with open(filename, "wb") as f:
                            pickle.dump(cookies, f)
                        set_session_health(self.email, HEALTHY)
                        account_name = (self.driver.title.split("|")[0].strip() if self.driver.title else "Facebook Account")
                        self._set_status("success", f"Login successful: {account_name}", account_name=account_name)
                        return
                time.sleep(2)

            self._set_status(
                "failed",
                "Facebook home screen was not visible before timeout. Verification did not complete in time.",
            )
        except Exception as exc:
            self.error = str(exc)
            self._set_status("failed", f"Login failed: {exc}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            with self.driver_lock:
                self.driver = None

    def is_active(self):
        return self.status in {"starting", "awaiting_verification"}

    def capture_frame(self):
        with self.driver_lock:
            if not self.driver:
                return None
            png = self.driver.get_screenshot_as_png()
        image = Image.open(io.BytesIO(png))
        if image.width > SCREENSHOT_MAX_WIDTH:
            height = max(1, int(image.height * (SCREENSHOT_MAX_WIDTH / float(image.width))))
            image = image.resize((SCREENSHOT_MAX_WIDTH, height))
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        self.screenshot_revision += 1
        return out.getvalue(), "image/png"

    def _viewport_size(self):
        with self.driver_lock:
            if not self.driver:
                return None
            return self.driver.execute_script(
                "return {w: Math.max(window.innerWidth || 0, 1), h: Math.max(window.innerHeight || 0, 1)};"
            )

    def click(self, rel_x: float, rel_y: float):
        rel_x = min(max(float(rel_x), 0.0), 1.0)
        rel_y = min(max(float(rel_y), 0.0), 1.0)
        with self.driver_lock:
            if not self.driver:
                raise RuntimeError("Login browser is not available.")
            viewport = self._viewport_size()
            x = max(1, int(viewport["w"] * rel_x))
            y = max(1, int(viewport["h"] * rel_y))
            clicked = False
            try:
                self.driver.execute_cdp_cmd(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchStart",
                        "touchPoints": [{"x": x, "y": y, "radiusX": 1, "radiusY": 1, "force": 1}],
                    },
                )
                self.driver.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
                )
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                )
                self.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                )
                clicked = True
            except Exception:
                clicked = False

            if clicked:
                return

            fallback_ok = self.driver.execute_script(
                """
                const x = arguments[0];
                const y = arguments[1];
                const el = document.elementFromPoint(x, y);
                if (!el) { return false; }
                if (typeof el.focus === 'function') { el.focus(); }
                if (typeof el.click === 'function') {
                  el.click();
                  return true;
                }
                ['pointerdown','mousedown','mouseup','click'].forEach(type => {
                  el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y, view:window}));
                });
                return true;
                """,
                x,
                y,
            )
            if not fallback_ok:
                raise RuntimeError("Nothing clickable was found at that point.")

    def scroll(self, amount: int):
        with self.driver_lock:
            if not self.driver:
                raise RuntimeError("Login browser is not available.")
            self.driver.execute_script("window.scrollBy(0, arguments[0]);", int(amount))

    def send_text(self, text: str):
        value = str(text or "")
        if not value:
            return
        with self.driver_lock:
            if not self.driver:
                raise RuntimeError("Login browser is not available.")
            active = self.driver.switch_to.active_element
            active.send_keys(value)

    def send_key(self, key_name: str):
        resolved = _resolve_key(key_name)
        if not resolved:
            raise RuntimeError("Unsupported key command.")
        with self.driver_lock:
            if not self.driver:
                raise RuntimeError("Login browser is not available.")
            active = self.driver.switch_to.active_element
            active.send_keys(resolved)

    def cancel(self):
        self._set_status("cancelled", "Phone verification session cancelled.")
        with self.driver_lock:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None


remote_login_sessions = {}
remote_login_sessions_lock = threading.Lock()


def create_remote_login_session(email: str, password: str):
    session = RemoteLoginSession(email=str(email or "").strip(), password=str(password or ""))
    with remote_login_sessions_lock:
        remote_login_sessions[session.session_id] = session
    session.start()
    return session


def get_remote_login_session(session_id: str):
    with remote_login_sessions_lock:
        session = remote_login_sessions.get(str(session_id or "").strip())
    return session


def cleanup_finished_remote_login_sessions(max_age_seconds: int = 900):
    now = time.time()
    stale_ids = []
    with remote_login_sessions_lock:
        for session_id, session in remote_login_sessions.items():
            done_at = session.completed_at or session.created_at
            if now - done_at > max_age_seconds and not session.is_active():
                stale_ids.append(session_id)
        for session_id in stale_ids:
            remote_login_sessions.pop(session_id, None)
