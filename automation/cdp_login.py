import json
import os
import pickle
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4
from pathlib import Path

from selenium.webdriver.chrome.options import Options

from automation.browser_settings import is_run_in_background_enabled
from automation.chromedriver import build_chrome_driver
from automation.fb_login import _is_home_screen_visible, _safe_session_filename
from automation.runtime_paths import data_dir, sessions_dir


_config_lock = threading.RLock()
_account_locks = {}
_watchers = {}
LOGIN_WAIT_SECONDS = 40 * 60


def _config_path():
    return Path(data_dir()) / "cdp_accounts.json"


def _profiles_root():
    path = Path(data_dir()) / "cdp_profiles"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_slug(value):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return slug[:80] or "facebook_account"


def _load_config():
    try:
        payload = json.loads(_config_path().read_text(encoding="utf-8"))
        accounts = payload.get("accounts") if isinstance(payload, dict) else None
        return accounts if isinstance(accounts, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _save_config(accounts):
    path = _config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"accounts": accounts}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def list_cdp_accounts():
    with _config_lock:
        return {name: dict(meta) for name, meta in _load_config().items()}


def get_cdp_account(account):
    account = str(account or "").strip()
    with _config_lock:
        accounts = _load_config()
        meta = dict(accounts.get(account) or {})
        session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
        # Recover from an older watcher/UI race where cookies were written but
        # the JSON status remained at "waiting".
        if (
            meta.get("status") == "waiting"
            and meta.get("cookies_updated_at")
            and os.path.isfile(session_path)
        ):
            meta.update(status="ready", message="Facebook login saved. Posting browser is ready.")
            accounts[account] = meta
            _save_config(accounts)
        return meta or None


def _update_account(account, **updates):
    with _config_lock:
        accounts = _load_config()
        meta = dict(accounts.get(account) or {})
        meta.update(updates)
        accounts[account] = meta
        _save_config(accounts)
        return dict(meta)


def _delete_profile_folder(profile_dir):
    if not profile_dir:
        return False
    root = _profiles_root().resolve()
    candidate = Path(profile_dir).resolve()
    try:
        if os.path.commonpath([str(root), str(candidate)]) != str(root) or candidate == root:
            return False
    except ValueError:
        return False
    if not candidate.exists():
        return True
    for _ in range(8):
        try:
            shutil.rmtree(candidate)
            return True
        except OSError:
            time.sleep(0.35)
    return not candidate.exists()


def _find_chrome():
    candidates = [
        os.getenv("CHROME_PATH", "").strip(),
        os.path.join(os.getenv("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.getenv("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError("Google Chrome was not found. Install Chrome or set CHROME_PATH.")


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def _next_port(accounts):
    used = {int(meta.get("port")) for meta in accounts.values() if str(meta.get("port", "")).isdigit()}
    for port in range(9222, 9322):
        if port not in used and not _port_open(port):
            return port
    raise RuntimeError("No free CDP port is available between 9222 and 9321.")


def _launch_profile(meta, *, background=False):
    port = int(meta["port"])
    if _port_open(port):
        return None
    profile_dir = str(Path(meta["profile_dir"]).resolve())
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    chrome_args = [
            _find_chrome(),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.facebook.com/",
        ]
    if background:
        chrome_args.insert(1, "--headless=new")
    proc = subprocess.Popen(
        chrome_args,
        creationflags=flags,
    )
    return proc


def _attach(port):
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{int(port)}")
    return build_chrome_driver(options)


def _detach(driver):
    try:
        driver.service.stop()
    except Exception:
        pass


def _close_dedicated_browser(driver):
    """Close only the dedicated CDP Chrome, then stop its driver service."""
    try:
        driver.execute_cdp_cmd("Browser.close", {})
    except Exception:
        pass
    finally:
        _detach(driver)


def _facebook_login_confirmed(driver):
    """Confirm the authenticated Facebook home location and session cookie."""
    current_url = str(driver.current_url or "").lower()
    if "facebook.com" not in current_url:
        return False
    if any(marker in current_url for marker in ("/login", "checkpoint", "two_step_verification")):
        return False

    # Keep the original home-screen locator check as a positive signal. Some
    # current Facebook layouts land on Marketplace, redirects, or localized
    # home URLs, so a valid c_user cookie on a non-auth Facebook URL is the
    # final authoritative confirmation.
    home_location = current_url.rstrip("/").endswith("facebook.com") or "/home.php" in current_url
    cookies = driver.get_cookies()
    has_authenticated_cookie = any(
        c.get("name") == "c_user" and str(c.get("value") or "").strip()
        for c in cookies
        if isinstance(c, dict)
    )
    return bool(has_authenticated_cookie and (home_location or _is_home_screen_visible(driver) or current_url))


def _capture_when_logged_in(account):
    deadline = time.time() + LOGIN_WAIT_SECONDS
    driver = None
    try:
        while time.time() < deadline:
            meta = get_cdp_account(account)
            if not meta:
                return
            try:
                if driver is None:
                    driver = _attach(meta["port"])
                if _facebook_login_confirmed(driver):
                    cookies = driver.get_cookies()
                    facebook_user_id = next(
                        (
                            str(c.get("value") or "").strip()
                            for c in cookies
                            if isinstance(c, dict) and c.get("name") == "c_user"
                        ),
                        "",
                    )
                    duplicate_title = next(
                        (
                            title
                            for title, saved in list_cdp_accounts().items()
                            if title != account
                            and facebook_user_id
                            and str(saved.get("facebook_user_id") or "") == facebook_user_id
                        ),
                        None,
                    )
                    if duplicate_title:
                        _update_account(
                            account,
                            status="failed",
                            message=f"This Facebook account is already saved as {duplicate_title}. Log out in Chrome and use a different account.",
                        )
                        _close_dedicated_browser(driver)
                        driver = None
                        return
                    session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
                    with open(session_path, "wb") as handle:
                        pickle.dump(cookies, handle)
                    _update_account(
                        account,
                        status="ready",
                        message="Facebook login saved. Posting browser is ready.",
                        facebook_user_id=facebook_user_id,
                        cookies_updated_at=int(time.time()),
                    )
                    _close_dedicated_browser(driver)
                    driver = None
                    return
            except Exception:
                if driver is not None:
                    _detach(driver)
                    driver = None
            time.sleep(2)
        _update_account(account, status="failed", message="Login was not completed within 40 minutes. Open login again to retry.")
    finally:
        if driver is not None:
            _detach(driver)
        with _config_lock:
            _watchers.pop(account, None)


def start_cdp_login(account):
    account = str(account or "").strip()
    if not account:
        raise ValueError("Enter an account title.")
    if re.search(r"[\\/:*?\"<>|]", account):
        raise ValueError('Account title cannot contain \\ / : * ? " < > or |.')
    existing_meta = get_cdp_account(account)
    session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
    # A ready mapping without its exported session means the user deleted the
    # account before complete-CDP cleanup existed. Remove that old identity so
    # it can never reopen the deleted Facebook login.
    if existing_meta and existing_meta.get("status") == "ready" and not os.path.isfile(session_path):
        delete_cdp_account(account)
    with _config_lock:
        accounts = _load_config()
        meta = dict(accounts.get(account) or {})
        if not meta:
            meta = {
                "port": _next_port(accounts),
                "profile_dir": str(_profiles_root() / f"{_safe_slug(account)}_{uuid4().hex[:10]}"),
            }
        meta.update(status="waiting", message="Complete the Facebook login in the Chrome window.")
        accounts[account] = meta
        _save_config(accounts)
    proc = _launch_profile(meta, background=False)
    if proc is not None:
        _update_account(account, pid=proc.pid)
    with _config_lock:
        watcher = _watchers.get(account)
        if not watcher or not watcher.is_alive():
            watcher = threading.Thread(target=_capture_when_logged_in, args=(account,), daemon=True)
            _watchers[account] = watcher
            watcher.start()
    return get_cdp_account(account)


def refresh_cdp_cookies(account):
    meta = get_cdp_account(account)
    if not meta:
        raise RuntimeError("No CDP profile is configured for this account.")
    if not _port_open(meta["port"]):
        _launch_profile(meta, background=is_run_in_background_enabled())
        deadline = time.time() + 15
        while time.time() < deadline and not _port_open(meta["port"]):
            time.sleep(0.25)
    driver = _attach(meta["port"])
    try:
        if not _facebook_login_confirmed(driver):
            raise RuntimeError("Facebook is not logged in in the dedicated Chrome profile.")
        cookies = driver.get_cookies()
        with open(os.path.join(sessions_dir(), _safe_session_filename(account)), "wb") as handle:
            pickle.dump(cookies, handle)
        _update_account(account, status="ready", message="Facebook cookies refreshed.", cookies_updated_at=int(time.time()))
    finally:
        _detach(driver)


def close_cdp_browser(account):
    """Best-effort close for the dedicated account browser after login."""
    meta = get_cdp_account(account)
    if not meta or not _port_open(meta.get("port")):
        return False
    driver = None
    try:
        driver = _attach(meta["port"])
        _close_dedicated_browser(driver)
        driver = None
        return True
    except Exception:
        return False
    finally:
        if driver is not None:
            _detach(driver)


def delete_cdp_account(account):
    """Remove an account's CDP mapping and its dedicated Chrome profile."""
    account = str(account or "").strip()
    with _config_lock:
        accounts = _load_config()
        meta = dict(accounts.get(account) or {})
    if not meta:
        return False

    if _port_open(meta.get("port")):
        driver = None
        try:
            driver = _attach(meta["port"])
            _close_dedicated_browser(driver)
            driver = None
        except Exception:
            pass
        finally:
            if driver is not None:
                _detach(driver)
        deadline = time.time() + 5
        while time.time() < deadline and _port_open(meta.get("port")):
            time.sleep(0.2)

    with _config_lock:
        accounts = _load_config()
        accounts.pop(account, None)
        _save_config(accounts)
        _watchers.pop(account, None)

    _delete_profile_folder(meta.get("profile_dir"))
    return True


def cleanup_orphaned_cdp_accounts():
    """Remove legacy CDP profiles whose visible portal account was deleted."""
    removed = []
    for account, meta in list_cdp_accounts().items():
        session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
        if meta.get("status") == "ready" and not os.path.isfile(session_path):
            if delete_cdp_account(account):
                removed.append(account)
    return removed


def open_posting_tab(account):
    account = str(account or "").strip()
    meta = get_cdp_account(account)
    if not meta or meta.get("status") != "ready":
        raise RuntimeError("Open and complete the dedicated Facebook login for this account first.")
    lock = _account_locks.setdefault(account, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("Another posting operation is already using this account.")
    driver = None
    try:
        if not _port_open(meta["port"]):
            _launch_profile(meta, background=is_run_in_background_enabled())
            deadline = time.time() + 15
            while time.time() < deadline and not _port_open(meta["port"]):
                time.sleep(0.25)
        driver = _attach(meta["port"])
        original_handles = set(driver.window_handles)
        driver.switch_to.new_window("tab")
        posting_handle = driver.current_window_handle
        return driver, posting_handle, original_handles, lock
    except Exception:
        if driver is not None:
            _detach(driver)
        lock.release()
        raise


def close_posting_tab(driver, posting_handle, original_handles, lock):
    try:
        _close_dedicated_browser(driver)
    except Exception:
        pass
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass
