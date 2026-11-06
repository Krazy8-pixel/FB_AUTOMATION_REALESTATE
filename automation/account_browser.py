"""One saved Chrome profile embedded in the Windows desktop window."""
import ctypes
from ctypes import wintypes
import os
import pickle
import subprocess
import sys
import threading
import time
from uuid import uuid4

from automation import cdp_login
from automation.fb_login import _safe_session_filename
from automation.runtime_paths import sessions_dir
from automation.session_health import HEALTHY, set_session_health
from automation.task_control import active_tasks

_lock = threading.RLock()
_account = _account_lock = _chrome_hwnd = None
_generation = 0
_needs_focus = False
_mobile_driver = None
_mobile_metrics = None
user32 = ctypes.windll.user32 if os.name == 'nt' else None
kernel32 = ctypes.windll.kernel32 if os.name == 'nt' else None
gdi32 = ctypes.windll.gdi32 if os.name == 'nt' else None
WS_CHILD, WS_POPUP, WS_CAPTION, WS_THICKFRAME = 0x40000000, 0x80000000, 0x00C00000, 0x00040000
WS_EX_FRAME_EDGES = 0x00000100 | 0x00000200 | 0x00000001 | 0x00020000
SW_HIDE, SW_SHOW, SWP_NOZORDER, SWP_FRAMECHANGED = 0, 5, 4, 0x20
if user32:
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
    user32.SetWindowRgn.restype = ctypes.c_int
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gdi32.CreateRectRgn.restype = wintypes.HANDLE
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _chrome_content_window(hwnd):
    """Find Chrome's keyboard-receiving content widget inside the embedded window."""
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(child, _):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, name, len(name))
        if name.value == 'Chrome_RenderWidgetHostHWND' and user32.IsWindowVisible(child):
            found.append(child)
        return True

    user32.EnumChildWindows(hwnd, visit, 0)
    return found[-1] if found else hwnd


def _focus_browser(hwnd, host):
    """Connect the request thread to Chrome's input queue before focusing it."""
    current = kernel32.GetCurrentThreadId()
    chrome_thread = user32.GetWindowThreadProcessId(hwnd, None)
    host_thread = user32.GetWindowThreadProcessId(host, None)
    attached = []
    try:
        for target in {chrome_thread, host_thread}:
            if target and target != current and user32.AttachThreadInput(current, target, True):
                attached.append(target)
        user32.SetForegroundWindow(host)
        user32.SetFocus(_chrome_content_window(hwnd))
    finally:
        for target in reversed(attached):
            user32.AttachThreadInput(current, target, False)


def _window_for_pid(pid):
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def _save_cookies(account, driver):
    cookies = driver.get_cookies()
    if not cookies:
        return False
    path = os.path.join(sessions_dir(), _safe_session_filename(account))
    temporary = f'{path}.{uuid4().hex}.tmp'
    try:
        with open(temporary, 'wb') as stream:
            pickle.dump(cookies, stream)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass
    cdp_login._update_account(account, status='ready', relogin=False,
                              cookies_updated_at=int(time.time()),
                              message='Facebook login saved. Posting browser is ready.')
    set_session_health(account, HEALTHY)
    return True


def _watch(account, generation):
    last_refresh = 0
    while True:
        time.sleep(5)
        with _lock:
            if generation != _generation or account != _account:
                return
        meta = cdp_login.get_cdp_account(account)
        if not meta or not cdp_login._port_open(meta.get('port')):
            return
        driver = None
        try:
            driver = cdp_login._attach(meta['port'])
            if cdp_login._facebook_login_confirmed(driver) and (not last_refresh or time.time() - last_refresh >= 60):
                if _save_cookies(account, driver):
                    last_refresh = time.time()
        except Exception:
            pass
        finally:
            if driver:
                cdp_login._detach(driver)


def status():
    with _lock:
        return {'account': _account, 'open': _account is not None, 'mobile': _mobile_driver is not None,
                'external': sys.platform == 'darwin'}


def _activate_external_chrome():
    try:
        subprocess.run(['/usr/bin/open', '-a', 'Google Chrome'],
                       check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _watch_external(account, generation):
    global _account, _account_lock, _mobile_driver
    last_refresh = 0
    while True:
        time.sleep(3)
        with _lock:
            if generation != _generation or account != _account:
                return
        meta = cdp_login.get_cdp_account(account)
        if not meta or not cdp_login._port_open(meta.get('port')):
            break
        driver = None
        try:
            driver = cdp_login._attach(meta['port'])
            if cdp_login._facebook_login_confirmed(driver) and (not last_refresh or time.time() - last_refresh >= 60):
                if _save_cookies(account, driver):
                    last_refresh = time.time()
        except Exception:
            pass
        finally:
            if driver:
                cdp_login._detach(driver)
    with _lock:
        if generation == _generation and account == _account:
            lock = _account_lock
            _account = _account_lock = None
            if _mobile_driver:
                cdp_login._detach(_mobile_driver)
            _mobile_driver = None
            if lock:
                lock.release()


def _open_external_account(account):
    global _account, _account_lock, _generation, _needs_focus
    account = str(account or '').strip()
    meta = cdp_login.get_cdp_account(account)
    if not meta or meta.get('status') not in {'ready', 'relogin_expired'}:
        raise ValueError('This account needs a saved Chrome login. Add or re-login to it first.')
    with _lock:
        if _account == account and cdp_login._port_open(meta.get('port')):
            _activate_external_chrome()
            return status()
        if active_tasks(account):
            raise RuntimeError('This account is running automation. Wait for it to finish.')
        if _account:
            stop()
        account_lock = cdp_login._account_locks.setdefault(account, threading.Lock())
        if not account_lock.acquire(blocking=False):
            raise RuntimeError('This account is being used by another operation. Wait for it to finish.')
        try:
            if cdp_login._port_open(meta.get('port')):
                cdp_login.close_cdp_browser(account)
            process = cdp_login._launch_profile(meta, background=False, app_mode=False)
            deadline = time.time() + 20
            while time.time() < deadline and process.poll() is None:
                if cdp_login._port_open(meta.get('port')):
                    break
                time.sleep(0.25)
            if not cdp_login._port_open(meta.get('port')):
                raise RuntimeError('Chrome did not open this account profile.')
            _account, _account_lock = account, account_lock
            _needs_focus = False
            _generation += 1
            generation = _generation
            _activate_external_chrome()
            threading.Thread(target=_watch_external, args=(account, generation), daemon=True).start()
            return status()
        except Exception:
            if cdp_login._port_open(meta.get('port')):
                cdp_login.close_cdp_browser(account)
            account_lock.release()
            raise


def hide():
    with _lock:
        if user32 and _chrome_hwnd and user32.IsWindow(_chrome_hwnd):
            user32.ShowWindow(_chrome_hwnd, SW_HIDE)
    return status()


def stop(*, force=False):
    global _account, _account_lock, _chrome_hwnd, _generation, _needs_focus, _mobile_driver, _mobile_metrics
    with _lock:
        if _account is None:
            return status()
        if not force and active_tasks(_account):
            raise RuntimeError('This account has active automation. Wait for it to finish.')
        account, lock = _account, _account_lock
        if _mobile_driver:
            cdp_login._detach(_mobile_driver)
            _mobile_driver = None
        _account = _account_lock = _chrome_hwnd = None
        _needs_focus = False
        _generation += 1
        try:
            meta = cdp_login.get_cdp_account(account)
            if meta and cdp_login._port_open(meta.get('port')):
                driver = None
                try:
                    driver = cdp_login._attach(meta['port'])
                    if cdp_login._facebook_login_confirmed(driver):
                        _save_cookies(account, driver)
                except Exception:
                    pass
                finally:
                    if driver:
                        cdp_login._detach(driver)
            cdp_login.close_cdp_browser(account)
        finally:
            if lock:
                lock.release()
    return status()


def open_account(account):
    global _account, _account_lock, _chrome_hwnd, _generation, _needs_focus
    if os.getenv('FB_AUTOMATION_DESKTOP') != '1':
        raise RuntimeError('Account Browser is available in the desktop app only.')
    if sys.platform == 'darwin':
        return _open_external_account(account)
    if os.name != 'nt':
        raise RuntimeError('Account Browser is supported in the Windows and Mac desktop apps.')
    account = str(account or '').strip()
    meta = cdp_login.get_cdp_account(account)
    if not meta or meta.get('status') not in {'ready', 'relogin_expired'}:
        raise ValueError('This account needs a saved Chrome login. Add or re-login to it first.')
    with _lock:
        if _account == account:
            if _chrome_hwnd and user32.IsWindow(_chrome_hwnd) and cdp_login._port_open(meta.get('port')):
                return status()
            stop(force=True)
        if active_tasks(account):
            raise RuntimeError('This account is running automation. Wait for it to finish.')
        account_lock = cdp_login._account_locks.setdefault(account, threading.Lock())
        if not account_lock.acquire(blocking=False):
            raise RuntimeError('This account is being used by another operation. Wait for it to finish.')
        try:
            if _account:
                stop()
            if cdp_login._port_open(meta.get('port')):
                cdp_login.close_cdp_browser(account)
            proc = cdp_login._launch_profile(meta, background=False, app_mode=True)
            deadline, hwnd = time.time() + 15, None
            while time.time() < deadline:
                hwnd = _window_for_pid(proc.pid)
                if hwnd and cdp_login._port_open(meta['port']):
                    break
                time.sleep(0.2)
            if not hwnd:
                raise RuntimeError('Chrome did not create an account window.')
            host = user32.FindWindowW(None, 'FBMK Automation Realestate')
            if not host:
                raise RuntimeError('The desktop app window is not available.')
            user32.ShowWindow(hwnd, SW_HIDE)
            style = user32.GetWindowLongW(hwnd, -16)
            user32.SetWindowLongW(hwnd, -16, (style | WS_CHILD) & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME))
            extended_style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, extended_style & ~WS_EX_FRAME_EDGES)
            if not user32.SetParent(hwnd, host):
                raise RuntimeError('Could not place Chrome inside the app window.')
            _account, _account_lock, _chrome_hwnd = account, account_lock, hwnd
            _needs_focus = True
            _generation += 1
            threading.Thread(target=_watch, args=(account, _generation), daemon=True).start()
            return status()
        except Exception:
            if cdp_login._port_open(meta.get('port')):
                cdp_login.close_cdp_browser(account)
            account_lock.release()
            raise



MOBILE_POSTING_URL = 'https://m.facebook.com/marketplace/create/'
MOBILE_USER_AGENT = ('Mozilla/5.0 (Linux; Android 14; Pixel 9) '
                     'AppleWebKit/537.36 (KHTML, like Gecko) '
                     'Chrome/131.0.0.0 Mobile Safari/537.36')

GROUP_POSTS_URL = ('https://www.facebook.com/61582828106692/allactivity?'
                   'activity_history=false&category_key=GROUPPOSTS&manage_mode=false&'
                   'should_load_landing_page=false')
MARKETPLACE_ADS_URL = 'https://www.facebook.com/marketplace/you/selling'
FACEBOOK_HOME_URL = 'https://www.facebook.com/'


def _navigate_desktop_page(account, url):
    """Navigate the selected account's embedded Chrome to a Facebook page."""
    global _mobile_driver, _needs_focus, _mobile_metrics
    with _lock:
        open_account(account)
        meta = cdp_login.get_cdp_account(account)
        driver = _mobile_driver
        if driver:
            try:
                driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
                driver.execute_cdp_cmd('Emulation.setUserAgentOverride', {'userAgent': ''})
                driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {'enabled': False})
            finally:
                cdp_login._detach(driver)
                _mobile_driver = None
                _mobile_metrics = None
            driver = None
        if not driver:
            driver = cdp_login._attach(meta['port'])
        try:
            driver.execute_cdp_cmd('Page.navigate', {'url': url})
            _needs_focus = True
            if sys.platform == 'darwin':
                _activate_external_chrome()
        finally:
            cdp_login._detach(driver)
        return status()


def open_group_posts_page(account):
    return _navigate_desktop_page(account, GROUP_POSTS_URL)


def open_marketplace_ads_page(account):
    return _navigate_desktop_page(account, MARKETPLACE_ADS_URL)


def return_home():
    """Stop the current Facebook destination by returning the open browser home."""
    global _mobile_driver, _needs_focus, _mobile_metrics
    with _lock:
        if not _account:
            return status()
        meta = cdp_login.get_cdp_account(_account)
        driver = _mobile_driver
        if driver:
            try:
                driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
                driver.execute_cdp_cmd('Emulation.setUserAgentOverride', {'userAgent': ''})
                driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {'enabled': False})
            finally:
                cdp_login._detach(driver)
                _mobile_driver = None
                _mobile_metrics = None
            driver = None
        if meta and cdp_login._port_open(meta.get('port')):
            driver = cdp_login._attach(meta['port'])
            try:
                driver.execute_cdp_cmd('Page.navigate', {'url': FACEBOOK_HOME_URL})
                _needs_focus = True
            finally:
                cdp_login._detach(driver)
        return status()


def open_posting_page(account):
    """Open the selected account's Marketplace creation page in mobile emulation."""
    global _mobile_driver, _needs_focus, _mobile_metrics
    with _lock:
        open_account(account)
        meta = cdp_login.get_cdp_account(account)
        driver = _mobile_driver or cdp_login._attach(meta['port'])
        try:
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': 412, 'height': 915, 'deviceScaleFactor': 2.625, 'mobile': True,
            })
            driver.execute_cdp_cmd('Emulation.setUserAgentOverride', {
                'userAgent': MOBILE_USER_AGENT, 'platform': 'Android',
            })
            driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {'enabled': True})
            driver.execute_cdp_cmd('Page.navigate', {'url': MOBILE_POSTING_URL})
            if sys.platform == 'darwin':
                _activate_external_chrome()
            _mobile_driver = driver
            _mobile_metrics = (412, 915)
            _needs_focus = True
        except Exception:
            try:
                driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
                driver.execute_cdp_cmd('Emulation.setUserAgentOverride', {'userAgent': ''})
                driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {'enabled': False})
            except Exception:
                pass
            cdp_login._detach(driver)
            _mobile_driver = None
            _mobile_metrics = None
            raise
        return status()


def desktop_view():
    global _mobile_driver, _needs_focus, _mobile_metrics
    with _lock:
        if _mobile_driver:
            driver = _mobile_driver
            try:
                driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
                driver.execute_cdp_cmd('Emulation.setUserAgentOverride', {'userAgent': ''})
                driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {'enabled': False})
                driver.execute_cdp_cmd('Page.navigate', {'url': 'https://www.facebook.com/'})
                _needs_focus = True
                if sys.platform == 'darwin':
                    _activate_external_chrome()
            finally:
                cdp_login._detach(driver)
                _mobile_driver = None
                _mobile_metrics = None
        return status()

def layout(x, y, width, height, viewport_width=None, viewport_height=None):
    global _needs_focus, _mobile_metrics
    with _lock:
        if not _chrome_hwnd or not user32.IsWindow(_chrome_hwnd):
            return status()
        host = user32.FindWindowW(None, 'FBMK Automation Realestate')
        client = wintypes.RECT()
        if not host or not user32.GetClientRect(host, ctypes.byref(client)):
            raise RuntimeError('The app window is not available for browser sizing.')
        client_width = client.right - client.left
        client_height = client.bottom - client.top
        css_width = float(viewport_width or 0)
        css_height = float(viewport_height or 0)
        if css_width <= 0 or css_height <= 0:
            raise ValueError('Browser view size is not available.')
        scale_x = client_width / css_width
        scale_y = client_height / css_height
        left = max(0, min(client_width - 1, int(float(x) * scale_x)))
        top = max(0, min(client_height - 1, int(float(y) * scale_y)))
        wide = max(1, min(client_width - left, int(float(width) * scale_x)))
        high = max(1, min(client_height - top, int(float(height) * scale_y)))
        # Match the emulated screen to the visible browser area. A fixed phone
        # height places Facebook's fixed bottom controls below the clipped window.
        if _mobile_driver:
            mobile_width = max(1, min(412, round(float(width))))
            mobile_height = max(1, round(float(height)))
            metrics = (mobile_width, mobile_height)
            if metrics != _mobile_metrics:
                _mobile_driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                    'width': mobile_width, 'height': mobile_height,
                    'deviceScaleFactor': 2.625, 'mobile': True,
                })
                _mobile_metrics = metrics
        # Chrome app windows paint their own title strip and dark outer frame
        # inside the child window. Crop those pixels with a window region.
        edge_x = max(1, round(10 * scale_x))
        caption = max(1, round(28 * scale_y))
        edge_bottom = max(1, round(10 * scale_y))
        region = gdi32.CreateRectRgn(edge_x, caption, edge_x + wide, caption + high)
        if not region:
            raise RuntimeError('Could not prepare the Facebook viewport.')
        if not user32.SetWindowRgn(_chrome_hwnd, region, True):
            gdi32.DeleteObject(region)
            raise RuntimeError('Could not crop Chrome to the Facebook viewport.')
        if not user32.SetWindowPos(_chrome_hwnd, None, left - edge_x, top - caption,
                                   wide + 2 * edge_x, high + caption + edge_bottom,
                                   SWP_NOZORDER | SWP_FRAMECHANGED):
            raise RuntimeError(f'Could not size Chrome inside the app: {ctypes.WinError()}')
        user32.ShowWindow(_chrome_hwnd, SW_SHOW)
        if _needs_focus:
            _focus_browser(_chrome_hwnd, host)
            _needs_focus = False
    return status()
