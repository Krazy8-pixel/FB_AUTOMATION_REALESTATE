"""Headless packaged-driver smoke test using isolated profiles and no Facebook."""
import json
import ssl
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from selenium.webdriver.chrome.options import Options
from automation.chromedriver import build_chrome_driver, installed_chrome_path


def run(folder):
    report = {'ssl':{}, 'launch':False, 'attach':False}
    output = Path(folder) / 'driver-smoke.json'
    try:
        try:
            ssl.create_default_context()
            report['ssl'] = {'ok':True, 'version':ssl.OPENSSL_VERSION}
        except Exception as error:
            report['ssl'] = {'ok':False, 'error':repr(error)}
            raise
        with tempfile.TemporaryDirectory(prefix='driver-smoke-', dir=folder) as temporary:
            options = Options()
            options.add_argument('--headless=new')
            options.add_argument('--no-first-run')
            options.add_argument('--user-data-dir=' + str(Path(temporary) / 'launch'))
            driver = build_chrome_driver(options)
            try:
                driver.get('data:text/html,<title>Driver startup works</title>')
                assert driver.title == 'Driver startup works'
                report['launch'] = True
            finally:
                driver.quit()
            profile = Path(temporary) / 'attach'
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            process = subprocess.Popen([installed_chrome_path(), '--headless=new', '--no-first-run',
                                        '--no-default-browser-check', '--remote-debugging-address=127.0.0.1',
                                        '--remote-debugging-port=0', '--user-data-dir=' + str(profile),
                                        'about:blank'], creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            driver = None
            try:
                port_file = profile / 'DevToolsActivePort'
                deadline = time.monotonic() + 25
                while not port_file.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Isolated Chrome exited before its debugging port became ready.')
                    time.sleep(0.1)
                port = int(port_file.read_text().splitlines()[0])
                options = Options()
                options.add_experimental_option('debuggerAddress', '127.0.0.1:' + str(port))
                driver = build_chrome_driver(options)
                driver.get('data:text/html,<title>Existing browser connection works</title>')
                assert driver.title == 'Existing browser connection works'
                report['attach'] = True
                try:
                    driver.execute_cdp_cmd('Browser.close', {})
                except Exception:
                    pass
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
        report['ok'] = True
    except Exception:
        report['ok'] = False
        report['error'] = traceback.format_exc()
        raise
    finally:
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
