from automation.responsiveness import RequestLock
from automation.diagnostics import event as diagnostic_event
import json
from automation.task_control import cancellable, check_cancelled
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
from urllib.parse import urlsplit

from selenium.webdriver.chrome.options import Options

from automation.browser_settings import is_run_in_background_enabled
from automation.chromedriver import build_chrome_driver, installed_chrome_path
from automation.fb_login import _is_home_screen_visible, _safe_session_filename
from automation.runtime_paths import data_dir, sessions_dir


_config_lock = RequestLock('cdp_login._config_lock', reentrant=True)
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
    if os.getenv('FB_AUTOMATION_DESKTOP') == '1':
        return installed_chrome_path()
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


def _launch_profile(meta, *, background=False, app_mode=False):
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
        ]
    chrome_args.append("--app=https://www.facebook.com/" if app_mode else "https://www.facebook.com/")
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
    driver = build_chrome_driver(options)
    config = getattr(driver.command_executor, '_client_config', None)
    if config is not None:
        config.timeout = 60
    return driver


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
        # Attached Chrome is not owned by ChromeDriver: stopping its service
        # alone leaves Chrome open. Fall back to closing this dedicated
        # browser's windows if the CDP close command fails.
        try:
            for handle in list(driver.window_handles):
                try:
                    driver.switch_to.window(handle)
                    driver.close()
                except Exception:
                    pass
        except Exception:
            pass
    finally:
        _detach(driver)


def _facebook_login_confirmed(driver):
    """Confirm the authenticated Facebook home location and session cookie."""
    current_url = str(driver.current_url or "").lower()
    hostname = urlsplit(current_url).hostname or ""
    if hostname != "facebook.com" and not hostname.endswith(".facebook.com"):
        return False
    if any(marker in current_url for marker in ("/login", "checkpoint", "two_step_verification")):
        return False

    # Cookie presence alone is insufficient: require authenticated home UI.
    cookies = driver.get_cookies()
    has_authenticated_cookie = any(
        c.get("name") == "c_user" and str(c.get("value") or "").strip()
        for c in cookies
        if isinstance(c, dict)
    )
    return bool(has_authenticated_cookie and _is_home_screen_visible(driver))


def _find_logged_in_tab(driver):
    """Login may finish in a different tab from the one ChromeDriver attached to."""
    if _facebook_login_confirmed(driver):
        return True
    original = driver.current_window_handle
    for handle in driver.window_handles:
        if handle == original:
            continue
        driver.switch_to.window(handle)
        if _facebook_login_confirmed(driver):
            return True
    driver.switch_to.window(original)
    return False


@cancellable
def _capture_when_logged_in(account):
    deadline = time.time() + LOGIN_WAIT_SECONDS
    driver = None
    diagnostic_event('CDP_LOGIN', 'WAITING_FOR_FACEBOOK', account=account)
    try:
        while time.time() < deadline:
            check_cancelled()
            meta = get_cdp_account(account)
            if not meta:
                diagnostic_event('CDP_LOGIN', 'STOPPED', account=account,
                                 reason='Login profile was removed before login completed')
                return
            try:
                if driver is None:
                    driver = _attach(meta["port"])
                if _find_logged_in_tab(driver):
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
                    if meta.get('relogin') and meta.get('facebook_user_id') and facebook_user_id != meta['facebook_user_id']:
                        _update_account(account, status='failed',
                                        message='Sign into the original Facebook account. Saved cookies and data were not replaced.')
                        diagnostic_event('CDP_LOGIN', 'FAILED', account=account,
                                         reason='A different Facebook account was used for re-login')
                        time.sleep(2)
                        continue
                    if duplicate_title:
                        _update_account(
                            account,
                            status="failed",
                            message=f"This Facebook account is already saved as {duplicate_title}. Log out in Chrome and use a different account.",
                        )
                        _close_dedicated_browser(driver)
                        driver = None
                        diagnostic_event('CDP_LOGIN', 'FAILED', account=account,
                                         reason=f'Facebook account is already saved as {duplicate_title}')
                        return
                    session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
                    with open(session_path, "wb") as handle:
                        pickle.dump(cookies, handle)
                    from automation.session_health import set_session_health, HEALTHY
                    set_session_health(account, HEALTHY)
                    # Finish browser cleanup before publishing ready to the UI.
                    _close_dedicated_browser(driver)
                    driver = None
                    _update_account(
                        account,
                        status="ready",
                        relogin=False,
                        message="Facebook login saved. Posting browser is ready.",
                        facebook_user_id=facebook_user_id,
                        cookies_updated_at=int(time.time()),
                    )
                    diagnostic_event('CDP_LOGIN', 'COMPLETED', account=account)
                    return
                _update_account(
                    account,
                    status='waiting',
                    message='Connected to Chrome. Waiting for the authenticated Facebook home page; finish any verification and open Facebook Home.',
                )
            except Exception as exc:
                message = str(exc)
                message = message or 'Could not connect to the login browser. Please try again.'
                current = get_cdp_account(account) or {}
                if current.get('message') != message or current.get('status') != 'failed':
                    print(f'[CDP Login] browser connection failed: {type(exc).__name__}: {message}', flush=True)
                    _update_account(account, status='failed', message=message)
                    diagnostic_event('CDP_LOGIN', 'FAILED', account=account,
                                     error_type=type(exc).__name__, error=message)
                if driver is not None:
                    _detach(driver)
                    driver = None
            time.sleep(2)
        print(f"[CDP Login] {account}: login timed out after 40 minutes; closing Chrome and removing the attempt.", flush=True)
        diagnostic_event('CDP_LOGIN', 'TIMED_OUT', account=account)
        if driver is not None:
            _close_dedicated_browser(driver)
            driver = None
        if (get_cdp_account(account) or {}).get('relogin'):
            _update_account(account, status='relogin_expired', relogin=True,
                            message='Re-login timed out. Saved account data was kept.')
        else:
            delete_cdp_account(account)
    except BaseException as exc:
        diagnostic_event('CDP_LOGIN', 'CANCELLED' if 'cancel' in type(exc).__name__.casefold()
                         else 'STOPPED', account=account, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        if driver is not None:
            _detach(driver)
        with _config_lock:
            _watchers.pop(account, None)


def start_cdp_login(account, relogin=False):
    account = str(account or "").strip()
    if not account:
        raise ValueError("Enter an account title.")
    if re.search(r"[\\/:*?\"<>|]", account):
        raise ValueError('Account title cannot contain \\ / : * ? " < > or |.')
    existing_meta = get_cdp_account(account)
    if relogin and _account_locks.get(account) and _account_locks[account].locked():
        raise RuntimeError('Wait for this account\'s posting operation to finish before re-login.')
    if relogin and existing_meta and _port_open(existing_meta.get('port')):
        close_cdp_browser(account)
    session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
    # A ready mapping without its exported session means the user deleted the
    # account before complete-CDP cleanup existed. Remove that old identity so
    # it can never reopen the deleted Facebook login.
    if not relogin and existing_meta and existing_meta.get("status") == "ready" and not os.path.isfile(session_path):
        delete_cdp_account(account)
    with _config_lock:
        accounts = _load_config()
        meta = dict(accounts.get(account) or {})
        if not meta:
            meta = {
                "port": _next_port(accounts),
                "profile_dir": str(_profiles_root() / f"{_safe_slug(account)}_{uuid4().hex[:10]}"),
            }
        meta.update(
            status="waiting",
            relogin=relogin,
            message="Complete the Facebook login in the Chrome window.",
            login_started_at=int(time.time()),
        )
        accounts[account] = meta
        _save_config(accounts)
    try:
        proc = _launch_profile(meta, background=False)
    except Exception as exc:
        _update_account(account, status='failed', message=f'Could not open Chrome: {exc}')
        diagnostic_event('CDP_LOGIN', 'FAILED_TO_START', account=account,
                         relogin=relogin, error_type=type(exc).__name__, error=str(exc))
        raise
    if proc is not None:
        _update_account(account, pid=proc.pid)
    diagnostic_event('CDP_LOGIN', 'STARTED', account=account, relogin=relogin)
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
    lock = _account_locks.setdefault(account, threading.Lock())
    if not lock.acquire(timeout=60):
        raise RuntimeError("The Facebook profile is busy with another operation. Try again shortly.")
    try:
        launched = not _port_open(meta["port"])
        if launched:
            _launch_profile(meta, background=is_run_in_background_enabled())
            deadline = time.time() + 15
            while time.time() < deadline and not _port_open(meta["port"]):
                time.sleep(0.25)
        driver = _attach(meta["port"])
        try:
            if not _facebook_login_confirmed(driver):
                raise RuntimeError("Facebook is not logged in in the dedicated Chrome profile.")
            cookies = driver.get_cookies()
            session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
            temp_path = f"{session_path}.{uuid4().hex}.tmp"
            try:
                with open(temp_path, "wb") as handle:
                    pickle.dump(cookies, handle)
                os.replace(temp_path, session_path)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            _update_account(
                account,
                status="ready",
                message="Facebook login saved. Posting browser is ready.",
                cookies_updated_at=int(time.time()),
            )
        finally:
            if launched:
                _close_dedicated_browser(driver)
            else:
                _detach(driver)
    finally:
        lock.release()


def load_current_cdp_cookies(account):
    """Refresh and return the cookie snapshot from a ready persistent profile.

    Older accounts that have no CDP profile return None so their legacy cookie
    file remains usable. For CDP accounts, callers should use this snapshot
    instead of treating an older exported pickle as the current login.
    """
    meta = get_cdp_account(account)
    if not meta or meta.get("status") != "ready":
        return None
    refresh_cdp_cookies(account)
    session_path = os.path.join(sessions_dir(), _safe_session_filename(account))
    with open(session_path, "rb") as handle:
        cookies = pickle.load(handle)
    return cookies if isinstance(cookies, list) else None


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


def cleanup_expired_cdp_logins():
    """Close and remove unfinished CDP login attempts older than 40 minutes."""
    removed = []
    cutoff = int(time.time()) - LOGIN_WAIT_SECONDS
    for account, meta in list_cdp_accounts().items():
        if meta.get("status") in {"ready", "relogin_expired"}:
            continue
        watcher = _watchers.get(account)
        if meta.get('status') == 'waiting' and (not watcher or not watcher.is_alive()):
            _update_account(account, status='failed',
                            message='Login was interrupted. Open Facebook Login again to retry.')
        try:
            started_at = int(meta.get("login_started_at") or 0)
        except (TypeError, ValueError):
            started_at = 0
        # Non-ready records created by older versions have no start time and
        # are the permanently stuck attempts this cleanup is intended to fix.
        if (not started_at or started_at <= cutoff) and meta.get('relogin'):
            _update_account(account, status='relogin_expired', relogin=True,
                            message='Re-login timed out. Saved account data was kept.')
            continue
        if (not started_at or started_at <= cutoff) and delete_cdp_account(account):
            removed.append(account)
            print(
                f"[CDP Login] {account}: removed expired unfinished login attempt.",
                flush=True,
            )
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
