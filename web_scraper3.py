import hashlib
import os
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
    if value.endswith(":"):
        value = value[:-1].strip()
    return " ".join(value.split())


def _get_best_img_url(img_el) -> str:
    candidates = [
        img_el.get_attribute("src"),
        img_el.get_attribute("data-src"),
        img_el.get_attribute("data-lazy"),
        img_el.get_attribute("data-original"),
    ]
    for candidate in candidates:
        v = str(candidate or "").strip()
        if v.startswith("http://") or v.startswith("https://"):
            return v
    return ""


def _guess_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ext
    return ".jpg"


def _prefer_hdr_url(url: str) -> str:
    """
    CarsByJatin pages commonly use Autobunny S3 thumbs:
      .../thumb-xxxx.jpg
    Prefer HDR by switching to:
      .../pic-xxxx.jpg
    """
    u = str(url or "").strip()
    if not u:
        return ""
    if "thumb-" in u:
        return u.replace("thumb-", "pic-")
    return u


def download_images(image_urls, *, referer_url: str, max_workers: int = 6) -> list[str]:
    folder = Path(screenshots_dir())
    seen: set[str] = set()

    candidates: list[str] = []
    for raw in image_urls:
        url = str(raw or "").strip()
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append(url)

    batch_ts = int(time.time())

    def _download_one(task_index: int, raw_url: str) -> str | None:
        hdr_url = _prefer_hdr_url(raw_url)
        attempt_urls = [hdr_url]
        if raw_url and raw_url != hdr_url:
            attempt_urls.append(raw_url)

        for img_url in attempt_urls:
            digest = hashlib.sha256(img_url.encode("utf-8", errors="ignore")).hexdigest()[:12]
            ext = _guess_ext_from_url(img_url)
            filename = f"website_post3_{batch_ts}_{task_index:02d}_{digest}{ext}"
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
                    continue
                path.write_bytes(content)
                return filename
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
        return None

    saved: list[str] = []
    if not candidates:
        return saved

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_download_one, idx, url): url for idx, url in enumerate(candidates, start=1)
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


def scrape_carsbyjatin_listing(url: str) -> tuple[dict, list[str], str]:
    """
    CarsByJatin listing scraper:
    - Collects gallery images from `#carousel ul.slides li` (data-thumb + img src)
      and downloads HDR versions first (thumb-* -> pic-*).
    - Extracts required fields from `.VehicleInfoDetails` (label/value spans).
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
            # Speed: don't load images in the browser; we download full-res ourselves.
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

        wait = WebDriverWait(driver, 20)
        time.sleep(random.uniform(0.2, 0.8))
        driver.get(target)

        # -----------------------
        # 1) Collect image URLs
        # -----------------------
        image_urls: list[str] = []
        try:
            lis = wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div#carousel ul.slides li"))
            )
        except Exception:
            try:
                lis = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "ul.slides li")))
            except Exception:
                lis = []

        for li in lis:
            try:
                thumb = str(li.get_attribute("data-thumb") or "").strip()
                if thumb:
                    image_urls.append(thumb)
            except Exception:
                pass
            try:
                wait.until(lambda d: len(li.find_elements(By.CSS_SELECTOR, "img")) > 0)
                img = li.find_element(By.CSS_SELECTOR, "img")
                best = _get_best_img_url(img)
                if best:
                    image_urls.append(best)
            except Exception:
                pass

        downloaded = download_images(image_urls, referer_url=target)

        # -----------------------
        # 2) Extract info fields
        # -----------------------
        info_map: dict[str, str] = {}
        try:
            container = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.VehicleInfoDetails")))
            rows = wait.until(
                lambda d: container.find_elements(By.CSS_SELECTOR, ":scope > div > div")
                or container.find_elements(By.CSS_SELECTOR, "div > div")
            )

            for row in rows:
                try:
                    spans = row.find_elements(By.CSS_SELECTOR, "span")
                    if len(spans) < 2:
                        continue
                    label = _normalize_label(spans[0].text)
                    value = str(spans[1].text or "").strip()
                    if label and value and label not in info_map:
                        info_map[label] = value
                except Exception:
                    continue
        except Exception:
            pass

        extracted: dict[str, str] = {}
        for out_key, label in (
            ("make", "make"),
            ("model", "model"),
            ("year", "year"),
            ("body_style", "body style"),
            ("odometer", "odometer"),
            ("transmission", "transmission"),
            ("interior", "interior"),
            ("exterior", "exterior"),
            ("fuel_type", "fuel type"),
        ):
            extracted[out_key] = info_map.get(label, "") or ""

        return extracted, downloaded, target
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
