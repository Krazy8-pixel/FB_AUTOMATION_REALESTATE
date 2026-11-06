import os
import sys
import re
import subprocess
import json
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException
from selenium.webdriver.chrome.service import Service as SeleniumService
from selenium.webdriver.common.selenium_manager import SeleniumManager

from automation.runtime_paths import resource_root, data_dir


class Service(SeleniumService):
    """Check ChromeDriver's loopback HTTP endpoint without initializing TLS.

    Selenium's urllib opener also constructs an HTTPS handler for HTTP URLs.
    In a frozen runtime, unrelated SSL initialization failures can therefore
    falsely report a healthy local driver as unreachable. Keep remote HTTPS
    verification unchanged and use plain HTTP only for this local service.
    """
    def is_connectable(self):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=1)
        try:
            connection.request('GET', '/status')
            response = connection.getresponse()
            if response.status != 200:
                return False
            data = json.loads(response.read(1024 * 1024))
            return data.get('ready', data.get('value', {}).get('ready')) is True
        except (OSError, ValueError, HTTPException, AttributeError, TypeError):
            return False
        finally:
            connection.close()

    def send_remote_shutdown_command(self):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request('GET', '/shutdown')
            connection.getresponse().read(1024)
        except (OSError, HTTPException):
            pass
        finally:
            connection.close()


def installed_chrome_path():
    """Find regular installed Chrome without downloading a browser."""
    candidates = [os.getenv('CHROME_PATH', '').strip()]
    if sys.platform == 'darwin':
        candidates.extend([
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            str(Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ])
    elif sys.platform.startswith('win'):
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', 0, winreg.KEY_READ | view) as key:
                        candidates.append(winreg.QueryValueEx(key, '')[0])
                except OSError:
                    pass
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        base = os.getenv(variable)
        if base:
            candidates.append(os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError('Google Chrome is not installed. Install regular Google Chrome, then try again.')


def resolve_chromedriver_path():
    env_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return str(candidate)

    checked = []

    bundled_root = resource_root()
    p = bundled_root / "drivers" / "chromedriver.exe"
    checked.append(str(p))
    if p.exists():
        return str(p)

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        p = exe_dir / "drivers" / "chromedriver.exe"
        checked.append(str(p))
        if p.exists():
            return str(p)

    project_root = Path(__file__).resolve().parent.parent
    p = project_root / "drivers" / "chromedriver.exe"
    checked.append(str(p))
    if p.exists():
        return str(p)

    raise FileNotFoundError(
        "chromedriver.exe not found. Put it at drivers/chromedriver.exe. Checked: "
        + " | ".join(checked)
    )


def build_chrome_service():
    return Service(resolve_chromedriver_path())


def _is_truthy_env(name):
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _run_version_cmd(cmd, timeout_s=3):
    try:
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            timeout=max(1, int(timeout_s or 3)),
            creationflags=creationflags,
        )
        return ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    except Exception:
        return ""


def _parse_first_major_version(text):
    m = re.search(r"\b(\d+)\.", str(text or ""))
    return int(m.group(1)) if m else None


def chromedriver_major_version(driver_path):
    text = _run_version_cmd([str(driver_path), "--version"])
    return _parse_first_major_version(text)


def chrome_major_version():
    """
    Best-effort Chrome major version detection (Windows).
    Returns an int or None.
    """
    if not sys.platform.startswith("win"):
        return None

    # Registry (fastest / most reliable)
    reg_queries = [
        r"HKCU\Software\Google\Chrome\BLBeacon",
        r"HKLM\Software\Google\Chrome\BLBeacon",
        r"HKLM\Software\WOW6432Node\Google\Chrome\BLBeacon",
    ]
    for key in reg_queries:
        text = _run_version_cmd(["reg", "query", key, "/v", "version"])
        major = _parse_first_major_version(text)
        if major:
            return major

    # Fallback: try chrome.exe file version (do not launch Chrome)
    candidates = [
        os.path.join(os.getenv("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.getenv("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            ps = (
                "(Get-Item -LiteralPath "
                + repr(p)
                + ").VersionInfo.ProductVersion"
            )
            major = _parse_first_major_version(_run_version_cmd(["powershell", "-NoProfile", "-Command", ps]))
            if major:
                return major

    return None


def _looks_like_driver_mismatch(exc):
    text = str(exc or "")
    return (
        "only supports chrome version" in text.lower()
        or "session not created" in text.lower()
        or isinstance(exc, SessionNotCreatedException)
    )


def build_chrome_driver(options):
    from automation.task_control import check_cancelled, guard_driver
    check_cancelled()
    return guard_driver(_build_chrome_driver(options))


def _build_chrome_driver(options):
    """
    Create a Chrome WebDriver with a resilient driver selection strategy:

    - If CHROMEDRIVER_PATH is set, use it.
    - Else try the bundled drivers/chromedriver.exe if it looks compatible.
    - If start fails due to version mismatch, retry using Selenium Manager
      with usage stats disabled.

    Env controls:
    - FB_AUTOMATION_FORCE_BUNDLED_CHROMEDRIVER=1 -> never use Selenium Manager fallback
    - FB_AUTOMATION_USE_SELENIUM_MANAGER=1 -> try Selenium Manager first
    """
    desktop = _is_truthy_env('FB_AUTOMATION_DESKTOP')
    chrome_path = installed_chrome_path() if desktop else os.getenv("CHROME_PATH", "").strip()
    if chrome_path:
        options.binary_location = chrome_path

    force_bundled = _is_truthy_env("FB_AUTOMATION_FORCE_BUNDLED_CHROMEDRIVER")
    prefer_manager = _is_truthy_env("FB_AUTOMATION_USE_SELENIUM_MANAGER")

    env_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    bundled_path = None
    try:
        bundled_path = resolve_chromedriver_path()
    except Exception:
        bundled_path = None

    def _try_with_service(path):
        return webdriver.Chrome(service=Service(path), options=options)

    def _try_with_manager():
        if desktop:
            try:
                paths = SeleniumManager().binary_paths([
                    '--browser', 'chrome', '--browser-path', chrome_path,
                    '--avoid-browser-download', '--avoid-stats',
                    '--cache-path', str(data_dir('driver-cache')),
                ])
                return _try_with_service(paths['driver_path'])
            except Exception as exc:
                if isinstance(exc, OSError) and getattr(exc, 'winerror', None) in (4551, 577, 1260):
                    raise RuntimeError('Windows Application Control blocked ChromeDriver. Facebook login cannot be saved until Windows permits this automation driver. Contact the PC administrator; this is not an internet or password error.') from exc
                raise RuntimeError(
                    'Could not connect the Chrome automation driver. '
                    f'{type(exc).__name__}: {exc}'
                ) from exc
        manager_error = None
        try:
            paths = SeleniumManager().binary_paths(["--browser", "chrome", "--avoid-stats"])
            driver_path = paths.get("driver_path")
            if driver_path:
                return webdriver.Chrome(service=Service(driver_path), options=options)
        except Exception as e:
            manager_error = e

        try:
            return webdriver.Chrome(options=options)
        except Exception as e:
            if manager_error is not None:
                raise RuntimeError(
                    f"Selenium Manager could not resolve ChromeDriver: {manager_error}"
                ) from e
            raise

    if desktop:
        return _try_with_manager()

    attempts = []

    if prefer_manager and not force_bundled:
        attempts.append(("selenium-manager", None))

    if env_path:
        attempts.append(("env-chromedriver", env_path))
    elif bundled_path:
        # If we can detect mismatch, avoid a guaranteed-fail attempt.
        chrome_major = chrome_major_version()
        driver_major = chromedriver_major_version(bundled_path)
        if chrome_major and driver_major and chrome_major != driver_major and not force_bundled:
            attempts.append(("selenium-manager", None))
        else:
            attempts.append(("bundled-chromedriver", bundled_path))

    if not force_bundled and ("selenium-manager", None) not in attempts:
        attempts.append(("selenium-manager", None))

    last_exc = None
    for mode, value in attempts:
        try:
            if mode == "selenium-manager":
                return _try_with_manager()
            return _try_with_service(value)
        except (SessionNotCreatedException, WebDriverException) as e:
            last_exc = e
            if mode != "selenium-manager" and not force_bundled and _looks_like_driver_mismatch(e):
                try:
                    return _try_with_manager()
                except Exception as e2:
                    last_exc = e2
            continue
        except Exception as e:
            last_exc = e
            continue

    raise RuntimeError(
        "Failed to start Chrome WebDriver. "
        "If this is a Chrome/ChromeDriver version mismatch, update drivers/chromedriver.exe "
        "or set CHROMEDRIVER_PATH to a matching chromedriver."
    ) from last_exc
