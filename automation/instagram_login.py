import os
import pickle
import time

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from automation.browser_settings import apply_background_mode_option
from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import data_dir


INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"
INSTAGRAM_HOME_URL = "https://www.instagram.com/"
INSTAGRAM_CREATE_URL = "https://www.instagram.com/create/select/"
INSTAGRAM_DIR = data_dir("instagram")
INSTAGRAM_COOKIE_PATH = str(INSTAGRAM_DIR / "cookies.pkl")
INSTAGRAM_ACCOUNT_PATH = str(INSTAGRAM_DIR / "account.json")
_INSTAGRAM_POST_DRIVER = None
INSTAGRAM_EVENT_DELAY = 2


def _instagram_pause(event):
    print(f"[Instagram] waiting {INSTAGRAM_EVENT_DELAY}s after {event}", flush=True)
    time.sleep(INSTAGRAM_EVENT_DELAY)


def _apply_browser_safety_patches(driver):
    """Use a visible, normal-looking browser without hiding user interaction."""
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                    Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
                    window.chrome = window.chrome || { runtime: {} };
                """,
            },
        )
    except Exception:
        pass


def _load_account():
    if not os.path.exists(INSTAGRAM_ACCOUNT_PATH):
        return None
    try:
        import json
        with open(INSTAGRAM_ACCOUNT_PATH, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def instagram_account():
    return _load_account()


def delete_instagram_account():
    global _INSTAGRAM_POST_DRIVER
    if _INSTAGRAM_POST_DRIVER is not None:
        try:
            _INSTAGRAM_POST_DRIVER.quit()
        except Exception:
            pass
        _INSTAGRAM_POST_DRIVER = None
    removed = False
    for path in (INSTAGRAM_COOKIE_PATH, INSTAGRAM_ACCOUNT_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed = True
        except Exception:
            pass
    return removed


def _visible_text_xpath(text):
    lowered = str(text or "").strip().lower()
    return (
        "//*[self::span or self::div or self::button or self::a]["
        "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        f"'abcdefghijklmnopqrstuvwxyz')={lowered!r}]"
    )


def _click_instagram_text(driver, text, timeout=30):
    """Click a visible control by its text, with semantic fallbacks."""
    base = _visible_text_xpath(text)
    locators = (
        (By.XPATH, f"{base}/ancestor-or-self::*[self::button or self::a or @role='button'][1]"),
        (By.XPATH, base),
    )
    last_error = None
    for locator in locators:
        try:
            element = WebDriverWait(driver, timeout).until(
                lambda current: next(
                    (
                        candidate
                        for candidate in current.find_elements(*locator)
                        if candidate.is_displayed()
                    ),
                    False,
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
            try:
                element.click()
            except Exception:
                driver.execute_script("arguments[0].click();", element)
            print(f"[Instagram] Clicked visible '{text}' control")
            return element
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Instagram '{text}' control was not found: {last_error}")


def _click_instagram_share(driver, scope=None, timeout=30):
    """Click the final Share control inside the active composer dialog."""
    share_xpath = (
        "//*[self::button or @role='button']["
        "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='share' or "
        ".//*[self::div or self::span][translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='share']]"
    )

    # The caption editor and final Share button live in the same dialog. This
    # prevents selecting Instagram's unrelated messaging/share control.
    roots = []
    if scope is not None:
        try:
            dialog = scope.find_element(By.XPATH, "./ancestor::*[@role='dialog'][1]")
            roots.append(dialog)
        except Exception:
            print("[Instagram] Composer dialog role not found; using page-level Share search", flush=True)
    if not roots:
        roots.append(driver)

    def _target(current):
        candidates = []
        for root in roots:
            try:
                locator_xpath = share_xpath if root is driver else "." + share_xpath
                candidates.extend(root.find_elements(By.XPATH, locator_xpath))
            except Exception:
                continue
        for candidate in reversed(candidates):
            if candidate.is_displayed() and candidate.is_enabled():
                print(
                    f"[Instagram] Share candidate: {candidate.get_attribute('outerHTML')[:400]!r}",
                    flush=True,
                )
                return candidate
        return False

    button = WebDriverWait(driver, timeout).until(_target)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
    try:
        button.click()
    except Exception:
        driver.execute_script("arguments[0].click();", button)
    print("[Instagram] Clicked visible 'share' control", flush=True)
    return button


def _find_media_input(driver, timeout=20):
    return WebDriverWait(driver, timeout).until(
        lambda current: next(
            (
                candidate
                for candidate in current.find_elements(By.CSS_SELECTOR, "input[type='file']")
                if candidate.is_enabled()
            ),
            False,
        )
    )


def _find_gallery_media_input(driver, previous_count=0, timeout=20):
    """Find the multiple-file input created after opening Instagram's gallery."""
    selector = "form[role='presentation'] input[type='file'][multiple]"

    def _latest_input(current):
        inputs = [
            candidate
            for candidate in current.find_elements(By.CSS_SELECTOR, selector)
            if candidate.is_enabled()
        ]
        if not inputs:
            return False
        # Instagram normally appends a new form after the gallery button is
        # clicked. If it reuses the existing form, the last input is still the
        # correct target for the new selection.
        if len(inputs) > previous_count or previous_count == 0:
            return inputs[-1]
        return inputs[-1]

    return WebDriverWait(driver, timeout).until(_latest_input)


def _print_gallery_state(driver, label):
    try:
        buttons = driver.find_elements(
            By.XPATH,
            "//button[.//svg[@aria-label='Open media gallery' or .//title[normalize-space()='Open media gallery']]]",
        )
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        gallery_inputs = driver.find_elements(
            By.CSS_SELECTOR,
            "form[role='presentation'] input[type='file'][multiple]",
        )
        print(
            f"[Instagram][Gallery] {label}: url={driver.current_url!r} "
            f"gallery_buttons={len(buttons)} file_inputs={len(inputs)} "
            f"multiple_gallery_inputs={len(gallery_inputs)}",
            flush=True,
        )
        for index, button in enumerate(buttons[:3], start=1):
            try:
                print(
                    f"[Instagram][Gallery] button_{index}: displayed={button.is_displayed()} "
                    f"enabled={button.is_enabled()} html={button.get_attribute('outerHTML')[:300]!r}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[Instagram][Gallery] button_{index} inspect failed: {exc!r}", flush=True)
        for index, file_input in enumerate(gallery_inputs[-3:], start=1):
            try:
                print(
                    f"[Instagram][Gallery] input_{index}: displayed={file_input.is_displayed()} "
                    f"enabled={file_input.is_enabled()} multiple={file_input.get_attribute('multiple')!r}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[Instagram][Gallery] input_{index} inspect failed: {exc!r}", flush=True)
    except Exception as exc:
        print(f"[Instagram][Gallery] state inspection failed at {label}: {exc!r}", flush=True)


def _upload_instagram_images(driver, image_paths):
    paths = [str(path) for path in (image_paths or []) if str(path or '').strip()]
    paths = [path for path in paths if os.path.isfile(path)]
    print(f"[Instagram][Gallery] received {len(paths)} valid image paths: {paths!r}", flush=True)
    if not paths:
        raise RuntimeError("No valid images were supplied for the Instagram post.")

    # 1) Upload the first image through Instagram's initial file input.
    try:
        first_input = _find_media_input(driver, timeout=15)
    except Exception:
        _click_instagram_text(driver, "Select from your computer", timeout=20)
        first_input = _find_media_input(driver, timeout=20)
    print(
        f"[Instagram][Gallery] first input found; multiple={first_input.get_attribute('multiple')!r}",
        flush=True,
    )
    first_input.send_keys(os.path.abspath(paths[0]))
    print(f"[Instagram][Gallery] uploaded image 1/{len(paths)}", flush=True)
    _instagram_pause("first image upload")

    if len(paths) == 1:
        return

    # 2) Use the stable SVG aria-label from Instagram's current markup, then
    #    click its containing button.
    gallery_svg = WebDriverWait(driver, 30).until(
        lambda current: next(
            (
                element
                for element in current.find_elements(
                    By.CSS_SELECTOR, "svg[aria-label='Open media gallery']"
                )
                if element.is_displayed()
            ),
            False,
        )
    )
    print("[Instagram][Gallery] Open media gallery SVG found", flush=True)
    gallery_button = gallery_svg.find_element(By.XPATH, "./ancestor::button[1]")
    try:
        gallery_button.click()
    except Exception:
        driver.execute_script("arguments[0].click();", gallery_button)
    print("[Instagram][Gallery] Open media gallery clicked", flush=True)
    _instagram_pause("gallery click")

    # 3) The click creates this exact multiple file input in the composer.
    gallery_input = WebDriverWait(driver, 30).until(
        lambda current: next(
            (
                element
                for element in current.find_elements(
                    By.CSS_SELECTOR,
                    "form[role='presentation'] input[type='file'][multiple]",
                )
                if element.is_enabled()
            ),
            False,
        )
    )
    print("[Instagram][Gallery] multiple gallery input found", flush=True)
    # The input has `multiple`; send the remaining paths together so the
    # browser keeps every selected file instead of replacing the prior one.
    remaining_paths = "\n".join(os.path.abspath(path) for path in paths[1:])
    gallery_input.send_keys(remaining_paths)
    for index in range(2, len(paths) + 1):
        print(f"[Instagram][Gallery] uploaded image {index}/{len(paths)}", flush=True)
    _instagram_pause("remaining image upload")


def _instagram_caption(post_data):
    data = post_data if isinstance(post_data, dict) else {}
    title = " ".join(
        str(data.get(field) or "").strip()
        for field in ("year", "make", "model")
        if str(data.get(field) or "").strip()
    ) or "Vehicle"
    mileage = str(data.get("mileage") or "").strip() or "N/A"
    price = str(data.get("price") or "").strip() or "N/A"
    description = str(data.get("description") or "").strip()
    return f"{title}\nMileage: {mileage} | Price: {price}\n{description}".rstrip()


def _publish_instagram_post(driver, post_data):
    global _INSTAGRAM_POST_DRIVER
    _click_instagram_text(driver, "Create", timeout=30)
    _instagram_pause("Create click")
    _click_instagram_text(driver, "Post", timeout=30)
    _instagram_pause("Post click")
    _upload_instagram_images(driver, (post_data or {}).get("instagram_image_paths", []))

    # Instagram displays Next twice: once after selecting media and once after
    # the edit screen. Use a fresh wait for each occurrence.
    _click_instagram_text(driver, "Next", timeout=30)
    _instagram_pause("first Next click")
    _click_instagram_text(driver, "Next", timeout=30)
    _instagram_pause("second Next click")

    caption_locators = (
        # Exact selector from Instagram's current composer markup.
        (By.CSS_SELECTOR, "div[contenteditable='true'][aria-label='Write a caption...']"),
        # Locale/accessibility fallback.
        (
            By.XPATH,
            "//*[@contenteditable='true' and "
            "contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'write a caption')]",
        ),
    )
    caption = WebDriverWait(driver, 30).until(
        lambda current: next(
            (
                candidate
                for locator in caption_locators
                for candidate in current.find_elements(*locator)
                if candidate.is_displayed() and candidate.is_enabled()
            ),
            False,
        )
    )
    caption_text = _instagram_caption(post_data)
    print("[Instagram] Caption editor found; inserting text", flush=True)
    driver.execute_script(
        """
        const editor = arguments[0];
        editor.focus();
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(editor);
        selection.removeAllRanges();
        selection.addRange(range);
        """,
        caption,
    )
    # Input.insertText is delivered as a real browser editing event and is
    # handled reliably by Instagram's Lexical contenteditable editor.
    try:
        driver.execute_cdp_cmd("Input.insertText", {"text": caption_text})
    except Exception:
        driver.execute_script(
            """
            const editor = arguments[0];
            const value = arguments[1];
            editor.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, value);
            editor.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertText', data: value
            }));
            """,
            caption,
            caption_text,
        )
    try:
        WebDriverWait(driver, 10).until(
            lambda current: bool(
                (caption.get_attribute("innerText") or caption.text or "").strip()
            )
        )
    except Exception as verify_error:
        raise RuntimeError(f"Instagram caption editor stayed empty: {verify_error}") from verify_error
    print("[Instagram] Caption entered", flush=True)
    _instagram_pause("caption entry")

    _click_instagram_share(driver, scope=caption, timeout=30)
    time.sleep(20)
    print("[Instagram] Share clicked; waited 20 seconds", flush=True)
    try:
        driver.quit()
    finally:
        if _INSTAGRAM_POST_DRIVER is driver:
            _INSTAGRAM_POST_DRIVER = None
    print("[Instagram] Browser closed after posting", flush=True)


def open_instagram_post_page(post_data=None):
    """Open Instagram with saved cookies and optionally publish a post."""
    global _INSTAGRAM_POST_DRIVER

    if not os.path.exists(INSTAGRAM_COOKIE_PATH):
        raise RuntimeError("No saved Instagram session. Add the Instagram account first.")

    # Reuse the already-open browser when a scheduled job is retried.
    if _INSTAGRAM_POST_DRIVER is not None:
        try:
            _INSTAGRAM_POST_DRIVER.get(INSTAGRAM_HOME_URL)
            WebDriverWait(_INSTAGRAM_POST_DRIVER, 30).until(_is_home)
            driver = _INSTAGRAM_POST_DRIVER
            if post_data:
                _publish_instagram_post(driver, post_data)
                # Publishing intentionally closes the browser. Do not issue
                # any more WebDriver commands against that finished session.
                print("[Instagram] Scheduled post flow completed", flush=True)
                return None
            return driver
        except Exception:
            try:
                _INSTAGRAM_POST_DRIVER.quit()
            except Exception:
                pass
        _INSTAGRAM_POST_DRIVER = None

    options = Options()
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    apply_background_mode_option(options)

    driver = build_chrome_driver(options)
    try:
        _apply_browser_safety_patches(driver)
        driver.set_page_load_timeout(45)
        # Cookies can only be added after the browser has visited the domain.
        driver.get(INSTAGRAM_HOME_URL)
        with open(INSTAGRAM_COOKIE_PATH, "rb") as handle:
            cookies = pickle.load(handle)
        for cookie in cookies:
            cookie = dict(cookie or {})
            if cookie.get("sameSite") not in (None, "Lax", "Strict", "None"):
                cookie.pop("sameSite", None)
            try:
                driver.add_cookie(cookie)
            except Exception:
                continue
        driver.refresh()
        WebDriverWait(driver, 30).until(_is_home)
        _INSTAGRAM_POST_DRIVER = driver
        if post_data:
            _publish_instagram_post(driver, post_data)
            # _publish_instagram_post closes Chrome after Share. Reaching this
            # line means the publishing routine completed without an error.
            print("[Instagram] Scheduled post flow completed", flush=True)
            return None
        print(f"[Instagram] Instagram home opened: {driver.current_url}")
        return driver
    except Exception:
        if _INSTAGRAM_POST_DRIVER is driver:
            _INSTAGRAM_POST_DRIVER = None
        try:
            driver.quit()
        except Exception:
            pass
        raise


def _save_session(email, driver):
    import json
    os.makedirs(INSTAGRAM_DIR, exist_ok=True)
    with open(INSTAGRAM_COOKIE_PATH, "wb") as handle:
        pickle.dump(driver.get_cookies(), handle)
    with open(INSTAGRAM_ACCOUNT_PATH, "w", encoding="utf-8") as handle:
        json.dump({"email": email, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, handle, indent=2)


def _is_home(driver):
    url = str(driver.current_url or "").lower()
    if "/accounts/login" in url:
        return False
    selectors = (
        'svg[aria-label="Home"]',
        'a[href="/direct/inbox/"]',
    )
    return any(driver.find_elements(By.CSS_SELECTOR, selector) for selector in selectors)


def login_instagram(email, password):
    email = str(email or "").strip()

    options = Options()
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    apply_background_mode_option(options, enabled=False)
    driver = None
    try:
        driver = build_chrome_driver(options)
        _apply_browser_safety_patches(driver)
        driver.set_page_load_timeout(45)
        driver.get(INSTAGRAM_HOME_URL)

        # Reuse the saved Instagram cookies first, if present.
        if os.path.exists(INSTAGRAM_COOKIE_PATH):
            try:
                with open(INSTAGRAM_COOKIE_PATH, "rb") as handle:
                    cookies = pickle.load(handle)
                for cookie in cookies:
                    try:
                        driver.add_cookie(cookie)
                    except Exception:
                        continue
                driver.refresh()
                WebDriverWait(driver, 20).until(_is_home)
                return True, "Saved Instagram session restored."
            except Exception:
                driver.get(INSTAGRAM_LOGIN_URL)
        else:
            driver.get(INSTAGRAM_LOGIN_URL)

        print("[Instagram] Login page opened; waiting for manual login and verification.")
        # The user completes the fields, login click, verification, 2FA, and
        # checkpoint screens in the visible browser. Home-page controls confirm
        # that the session is ready; no password is read or typed by the bot.
        WebDriverWait(driver, 6000).until(_is_home)
        _save_session(email or "Instagram account", driver)
        return True, "Instagram login completed and cookies saved."
    except Exception as exc:
        return False, f"Instagram login failed or timed out: {exc}"
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
