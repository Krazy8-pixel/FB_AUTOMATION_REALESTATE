"""Shared Chrome anti-automation hints for website scraping sessions."""


_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


def configure_stealth_options(options):
    """Apply the same low-noise Chrome options used by the other browsers."""
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--log-level=3")
    options.add_experimental_option(
        "excludeSwitches", ["enable-logging", "enable-automation"]
    )
    options.add_experimental_option("useAutomationExtension", False)
    return options


def apply_stealth_patches(driver):
    """Install patches for both the current document and future documents."""
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_SCRIPT}
        )
    except Exception:
        pass
    try:
        driver.execute_script(_STEALTH_SCRIPT)
    except Exception:
        pass
