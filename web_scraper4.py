import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
import threading
import random
import shutil
import stat
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains

from automation.browser_settings import is_run_in_background_enabled
from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import data_dir, screenshots_dir
from automation.stealth import apply_stealth_patches


CDP_PORT = 9222
CDP_ADDRESS = f"127.0.0.1:{CDP_PORT}"
CHROME_PROFILE_DIR = Path(data_dir("carsforsale_chrome_profile"))
CHROME_COOKIE_FILE = Path(data_dir()) / "carsforsale_chrome_cookies.json"
HISTORY_DIR = Path(data_dir("carsforsale_history"))
INVENTORY_URL = "https://www.myaccountcenter.com/inventory?type=Vehicle"
_carsforsale_process = None
_carsforsale_browser_lock = threading.Lock()
_login_status_lock = threading.Lock()
def _has_saved_cookies():
    try:
        payload = json.loads(CHROME_COOKIE_FILE.read_text(encoding="utf-8"))
        return isinstance(payload, list) and bool(payload)
    except Exception:
        return False


_login_status = {
    "running": False,
    "ready": _has_saved_cookies(),
    "manual_verification": False,
    "message": "",
}


def _find_chrome_executable():
    candidates = (
        os.environ.get("PROGRAMFILES", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "chrome.exe"


def _cdp_running():
    try:
        with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _open_dedicated_chrome(target, force_visible=False):
    """Open/reuse the isolated real Chrome instance on CDP port 9222."""
    global _carsforsale_process
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if _cdp_running():
        process = _carsforsale_process
        return process, str(CHROME_PROFILE_DIR), CDP_PORT
    chrome_args = [
            _find_chrome_executable(),
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={CHROME_PROFILE_DIR}",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-notifications",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--log-level=3",
            target,
        ]
    if is_run_in_background_enabled() and not force_visible:
        chrome_args.insert(1, "--headless=new")
    process = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=0.5):
                _carsforsale_process = process
                return process, str(CHROME_PROFILE_DIR), CDP_PORT
        except OSError:
            time.sleep(0.2)
    try:
        process.terminate()
    except Exception:
        pass
    raise RuntimeError("Dedicated Chrome did not start its debugging port")


def _save_cookies(driver):
    try:
        CHROME_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Older builds accidentally created this .json path as a directory.
        # Remove only that known bad marker directory before writing the file.
        if CHROME_COOKIE_FILE.is_dir():
            shutil.rmtree(CHROME_COOKIE_FILE, onerror=_rmtree_onerror)
        CHROME_COOKIE_FILE.write_text(json.dumps(driver.get_cookies(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[WebsitePosting4] Saved {len(driver.get_cookies())} CarsForSale cookies", flush=True)
    except Exception as exc:
        print(f"[WebsitePosting4] Cookie save warning: {exc}", flush=True)


def carsforsale_login_status():
    with _login_status_lock:
        result = dict(_login_status)
    result["ready"] = bool(result.get("ready") and _has_saved_cookies())
    return result


def _set_login_status(*, running=False, ready=False, manual_verification=False, message=""):
    global _login_status
    with _login_status_lock:
        _login_status = {
            "running": bool(running),
            "ready": bool(ready),
            "manual_verification": bool(manual_verification),
            "message": str(message or ""),
        }


def _is_challenge_page(driver):
    try:
        title = str(driver.title or "").casefold()
        url = str(driver.current_url or "").casefold()
        source = str(driver.page_source or "").casefold()
    except Exception:
        return False
    markers = (
        "just a moment", "verify you are human", "checking your browser",
        "challenge-platform", "cf-chl-", "captcha", "security verification",
    )
    combined = f"{title}\n{url}\n{source[:250000]}"
    return any(marker in combined for marker in markers)


def _detach_driver(driver):
    """Disconnect Selenium while deliberately leaving the CDP Chrome open."""
    if not driver:
        return
    try:
        driver.service.stop()
    except Exception:
        pass


def _make_writable(path):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def _remove_file_with_retries(path, attempts=12):
    target = Path(path)
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target, onerror=_rmtree_onerror)
        return
    last_error = None
    for attempt in range(max(1, int(attempts))):
        try:
            _make_writable(target)
            target.unlink(missing_ok=True)
            return
        except (PermissionError, OSError) as exc:
            last_error = exc
            time.sleep(min(1.5, 0.15 + attempt * 0.12))
    raise last_error or PermissionError(f"Could not delete {target}")


def _rmtree_onerror(function, path, _exc_info):
    _make_writable(path)
    function(path)


def delete_carsforsale_session():
    """Delete the isolated CarsForSale login profile and cookie marker."""
    global _carsforsale_process, _login_status
    with _login_status_lock:
        if _login_status.get("running"):
            raise RuntimeError("CarsForSale login setup is still running.")
    with _carsforsale_browser_lock:
        process = _carsforsale_process
        if _cdp_running():
            attached = None
            try:
                options = Options()
                options.add_experimental_option("debuggerAddress", CDP_ADDRESS)
                attached = build_chrome_driver(options)
                attached.quit()
                _human_pause(0.7, 1.3)
            except Exception:
                pass
        if process:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try: process.kill()
                except Exception: pass
            _carsforsale_process = None
        deadline = time.time() + 10
        while _cdp_running() and time.time() < deadline:
            time.sleep(0.25)
        # On some Windows VPS installations security software denies unlink()
        # on this marker even after Chrome exits. Invalidate it in place instead;
        # an empty list is explicitly treated as no saved session.
        if CHROME_COOKIE_FILE.is_dir():
            shutil.rmtree(CHROME_COOKIE_FILE, onerror=_rmtree_onerror)
        CHROME_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHROME_COOKIE_FILE.write_text("[]", encoding="utf-8")
        if CHROME_PROFILE_DIR.exists():
            try:
                shutil.rmtree(CHROME_PROFILE_DIR, onerror=_rmtree_onerror)
            except TypeError:
                # Python 3.13+ also supports onexc; retain compatibility with
                # the older runtimes used by existing packaged builds.
                shutil.rmtree(CHROME_PROFILE_DIR, onexc=lambda fn, path, exc: _rmtree_onerror(fn, path, exc))
        CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with _login_status_lock:
        _login_status = {"running": False, "ready": False, "manual_verification": False, "message": "CarsForSale saved session deleted."}
    return True


def _human_pause(minimum=0.35, maximum=1.1):
    time.sleep(random.uniform(float(minimum), float(maximum)))


def _human_scroll_to(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block:'center', behavior:'smooth'});", element)
    _human_pause(0.45, 1.25)


def _human_click(driver, element):
    _human_scroll_to(driver, element)
    try:
        ActionChains(driver).move_to_element(element).pause(random.uniform(0.15, 0.45)).click().perform()
    except Exception:
        try: element.click()
        except Exception: driver.execute_script("arguments[0].click();", element)
    _human_pause(0.45, 1.2)


def _desktop_cursor_gesture_before_cookie_save():
    """Briefly move the real cursor after login succeeds, never while credentials are entered."""
    try:
        import pyautogui

        pyautogui.FAILSAFE = True
        width, height = pyautogui.size()
        current_x, current_y = pyautogui.position()
        for _ in range(random.randint(2, 4)):
            target_x = max(20, min(width - 20, current_x + random.randint(-90, 90)))
            target_y = max(20, min(height - 20, current_y + random.randint(-55, 55)))
            pyautogui.moveTo(
                target_x,
                target_y,
                duration=random.uniform(0.25, 0.65),
                tween=pyautogui.easeInOutQuad,
            )
            current_x, current_y = target_x, target_y
            _human_pause(0.12, 0.35)
    except Exception as exc:
        print(f"[WebsitePosting4] Desktop cursor gesture skipped: {exc}", flush=True)


def setup_carsforsale_session(timeout_seconds=3600):
    """Open the isolated CarsForSale profile and allow up to one hour for login."""
    global _login_status
    with _login_status_lock:
        if _login_status.get("running"):
            return False
        _login_status = {"running": True, "ready": False, "manual_verification": False, "message": "Waiting for CarsForSale login..."}
    driver = None
    process = None
    try:
        with _carsforsale_browser_lock:
            process, _profile, port = _open_dedicated_chrome(INVENTORY_URL, force_visible=True)
            options = Options()
            options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
            driver = build_chrome_driver(options)
            apply_stealth_patches(driver)
            _human_pause(1.0, 2.2)
            deadline = time.time() + max(60, int(timeout_seconds))
            while time.time() < deadline:
                try:
                    if _is_challenge_page(driver):
                        _set_login_status(
                            running=True, ready=False, manual_verification=True,
                            message="Manual verification required in the open CarsForSale Chrome window.",
                        )
                    if driver.find_elements(By.CSS_SELECTOR, ".vehicle-card a[href*='/Inventory/VehicleDetails/']"):
                        _desktop_cursor_gesture_before_cookie_save()
                        _save_cookies(driver)
                        _set_login_status(
                            running=False, ready=True, manual_verification=False,
                            message="CarsForSale login saved. Keep this Chrome window open.",
                        )
                        return True
                except Exception:
                    pass
                _human_pause(1.6, 2.8)
            _set_login_status(running=False, ready=False, message="CarsForSale login was not completed within 60 minutes.")
            return False
    except Exception as exc:
        _set_login_status(running=False, ready=False, message=f"CarsForSale login setup failed: {exc}")
        return False
    finally:
        if driver:
            _detach_driver(driver)


def _account_key(account):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(account or "account")).strip("._") or "account"


def history_path(account):
    return HISTORY_DIR / f"{_account_key(account)}.json"


def load_history(account):
    try:
        payload = json.loads(history_path(account).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"links": []}
    except Exception:
        return {"account": str(account or ""), "links": [], "listings": []}


def mark_history_status(account, link, status):
    if not link:
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    payload = load_history(account)
    posted = str(status or "").lower() == "posted"
    links = [x for x in payload.get("links", []) if x != link]
    listings = [x for x in payload.get("listings", []) if isinstance(x, dict) and x.get("url") != link and x.get("status") == "posted"]
    if posted:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        links.append(link)
        listings.append({"url": link, "status": "posted", "posted_at": now})
    payload.update({"account": str(account or ""), "links": list(dict.fromkeys(links)), "listings": listings})
    temp = history_path(account).with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, history_path(account))


def normalize_http_url(raw_url: str):
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError("URL is required")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid URL")
    return urlunparse(parsed._replace(scheme=parsed.scheme.lower()))


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _image_url(img):
    for attr in ("data-src", "data-original", "data-lazy", "src"):
        value = str(img.get_attribute(attr) or "").strip()
        if value.startswith(("http://", "https://")) and "car.gif" not in value:
            return value
    return ""


def _download_images(urls, referer):
    folder = Path(screenshots_dir())
    folder.mkdir(parents=True, exist_ok=True)
    unique = list(dict.fromkeys(str(u).strip() for u in urls if str(u).strip()))
    stamp = int(time.time())

    def download(index_url):
        index, url = index_url
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        name = f"website_post4_{stamp}_{index:03d}_{hashlib.sha256(url.encode()).hexdigest()[:12]}{ext}"
        path = folder / name
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 Chrome/122 Safari/537.36",
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            })
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
            if len(content) < 1000:
                return None
            path.write_bytes(content)
            return name
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result() for future in as_completed(pool.submit(download, pair) for pair in enumerate(unique, 1))]
    return sorted(name for name in results if name)


def _infer_body_style(trim):
    text = _clean(trim).lower()
    for label in ("pickup truck", "suv", "crossover", "sedan", "coupe", "convertible", "hatchback", "wagon", "van", "minivan", "truck"):
        if label in text:
            return label.title()
    return _clean(trim)


def _parse_title(title, trim):
    parts = _clean(title).split()
    year = parts[0] if parts and re.fullmatch(r"\d{4}", parts[0]) else ""
    make = parts[1] if len(parts) > 1 else ""
    model_parts = parts[2:]
    trim_words = _clean(trim).split()
    if trim_words and len(model_parts) >= len(trim_words) and model_parts[-len(trim_words):] == trim_words:
        model_parts = model_parts[:-len(trim_words)]
    return year, make, " ".join(model_parts) or (parts[2] if len(parts) > 2 else "")


def scrape_carsforsale_listing(url: str):
    target = normalize_http_url(url)
    options = Options()
    # Attach Selenium to an isolated, visible Chrome process. Its temporary
    # profile and dynamically selected port cannot collide with the user's
    # normal Chrome windows or the other automation workflows.
    driver = None
    chrome_process = None
    chrome_profile = None
    try:
        # Start blank so stealth patches are installed before the target site
        # creates its first document.
        chrome_process, chrome_profile, debug_port = _open_dedicated_chrome("about:blank")
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")
        driver = build_chrome_driver(options)
        driver.set_page_load_timeout(30)
        apply_stealth_patches(driver)
        # Reuse the verified CDP browser but navigate to the requested listing.
        driver.get(target)
        # The user completes any visible verification challenge manually.
        # A loaded vehicle title is the signal that verification succeeded.
        info_wait = WebDriverWait(driver, 3000)
        title_el = info_wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".title__left h1"))
        )
        print("[WebsitePosting4] Vehicle title found; continuing extraction")
        detail_wait = WebDriverWait(driver, 15)
        title = _clean(title_el.text)
        trim = ""
        try:
            trim = _clean(detail_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".title__trim"))
            ).text)
        except Exception:
            pass
        year, make, model = _parse_title(title, trim)
        mileage = ""
        try:
            mileage = _clean(detail_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".title__mileage .mileage"))
            ).text)
        except Exception:
            pass

        values = {}
        try:
            stat_rows = detail_wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#stats li"))
            )
        except Exception:
            stat_rows = []
        for row in stat_rows:
            try:
                label = _clean(row.find_element(By.CSS_SELECTOR, ".stats-title").text).lower()
                value = _clean(row.find_element(By.CSS_SELECTOR, ".stats-value").text)
                if label and value:
                    values[label] = value
            except Exception:
                continue

        image_urls = []
        try:
            image_elements = detail_wait.until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, ".photo__carousel img, .photo.vehicle-img img")
                )
            )
        except Exception:
            image_elements = []
        for img in image_elements:
            image = _image_url(img)
            if image:
                image_urls.append(image)
        downloaded = _download_images(image_urls, target)
        info = {
            "vehicle_type": _infer_body_style(trim),
            "year": year,
            "make": make,
            "model": model,
            "body_style": _infer_body_style(trim),
            "vehicle_condition": values.get("condition", ""),
            "mileage": mileage or re.sub(r"[^0-9]", "", values.get("mileage", "")),
            "interior_color": values.get("interior color", ""),
            "exterior_color": values.get("exterior color", ""),
            "fuel_type": values.get("fuel", ""),
            "transmission": "Manual",
        }
        _save_cookies(driver)
        return info, downloaded, target
    finally:
        if driver:
            try:
                _save_cookies(driver)
            except Exception:
                pass
            try:
                driver.quit()
            except Exception:
                pass
        if chrome_process:
            try:
                chrome_process.terminate()
                chrome_process.wait(timeout=5)
            except Exception:
                try:
                    chrome_process.kill()
                except Exception:
                    pass


def _digits(value):
    return re.sub(r"[^0-9]", "", str(value or ""))


def _label_value(container, label):
    wanted = _clean(label).casefold()
    for block in container.find_elements(By.CSS_SELECTOR, ".grow-shrink-1, .vehicle-detail-item"):
        try:
            label_el = block.find_element(By.CSS_SELECTOR, ".detail-label")
            if _clean(label_el.text).rstrip(":").casefold() == wanted:
                return _clean(block.find_element(By.CSS_SELECTOR, ".detail-value").text)
        except Exception:
            continue
    return ""


BODY_STYLES = (
    ("hatchback", "Hatchback"), ("sport utility", "SUV"), ("suv", "SUV"),
    ("minicar", "Minicar"), ("minivan", "Minivan"), ("pickup", "Pickup truck"),
    ("truck", "Pickup truck"), ("convertible", "Convertible"), ("coupe", "Coupe"),
    ("sedan", "Sedan"), ("wagon", "Wagon"), ("van", "Van"),
    ("crossover", "SUV"),
)


def _body_style_from_text(value):
    text = _clean(value).casefold()
    for keyword, result in BODY_STYLES:
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            return result
    return "Other"


def _inventory_candidates(driver, seen):
    if _is_challenge_page(driver):
        raise RuntimeError("CarsForSale manual verification required in the open Chrome tab.")
    WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".vehicle-card")))
    candidates = []
    for card in driver.find_elements(By.CSS_SELECTOR, ".vehicle-card"):
        try:
            anchors = card.find_elements(By.CSS_SELECTOR, "a[href*='/Inventory/VehicleDetails/']")
            href = next((a.get_attribute("href") for a in anchors if a.get_attribute("href")), "")
            if not href or href in seen:
                continue
            title = _clean(card.find_element(By.CSS_SELECTOR, ".card-title").text)
            subtitle = _clean(card.find_element(By.CSS_SELECTOR, ".card-subtitle").text)
            year, make, model = _parse_title(title, "")
            price = _digits(_clean(card.find_element(By.CSS_SELECTOR, ".list-pricing-row .value.all-in, .list-pricing-row [class*='price'] .value").text))
            mileage = _digits(_clean(card.find_element(By.CSS_SELECTOR, ".list-pricing-row .mileage").text))
            transmission = _label_value(card, "Transmission")
            candidates.append({
                "source_url": href, "title": title, "year": year, "make": make, "model": model,
                "price": price, "mileage": mileage,
                "transmission": "Manual" if "manual" in transmission.casefold() else "Automatic transmission",
                "body_style": _body_style_from_text(f"{subtitle} {card.text} {href}"),
            })
        except Exception as exc:
            print(f"[CarsForSaleDaily] Inventory card skipped: {exc}", flush=True)
    return candidates


def _collect_inventory_candidates(driver, seen, target_count):
    collected = {}
    for _page in range(50):
        for _ in range(30):
            for item in _inventory_candidates(driver, seen):
                collected.setdefault(item["source_url"], item)
            if len(collected) >= target_count:
                return list(collected.values())
            before = driver.execute_script("return document.body.scrollHeight")
            viewport = driver.execute_script("return window.innerHeight || 800") or 800
            step = random.randint(max(250, int(viewport * .45)), max(400, int(viewport * .85)))
            driver.execute_script("window.scrollBy({top: arguments[0], behavior:'smooth'});", step)
            _human_pause(0.7, 1.6)
            after = driver.execute_script("return document.body.scrollHeight")
            at_bottom = driver.execute_script(
                "return (window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 12)"
            )
            if at_bottom and after <= before:
                for item in _inventory_candidates(driver, seen):
                    collected.setdefault(item["source_url"], item)
                break
        next_button = None
        for selector in ("a[rel='next']", ".pagination .page-next a", "button[aria-label='Next']"):
            try:
                next_button = next((x for x in driver.find_elements(By.CSS_SELECTOR, selector) if x.is_displayed() and x.is_enabled()), None)
            except Exception:
                next_button = None
            if next_button: break
        if not next_button:
            try:
                next_button = next((x for x in driver.find_elements(By.XPATH, "//*[self::a or self::button][normalize-space()='Next']") if x.is_displayed() and x.is_enabled()), None)
            except Exception:
                next_button = None
        if not next_button:
            break
        _human_click(driver, next_button)
        _human_pause(1.2, 2.6)
    return list(collected.values())


def _detail_value(driver, label):
    wanted = _clean(label).casefold()
    for block in driver.find_elements(By.CSS_SELECTOR, "#vehicleDetails .grow-shrink-1"):
        try:
            if _clean(block.find_element(By.CSS_SELECTOR, ".detail-label").text).casefold() == wanted:
                return _clean(block.find_element(By.CSS_SELECTOR, ".detail-value").text)
        except Exception:
            continue
    return ""


def _download_detail_images(driver, listing_url):
    try:
        trigger = WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH,
            "//*[normalize-space()='Photo Gallery & Overlay']"
            " | //*[@id='photoGallery']//*[normalize-space()='Photo Gallery']"
            " | //*[@id='photoGallery']//button[contains(normalize-space(.), 'Photo Gallery')]")))
        _human_click(driver, trigger)
    except Exception:
        pass
    time.sleep(random.uniform(5.0, 6.5))
    images = []
    for img in driver.find_elements(By.CSS_SELECTOR, "#photoGallery .ai-vdp-tile img, #photoGallery .image-item img"):
        url = _image_url(img)
        if url and url not in images:
            images.append(url)
        if len(images) >= 20:
            break
    return _download_images(images[:20], listing_url)


def _enrich_candidate(driver, candidate):
    driver.get(candidate["source_url"])
    _human_pause(1.1, 2.4)
    if _is_challenge_page(driver):
        raise RuntimeError("CarsForSale manual verification required in the open Chrome tab.")
    WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#vehicleDetails, #vehicleEngagements")))
    images = _download_detail_images(driver, candidate["source_url"])
    try:
        description_el = driver.find_element(By.CSS_SELECTOR, "#description_Values .accordion-body .col-12.col-md-9, #description_Values .accordion-body")
        description = _clean(description_el.get_attribute("innerText") or description_el.text)
    except Exception:
        description = ""
    payload = dict(candidate)
    payload.update({
        "vehicle_type": "Car", "vehicle_condition": "Very good", "source_type": "carsforsale",
        "fuel_type": _detail_value(driver, "Fuel Type") or "Other",
        "exterior_color": _detail_value(driver, "Exterior Color") or "Other",
        "interior_color": _detail_value(driver, "Interior Color") or "Other",
        "description": description, "description_optional": True, "location": "",
    })
    for index, image in enumerate(images[:20], 1):
        payload[f"image_path{index}"] = str(Path(screenshots_dir()) / image)
    missing = [key for key in ("year", "make", "model", "price", "mileage") if not payload.get(key)]
    if missing:
        raise ValueError("Missing listing field(s): " + ", ".join(missing))
    if not images:
        raise ValueError("No vehicle images could be downloaded")
    return payload


def collect_carsforsale_listings(account, limit=4):
    """Collect unposted account-center inventory using the saved CarsForSale profile."""
    if not _has_saved_cookies():
        raise RuntimeError("Set up CarsForSale Login before starting automation.")
    with _carsforsale_browser_lock:
        driver = None
        process = None
        extraction_handle = None
        original_handle = None
        manual_verification = False
        try:
            process, _profile, port = _open_dedicated_chrome(INVENTORY_URL, force_visible=True)
            options = Options()
            options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
            driver = build_chrome_driver(options)
            apply_stealth_patches(driver)
            original_handle = driver.current_window_handle
            existing_handles = set(driver.window_handles)
            driver.execute_script("window.open(arguments[0], '_blank');", INVENTORY_URL)
            WebDriverWait(driver, 15).until(lambda current: len(current.window_handles) > len(existing_handles))
            extraction_handle = next(handle for handle in driver.window_handles if handle not in existing_handles)
            driver.switch_to.window(extraction_handle)
            _human_pause(1.2, 2.8)
            if _is_challenge_page(driver):
                manual_verification = True
                _set_login_status(
                    running=False, ready=False, manual_verification=True,
                    message="Manual verification required. Complete it in the open CarsForSale Chrome tab, then start again.",
                )
                raise RuntimeError("CarsForSale manual verification required in the open Chrome tab.")
            seen = set(load_history(account).get("links") or [])
            candidates = _collect_inventory_candidates(driver, seen, max(1, int(limit or 4)))
            results = []
            for candidate in candidates:
                try:
                    results.append(_enrich_candidate(driver, candidate))
                    if len(results) >= max(1, int(limit or 4)):
                        break
                except Exception as exc:
                    print(f"[CarsForSaleDaily] Listing failed {candidate.get('source_url')}: {exc}", flush=True)
                    if _is_challenge_page(driver) or "manual verification required" in str(exc).casefold():
                        manual_verification = True
                        raise
                    mark_history_status(account, candidate.get("source_url"), "failed")
            _save_cookies(driver)
            _set_login_status(
                running=False, ready=True, manual_verification=False,
                message="CarsForSale session ready. Persistent Chrome remains open.",
            )
            return results
        finally:
            if driver:
                try:
                    manual_verification = bool(manual_verification or _is_challenge_page(driver))
                except Exception:
                    pass
                if manual_verification:
                    _set_login_status(
                        running=False, ready=False, manual_verification=True,
                        message="Manual verification required. Complete it in the open CarsForSale Chrome tab, then start again.",
                    )
                if extraction_handle and not manual_verification:
                    try:
                        if extraction_handle in driver.window_handles:
                            driver.switch_to.window(extraction_handle)
                            driver.close()
                        if original_handle in driver.window_handles:
                            driver.switch_to.window(original_handle)
                    except Exception:
                        pass
                _detach_driver(driver)
