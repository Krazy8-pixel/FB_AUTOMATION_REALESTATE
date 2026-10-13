import json
from automation.task_control import cancellable
import os
import pickle
import re
import threading
import time
from datetime import datetime

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from automation.chromedriver import build_chrome_driver
from automation.fb_login import _is_home_screen_visible
from automation.runtime_paths import session_health_path, sessions_dir


SESSION_DIR = sessions_dir()
HEALTHY = "Valid"
EXPIRED = "Expired"
UNKNOWN = "Check failed"

_health_lock = threading.Lock()
_check_job_lock = threading.Lock()
_check_job = None


def _safe_session_filename(email):
    return re.sub(r"[\\/:*?\"<>|]", "_", str(email or "").strip()) + ".pkl"


def _now_label():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_health_unlocked():
    path = session_health_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    changed = False
    for account, item in list(payload.items()):
        if not isinstance(item, dict):
            payload.pop(account, None)
            changed = True
            continue
        if item.get("status") == "Healthy":
            item["status"] = HEALTHY
            changed = True
    if changed:
        _write_health_unlocked(payload)
    return payload


def _write_health_unlocked(payload):
    path = session_health_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_session_health():
    with _health_lock:
        return _read_health_unlocked()


def set_session_health(account, status):
    account = str(account or "").strip()
    if not account:
        return
    status = status if status in {HEALTHY, EXPIRED, UNKNOWN} else UNKNOWN
    with _health_lock:
        payload = _read_health_unlocked()
        payload[account] = {
            "status": status,
            "checked_at": _now_label(),
        }
        _write_health_unlocked(payload)


def remove_session_health(account):
    account = str(account or "").strip()
    if not account:
        return
    with _health_lock:
        payload = _read_health_unlocked()
        payload.pop(account, None)
        _write_health_unlocked(payload)


def ensure_default_healthy(accounts):
    changed = False
    with _health_lock:
        payload = _read_health_unlocked()
        for account in accounts:
            account = str(account or "").strip()
            if account and account not in payload:
                payload[account] = {
                    "status": HEALTHY,
                    "checked_at": _now_label(),
                }
                changed = True
        if changed:
            _write_health_unlocked(payload)
        return payload


def _session_file_for_account(account):
    direct = os.path.join(SESSION_DIR, _safe_session_filename(account))
    if os.path.exists(direct):
        return direct
    raw = os.path.join(SESSION_DIR, f"{account}.pkl")
    if os.path.exists(raw):
        return raw
    return direct


def _safe_get(driver, url):
    try:
        driver.get(url)
        return True
    except TimeoutException:
        # Facebook can keep background requests open on slow networks. Stop
        # loading and inspect whatever rendered instead of failing the check.
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        return False


@cancellable
def check_account_login_cookie(account, cancel_event=None, driver_holder=None):
    cancel_event = cancel_event or threading.Event()
    driver = None
    try:
        if cancel_event.is_set():
            return None

        session_file = _session_file_for_account(account)
        if not os.path.exists(session_file):
            return EXPIRED

        with open(session_file, "rb") as f:
            cookies = pickle.load(f)
        if not any(cookie.get("name") == "c_user" for cookie in cookies if isinstance(cookie, dict)):
            return EXPIRED

        options = Options()
        options.add_argument("--window-size=1280,960")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-notifications")
        options.add_argument("--log-level=3")
        options.add_argument("--disable-blink-features=AutomationControlled")
        # Cookie checks run invisibly, independent of the Background toggle.
        options.add_argument("--headless=new")
        options.page_load_strategy = "eager"
        options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        driver = build_chrome_driver(options)
        driver.set_page_load_timeout(100)
        driver.set_script_timeout(20)
        if driver_holder is not None:
            driver_holder["driver"] = driver

        if cancel_event.is_set():
            return None

        _safe_get(driver, "https://www.facebook.com/")
        time.sleep(1.0)

        for cookie in cookies:
            if cancel_event.is_set():
                return None
            if not isinstance(cookie, dict):
                continue
            cookie = dict(cookie)
            if "sameSite" not in cookie:
                cookie["sameSite"] = "Lax"
            cookie["domain"] = ".facebook.com"
            try:
                driver.add_cookie(cookie)
            except Exception:
                continue

        if cancel_event.is_set():
            return None

        _safe_get(driver, "https://www.facebook.com/")
        try:
            WebDriverWait(driver, 15, poll_frequency=0.5).until(_is_home_screen_visible)
            return HEALTHY
        except TimeoutException:
            return EXPIRED
    except Exception as exc:
        if cancel_event.is_set():
            return None
        if driver_holder is not None:
            driver_holder["error"] = str(exc)
        print(f"[SessionCheck] Failed for {account}: {exc}", flush=True)
        return UNKNOWN
    finally:
        if driver_holder is not None:
            driver_holder["driver"] = None
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


class SessionCheckJob:
    def __init__(self, accounts):
        self.accounts = list(accounts)
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.driver_holder = {"driver": None}
        self.running = False
        self.cancelled = False
        self.current_account = None
        self.message = ""
        self.last_error = ""
        self.completed = 0
        self.total = len(self.accounts)
        self.started_at = _now_label()
        self.finished_at = None
        self.results = {}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        with self.lock:
            self.running = True
        self.thread.start()

    def cancel(self):
        self.cancel_event.set()
        with self.lock:
            self.cancelled = True
            self.running = False
        driver = self.driver_holder.get("driver")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    def _run(self):
        try:
            for account in self.accounts:
                if self.cancel_event.is_set():
                    break
                with self.lock:
                    self.current_account = account
                    self.message = f"Launching Chrome for {account}"
                    self.last_error = ""
                status = check_account_login_cookie(
                    account,
                    cancel_event=self.cancel_event,
                    driver_holder=self.driver_holder,
                )
                if status is None:
                    break
                set_session_health(account, status)
                with self.lock:
                    self.results[account] = status
                    self.completed += 1
                    if self.driver_holder.get("error"):
                        self.last_error = f"{account}: {self.driver_holder.get('error')}"
                        self.driver_holder["error"] = ""
                    self.message = f"{account}: {status}"
            with self.lock:
                self.cancelled = self.cancel_event.is_set()
                self.running = False
                self.current_account = None
                self.finished_at = _now_label()
        finally:
            with _check_job_lock:
                global _check_job
                if _check_job is self:
                    pass

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "cancelled": self.cancelled,
                "current_account": self.current_account,
                "message": self.message,
                "last_error": self.last_error,
                "completed": self.completed,
                "total": self.total,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "results": dict(self.results),
            }


def start_session_check(accounts):
    global _check_job
    accounts = [str(account or "").strip() for account in accounts]
    accounts = [account for account in accounts if account]
    with _check_job_lock:
        if _check_job and _check_job.snapshot().get("running"):
            return False, _check_job
        _check_job = SessionCheckJob(accounts)
        _check_job.start()
        return True, _check_job


def cancel_session_check():
    with _check_job_lock:
        job = _check_job
    if job and job.snapshot().get("running"):
        job.cancel()
        return True
    return False


def get_session_check_snapshot():
    with _check_job_lock:
        job = _check_job
    if not job:
        return {
            "running": False,
            "cancelled": False,
            "current_account": None,
            "completed": 0,
            "total": 0,
            "started_at": None,
            "finished_at": None,
            "results": {},
        }
    return job.snapshot()
