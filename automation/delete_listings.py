import os
import time
import base64
import requests
import random
import re,json
import shutil
import pandas as pd
from pathlib import Path
from selenium import webdriver
import pickle, os, time, random
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from PIL import Image
import re,pickle
import traceback
from urllib.parse import unquote
from automation.browser_settings import apply_background_mode_option
from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import sessions_dir
 
def _apply_browser_safety_patches(driver):
    """
    Best-effort browser normalization. Non-fatal by design.
    """
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                    Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
                """
            },
        )
    except Exception as e:
        print(f"[DeleteListings] Browser safety patch warning: {e}")

def delete_listing(account, limit, status):

    limit = int(limit)

    SESSION_DIR = sessions_dir()

    session_file = os.path.join(SESSION_DIR, account + ".pkl")
    with open(session_file, "rb") as f:
        cookies = pickle.load(f)

    logs = []
    def log(msg):
        print(msg)
        logs.append(msg)

    def robust_click(el):
        # Attempt multiple click strategies to bypass overlays/interception.
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.3)
        except Exception:
            pass
        # Try clicking the element at its center using JS and elementFromPoint.
        try:
            clicked = driver.execute_script(
                """
                const el = arguments[0];
                const rect = el.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                const topEl = document.elementFromPoint(cx, cy);
                if (topEl && (topEl === el || el.contains(topEl))) {
                    topEl.click();
                    return true;
                }
                let cur = topEl;
                while (cur) {
                    if (cur.getAttribute && cur.getAttribute('role') === 'button') {
                        cur.click();
                        return true;
                    }
                    cur = cur.parentElement;
                }
                return false;
                """,
                el
            )
            if clicked:
                return True
        except Exception:
            pass
        for _ in range(2):
            try:
                driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                pass
            try:
                driver.execute_script(
                    "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));",
                    el
                )
                return True
            except Exception:
                pass
            try:
                ActionChains(driver).move_to_element(el).pause(0.2).click().perform()
                return True
            except Exception:
                pass
            try:
                el.click()
                return True
            except Exception:
                pass
            try:
                el.send_keys(Keys.ENTER)
                return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    options = Options()
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    apply_background_mode_option(options)
    options.add_argument("--disable-notifications")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/116.0.5845.111 Safari/537.36"
    )
    prefs = {"profile.managed_default_content_settings.images": 1}
    options.add_experimental_option("prefs", prefs)

    driver = build_chrome_driver(options)
    _apply_browser_safety_patches(driver)
    wait = WebDriverWait(driver, 50)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    indicator=False
    driver.get("https://www.facebook.com/")
    time.sleep(random.uniform(2, 4))

    for cookie in cookies:
        if "sameSite" not in cookie:
            cookie["sameSite"] = "Lax"
        try:
            driver.add_cookie(cookie)
        except Exception as e:
            print(f"âš ï¸ Could not add cookie: {e}")

    driver.refresh()
    time.sleep(random.uniform(2, 5))

    driver.get("https://www.facebook.com/marketplace/you/selling")
    time.sleep(2)
    indicator=False
    status_upper = status.upper()
    if status_upper == "ACTIVE":
        real_status = "Active"
    elif status_upper == "SOLD":
        real_status = "Sold"
    elif status_upper == "PENDING":
        real_status = "Pending"
    else:
        print(f"Invalid status '{status}'")
        driver.quit()
        return indicator

    try:
        i = 0
        while i < limit:

            listings_xpath = (
                f'//span[starts-with(normalize-space(), "{real_status}") '
                f'and contains(., "Listed on")]'
            )

            listings = WebDriverWait(driver, 20).until(
                EC.presence_of_all_elements_located((By.XPATH, listings_xpath))
            )

            if not listings:
                print("No more listings found")
                break

            listing = listings[0]
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", listing
            )
            time.sleep(1)
            driver.execute_script("arguments[0].click();", listing)
            print(f"Clicked listing {i + 1}")
            time.sleep(2)

            delete_btn = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((
                    By.XPATH,
                    '//div[@role="button" and ('
                    '@aria-label="Delete marketplace listing" or '
                    '@aria-label="Delete Marketplace listing")]'
                ))
            )
            driver.execute_script("arguments[0].click();", delete_btn)
            time.sleep(1)
            print(f"Clicked delete button for listing {i + 1}")

            delete_box = WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//div[@role="dialog"]')
                )
            )

            print("Found  delete box")
            def find_confirm_btn():
                # Prefer the latest dialog's Delete button (Facebook can render multiple layers)
                candidates = []
                try:
                    candidates = driver.find_elements(
                        By.XPATH,
                        "(//div[@role='dialog']//div[@role='button' and @aria-label='Delete'])"
                    )
                except Exception:
                    candidates = []
                if candidates:
                    return candidates[-1]
                try:
                    candidates = driver.find_elements(
                        By.XPATH,
                        "(//div[@role='dialog']//div[@role='button'][.//span[normalize-space()='Delete']])"
                    )
                except Exception:
                    candidates = []
                if candidates:
                    return candidates[-1]
                try:
                    candidates = driver.find_elements(
                        By.XPATH,
                        "(//div[@role='button' and @aria-label='Delete'])"
                    )
                except Exception:
                    candidates = []
                if candidates:
                    return candidates[-1]
                return None

            confirm_delete_btn = WebDriverWait(driver, 30).until(
                lambda d: find_confirm_btn()
            )
            print("Found confirm delete button")
            # Re-find inside dialog right before click to avoid stale element
            try:
                confirm_delete_btn = find_confirm_btn()
            except Exception:
                pass
            if not robust_click(confirm_delete_btn):
                try:
                    # Last resort: click via JS selector
                    driver.execute_script(
                        """
                        const btns = document.querySelectorAll('[role="button"][aria-label="Delete"]');
                        if (btns && btns.length) { btns[btns.length - 1].click(); return true; }
                        return false;
                        """
                    )
                except Exception:
                    pass
                else:
                    time.sleep(0.5)
                if not robust_click(confirm_delete_btn):
                    raise Exception("Failed to click confirm delete button")

            log(f"Confirmed deletion for listing {i + 1}")
            indicator=True
            i += 1
            time.sleep(2)

            # âœ… CRITICAL FIX: go back to listings page
            driver.get("https://www.facebook.com/marketplace/you/selling")
            time.sleep(4)

    except Exception as e:
        log(f"Error finding or clicking listings: {e}")
        traceback.print_exc()
        

    driver.quit()
    return indicator


