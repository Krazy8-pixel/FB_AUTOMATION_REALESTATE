"""Best-effort local video uploads for Facebook listing composers."""
import os

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


def upload_optional_videos(driver, paths, log=print):
    """Only target video-capable inputs; failure must not abort posting."""
    uploaded = 0
    for path in list(paths or [])[:5]:
        removal_count = None
        try:
            path = os.path.abspath(str(path))
            if not os.path.isfile(path):
                raise ValueError("video file is missing")

            def video_input(current):
                for element in current.find_elements(By.CSS_SELECTOR, "input[type='file']:not([disabled])"):
                    accept = (element.get_attribute("accept") or "").lower()
                    if element.is_enabled() and any(token in accept for token in ("video", ".mp4", ".mov", ".webm", ".m4v")):
                        return element
                return False

            element = video_input(driver)
            if not element:
                for button in driver.find_elements(By.XPATH, "//*[@role='button' or self::button][normalize-space(.)='Add video' or @aria-label='Add video']"):
                    if button.is_displayed() and button.is_enabled():
                        button.click()
                        break
                element = WebDriverWait(driver, 5).until(video_input)
            removal_count = len(driver.find_elements(By.CSS_SELECTOR, "[aria-label='Remove video'], [aria-label='Delete video']"))
            element.send_keys(path)

            # Allow bounded processing time. Immediate browser/DOM errors and
            # explicit rejection messages are handled without aborting the post.
            def settled(current):
                for alert in current.find_elements(By.CSS_SELECTOR, "[role='alert']"):
                    text = (alert.text or "").lower()
                    if alert.is_displayed() and "video" in text and any(word in text for word in ("failed", "unsupported", "cannot", "couldn't", "too large", "error")):
                        raise ValueError(text)
                return not any(
                    item.is_displayed()
                    for item in current.find_elements(By.CSS_SELECTOR, "[role='progressbar']")
                )

            WebDriverWait(driver, 30).until(settled)
            uploaded += 1
            log(f"[Video] Submitted: {os.path.basename(path)}")
        except Exception as exc:
            log(f"[Video] Skipped {os.path.basename(str(path))}: {exc}")
            # Remove a rejected video if the composer offers a video-specific
            # removal action. Never click a photo-removal control.
            try:
                if removal_count is not None:
                    buttons = driver.find_elements(By.CSS_SELECTOR, "[aria-label='Remove video'], [aria-label='Delete video']")
                    for button in buttons[removal_count:]:
                        if button.is_displayed() and button.is_enabled():
                            button.click()
            except Exception:
                pass
    return uploaded
