import os
import pickle
import re
from automation.diagnostics import traced

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import sessions_dir

SESSION_DIR = sessions_dir()

MAX_ACCOUNTS = 20
MANUAL_VERIFICATION_WAIT_SECONDS = 60000

LOGIN_FIELD_LOCATORS = [
    (By.ID, "email"),
    (By.NAME, "email"),
    (By.CSS_SELECTOR, "input#email"),
    (By.CSS_SELECTOR, "input[name='email']"),
]

PASSWORD_FIELD_LOCATORS = [
    (By.ID, "pass"),
    (By.NAME, "pass"),
    (By.CSS_SELECTOR, "input#pass"),
    (By.CSS_SELECTOR, "input[name='pass']"),
]

HOME_SCREEN_XPATHS = [
    "//input[@aria-label='Search Facebook' or @placeholder='Search Facebook']",
    "//a[@aria-label='Home']",
    "//a[contains(@href, '/home.php') or @href='/']",
    "//div[@role='feed']",
]


def _first_visible_element(wait, locators):
    for locator in locators:
        try:
            element = wait.until(EC.visibility_of_element_located(locator))
            if element:
                return element
        except TimeoutException:
            continue
    return None


def _type_text(element, text):
    element.click()
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACKSPACE)
    element.send_keys(text)


def _safe_session_filename(email):
    return re.sub(r"[\\/:*?\"<>|]", "_", email) + ".pkl"


def _is_home_screen_visible(driver):
    url = (driver.current_url or "").lower()
    if "facebook.com" not in url:
        return False
    if any(token in url for token in ("/login", "checkpoint", "two_step_verification")):
        return False
    for xpath in HOME_SCREEN_XPATHS:
        if driver.find_elements(By.XPATH, xpath):
            return True
    return False


@traced('FACEBOOK_LOGIN')
def fb_login_function(email, password):
    print("1. Checking existing sessions...")
    existing_sessions = [f for f in os.listdir(SESSION_DIR) if f.endswith(".pkl")]
    if len(existing_sessions) >= MAX_ACCOUNTS:
        return False, None, f"Not possible: Maximum {MAX_ACCOUNTS} accounts already added"

    print("2. Attempting login...")
    driver = None
    try:
        options = Options()
        options.add_argument("--window-size=1920,1080")
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

        driver = build_chrome_driver(options)
        field_wait = WebDriverWait(driver, 8)

        driver.get("https://www.facebook.com/")

        # Explicitly detect transport-level failures so we can return a useful message.
        if driver.current_url.startswith("chrome-error://") or "ERR_" in (driver.title or ""):
            return False, None, f"Network/SSL error while opening Facebook: {driver.title}"

        email_input = _first_visible_element(field_wait, LOGIN_FIELD_LOCATORS)
        password_input = _first_visible_element(field_wait, PASSWORD_FIELD_LOCATORS)
        if not email_input or not password_input:
            return False, None, (
                "Facebook login fields not found. "
                "Check internet/VPN/firewall and confirm facebook.com opens normally in Chrome."
            )

        _type_text(email_input, email)
        _type_text(password_input, password)
        password_input.send_keys(Keys.ENTER)

        print(
            f"3. Waiting for Facebook home screen (up to {MANUAL_VERIFICATION_WAIT_SECONDS} seconds) "
            "so manual verification can be completed if prompted..."
        )
        manual_wait = WebDriverWait(driver, MANUAL_VERIFICATION_WAIT_SECONDS, poll_frequency=2)
        manual_wait.until(_is_home_screen_visible)

        cookies = driver.get_cookies()
        if not cookies:
            return False, None, "Login Failed: no session cookies captured."

        filename = os.path.join(SESSION_DIR, _safe_session_filename(email))
        with open(filename, "wb") as f:
            pickle.dump(cookies, f)

        account_name = (driver.title.split("|")[0].strip() if driver.title else "Facebook Account")
        if not account_name:
            account_name = "Facebook Account"
        return True, account_name, f"Login Successful: {account_name}"

    except TimeoutException:
        return (
            False,
            None,
            "Login Failed: Facebook home screen was not visible before timeout. "
            f"Waited {MANUAL_VERIFICATION_WAIT_SECONDS} seconds for manual verification/checkpoint steps.",
        )
    except Exception as e:
        return False, None, f"Login Failed: {str(e)}"
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
