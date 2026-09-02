import hashlib
import os
import random
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from automation.browser_settings import apply_background_mode_option
from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import screenshots_dir


@dataclass(frozen=True)
class NormalizedUrl:
    original: str
    normalized: str


MIN_IMAGE_BYTES = 80_000
MAX_IMAGES_DOWNLOAD = 25


def normalize_http_url(raw_url: str) -> NormalizedUrl:
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError("URL is required")

    parsed = urlparse(value)
    if not parsed.scheme:
        value = "https://" + value
        parsed = urlparse(value)

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are supported")

    if not parsed.netloc:
        raise ValueError("Invalid URL (missing host)")

    normalized = urlunparse(parsed._replace(scheme=scheme))
    return NormalizedUrl(original=str(raw_url), normalized=normalized)


def _apply_browser_safety_patches(driver):
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
    except Exception:
        pass


def _normalize_label(text: str) -> str:
    value = str(text or "").strip().lower()
    value = " ".join(value.split())
    return value


def _digits_only(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def _guess_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ext
    return ".jpg"


def _upgrade_carpages_image_url(url: str) -> str:
    """
    The provided HTML shows URLs like:
      https://images.carpages.ca/inventory/... ?w=160&h=120&q=80&s=...
    Upgrade to higher quality by requesting a large size and max quality.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""

    parsed = urllib.parse.urlparse(raw)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if "w" in query:
        query["w"] = "2000"
    if "h" in query:
        query["h"] = "1500"
    if "q" in query:
        query["q"] = "100"
    rebuilt = parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
    return urllib.parse.urlunparse(rebuilt)


def download_images(image_urls, *, referer_url: str, max_workers: int = 6) -> list[str]:
    folder = Path(screenshots_dir())
    seen: set[str] = set()

    candidates: list[str] = []
    for raw in image_urls:
        url = _upgrade_carpages_image_url(raw)
        url = str(url or "").strip()
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append(url)
        if len(candidates) >= MAX_IMAGES_DOWNLOAD:
            break

    batch_ts = int(time.time())

    def _download_one(task_index: int, img_url: str) -> str | None:
        digest = hashlib.sha256(img_url.encode("utf-8", errors="ignore")).hexdigest()[:12]
        ext = _guess_ext_from_url(img_url)
        filename = f"website_post2_{batch_ts}_{task_index:02d}_{digest}{ext}"
        path = folder / filename

        req = urllib.request.Request(
            img_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/116.0.5845.111 Safari/537.36"
                ),
                "Referer": referer_url,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                content = resp.read()
            if not content or len(content) < MIN_IMAGE_BYTES:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                return None
            path.write_bytes(content)
            return filename
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    saved: list[str] = []
    if not candidates:
        return saved

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_download_one, idx, url): url
            for idx, url in enumerate(candidates, start=1)
        }
        for fut in as_completed(futures):
            try:
                filename = fut.result()
            except Exception:
                filename = None
            if filename:
                saved.append(filename)

    saved.sort()
    return saved


def _safe_click(driver, el) -> bool:
    try:
        el.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False


def scrape_autorama_listing(url: str, *, wait_seconds: int = 0) -> tuple[dict, list[str], str]:
    """
    Autorama listing scraper:
    - Downloads all gallery images from `.inventory-gallery-nav-slider img.img-fluid` (upgraded to HQ).
    - Extracts year/make/model from `h1.vdp-title` spans.
    - Extracts mileage from `p.m-0.p-0.large` containing span 'Mileage'.
    - Clicks 'Tech Specs' tab and extracts Exterior/Interior/Body Style/Fuel Type/Transmission.
    - Optional `wait_seconds` at the end (defaults to 0 for speed).
    """
    target = normalize_http_url(url).normalized

    options = Options()
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    apply_background_mode_option(options)
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("detach", True)
    options.add_experimental_option(
        "prefs",
        {
            "profile.managed_default_content_settings.images": 2
        },
    )
    try:
        options.page_load_strategy = "eager"
    except Exception:
        pass
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/116.0.5845.111 Safari/537.36"
    )

    driver = None
    try:
        driver = build_chrome_driver(options)
        try:
            driver.set_page_load_timeout(25)
        except Exception:
            pass
        _apply_browser_safety_patches(driver)
        try:
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        except Exception:
            pass

        wait = WebDriverWait(driver, 18)
        driver.get(target)

        # -----------------------
        # 1) Collect image URLs
        # -----------------------
        image_urls: list[str] = []
        try:
            slider = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.inventory-gallery-nav-slider"))
            )
            imgs = slider.find_elements(By.CSS_SELECTOR, "img.img-fluid")
            if not imgs:
                imgs = driver.find_elements(By.CSS_SELECTOR, "div.inventory-gallery-nav-slider img")
            for img in imgs:
                try:
                    src = str(img.get_attribute("src") or "").strip()
                    if src.startswith("http://") or src.startswith("https://"):
                        image_urls.append(src)
                except Exception:
                    continue
        except Exception:
            # fallback: any inventory images
            try:
                for img in driver.find_elements(By.CSS_SELECTOR, "img.img-fluid"):
                    src = str(img.get_attribute("src") or "").strip()
                    if src.startswith("http://") or src.startswith("https://"):
                        image_urls.append(src)
            except Exception:
                pass

        downloaded = download_images(image_urls, referer_url=target)

        # -----------------------
        # 2) Extract year/make/model
        # -----------------------
        extracted: dict[str, str] = {}
        try:
            title_h1 = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h1.vdp-title")))
            try:
                year_el = title_h1.find_element(By.CSS_SELECTOR, "span[itemprop='releaseDate']")
                extracted["year"] = str(year_el.text or "").strip()
            except Exception:
                extracted["year"] = ""
            try:
                make_el = title_h1.find_element(By.CSS_SELECTOR, "span[itemprop='brand']")
                extracted["make"] = str(make_el.text or "").strip()
            except Exception:
                extracted["make"] = ""
            try:
                model_el = title_h1.find_element(By.CSS_SELECTOR, "span[itemprop='model']")
                extracted["model"] = str(model_el.text or "").strip()
            except Exception:
                extracted["model"] = ""
        except Exception:
            extracted.setdefault("year", "")
            extracted.setdefault("make", "")
            extracted.setdefault("model", "")

        # -----------------------
        # 3) Mileage (before Tech Specs)
        # -----------------------
        try:
            mileage_p = wait.until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//p[contains(@class,'large') and .//span[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'mileage')]]",
                    )
                )
            )
            extracted["mileage"] = _digits_only(mileage_p.text)
        except Exception:
            extracted.setdefault("mileage", "")

        # -----------------------
        # 4) Click Tech Specs tab
        # -----------------------
        try:
            tech_specs = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//a[@id='specs-tab' or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'tech specs')]",
                    )
                )
            )
            _safe_click(driver, tech_specs)
            try:
                wait.until(
                    lambda d: (tech_specs.get_attribute("aria-selected") or "").lower() == "true"
                    or "active" in (tech_specs.get_attribute("class") or "").lower()
                )
            except Exception:
                pass
        except Exception:
            pass

        # -----------------------
        # 5) Extract specs from dl rows
        # -----------------------
        specs_map: dict[str, str] = {}
        try:
            stats = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.col.stats-card")))
            rows = stats.find_elements(By.CSS_SELECTOR, "dl.row")
            for row in rows:
                try:
                    dt = row.find_element(By.CSS_SELECTOR, "dt")
                    dd = row.find_element(By.CSS_SELECTOR, "dd")
                    key = _normalize_label(dt.text)
                    val = ""
                    try:
                        li = dd.find_element(By.CSS_SELECTOR, "li")
                        val = str(li.text or "").strip()
                    except Exception:
                        val = str(dd.text or "").strip()
                    if key and val and key not in specs_map:
                        specs_map[key] = val
                except Exception:
                    continue
        except TimeoutException:
            pass
        except Exception:
            pass

        # Fill required fields (flexible, missing -> empty)
        extracted.setdefault("exterior_color", specs_map.get("exterior colour", "") or specs_map.get("exterior color", "") or "")
        extracted.setdefault("interior_color", specs_map.get("interior colour", "") or specs_map.get("interior color", "") or "")
        extracted.setdefault("body_style", specs_map.get("body style", "") or "")
        extracted.setdefault("fuel_type", specs_map.get("fuel type", "") or "")
        extracted.setdefault("transmission", specs_map.get("transmission", "") or "")

        if wait_seconds and int(wait_seconds) > 0:
            time.sleep(int(wait_seconds))
        return extracted, downloaded, target
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
