import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from automation.browser_settings import apply_background_mode_option
from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import data_dir, screenshots_dir
from automation.stealth import apply_stealth_patches, configure_stealth_options


SEARCH_URL = "https://www.californiaautofinancegroup.com/searchall.aspx"
SITE_ORIGIN = "https://www.californiaautofinancegroup.com"
HISTORY_DIR = Path(data_dir("california_autofinance_history"))
PAGE_LOAD_TIMEOUT_SECONDS = 180
ELEMENT_WAIT_SECONDS = 60


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _account_key(account):
    key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(account or "account")).strip("._")
    return key or "account"


def _is_bike_url(url):
    value = unquote(str(url or "")).lower()
    normalized = re.sub(r"[-_+]+", " ", value)
    return any(
        keyword in normalized
        for keyword in ("bike", "motor bike", "motorbike", "motorcycle", "motercycle")
    )


def history_path(account):
    return HISTORY_DIR / f"{_account_key(account)}.json"


def delete_history(account):
    """Remove only this account's posted-listing URL history file."""
    path = history_path(account)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def load_history(account):
    path = history_path(account)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("history root is not an object")
        links = payload.get("links") or []
        payload["links"] = [str(link).strip() for link in links if str(link).strip()]
        return payload
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return {"account": str(account or ""), "links": [], "listings": []}


def mark_history_status(account, link, status):
    if not link:
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    payload = load_history(account)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    posted = str(status or "").strip().lower() == "posted"
    links = [item for item in payload.setdefault("links", []) if item != link]
    records = [
        item for item in payload.setdefault("listings", [])
        if isinstance(item, dict) and item.get("url") != link and item.get("status") == "posted"
    ]
    if posted:
        links.append(link)
        records.append({"url": link, "status": "posted", "posted_at": now})
    payload["links"] = list(dict.fromkeys(links))
    payload["listings"] = records
    payload["account"] = str(account or "")
    payload["updated_at"] = now
    path = history_path(account)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _new_driver():
    options = Options()
    # DealerOn pages keep analytics/network requests running after the useful
    # vehicle HTML is ready. Do not block the scan waiting for every request.
    options.page_load_strategy = "eager"
    apply_background_mode_option(options)
    options.add_argument("--start-maximized")
    configure_stealth_options(options)
    driver = build_chrome_driver(options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    apply_stealth_patches(driver)
    return driver


def _navigate(driver, url):
    """Navigate while tolerating a page whose background requests never end."""
    try:
        driver.get(url)
    except TimeoutException:
        print(
            f"[CaliforniaAutoFinance] Navigation timed out after "
            f"{PAGE_LOAD_TIMEOUT_SECONDS}s; using the HTML already loaded: {url}",
            flush=True,
        )
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass


def find_first_unposted_links(driver, account, limit=None):
    history = load_history(account)
    seen = set(history.get("links") or [])
    seen.update(
        str(item.get("url") or "")
        for item in (history.get("listings") or [])
        if isinstance(item, dict) and str(item.get("status") or "").lower() == "posted"
    )
    card_selector = "[data-vehicle-information], .vehicle-card"
    all_links = []

    def capture_links(page_links):
        # The inventory grid is re-rendered while it loads/expands. Read fresh
        # anchors directly instead of keeping vehicle-card references that can
        # become stale between find_elements calls.
        anchor_selector = (
            "[data-vehicle-information] a.vehicle-title, "
            "[data-vehicle-information] a[href*='/used-'], "
            ".vehicle-card a.vehicle-title, "
            ".vehicle-card a[href*='/used-']"
        )
        for attempt in range(3):
            try:
                anchors = driver.find_elements(By.CSS_SELECTOR, anchor_selector)
                hrefs = []
                for anchor in anchors:
                    try:
                        href = anchor.get_attribute("href") or ""
                    except StaleElementReferenceException:
                        raise
                    if href:
                        hrefs.append(href)
            except StaleElementReferenceException:
                if attempt < 2:
                    time.sleep(0.3)
                    continue
                print(
                    "[CaliforniaAutoFinance] Inventory changed while reading links; "
                    "continuing with the links already captured",
                    flush=True,
                )
                return

            for href in hrefs:
                link = urljoin(SITE_ORIGIN, href)
                host = (urlparse(link).hostname or "").lower()
                if (
                    host in {"californiaautofinancegroup.com", "www.californiaautofinancegroup.com"}
                    and "/used-" in link.lower()
                    and link not in all_links
                ):
                    all_links.append(link)
                    page_links.append(link)
            return

    target_count = max(1, int(limit)) if limit else None
    page_urls = [SEARCH_URL] + [f"{SEARCH_URL}?pt={page_index}" for page_index in range(1, 4)]

    for page_number, page_url in enumerate(page_urls, start=1):
        _navigate(driver, page_url)
        try:
            WebDriverWait(driver, ELEMENT_WAIT_SECONDS).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, card_selector))
            )
        except TimeoutException:
            print(
                f"[CaliforniaAutoFinance] No inventory cards found on page {page_number}: {page_url}",
                flush=True,
            )
            continue

        page_links = []
        # Capture after each expansion in case a page also has a Load More control.
        for _ in range(50):
            capture_links(page_links)
            previous_page_count = len(page_links)
            clicked = False
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                controls = driver.find_elements(
                    By.CSS_SELECTOR,
                    "#load-more-vehicles__btn, .load-more-vehicles button, [data-load-more], "
                    "button[id*='load-more'], a[id*='load-more']",
                )
                if not controls:
                    controls = driver.find_elements(
                        By.XPATH,
                        "//*[self::button or self::a][contains(translate(normalize-space(.), "
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'load more')]",
                    )
                for control in controls:
                    if control.is_displayed() and control.is_enabled():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", control)
                        driver.execute_script("arguments[0].click();", control)
                        clicked = True
                        break
            except Exception:
                clicked = False
            try:
                WebDriverWait(driver, 8).until(
                    lambda current: len(current.find_elements(By.CSS_SELECTOR, "a[href*='/used-']"))
                    > previous_page_count
                )
            except Exception:
                pass
            capture_links(page_links)
            if len(page_links) == previous_page_count:
                break

        page_eligible = [link for link in page_links if link not in seen and not _is_bike_url(link)]
        total_eligible = [link for link in all_links if link not in seen and not _is_bike_url(link)]
        print(
            f"[CaliforniaAutoFinance] Page {page_number}/4 URLs={len(page_links)} "
            f"new_eligible={len(page_eligible)} total_eligible={len(total_eligible)}",
            flush=True,
        )
        if target_count and len(total_eligible) >= target_count:
            break

    eligible = [link for link in all_links if link not in seen and not _is_bike_url(link)]
    print(
        f"[CaliforniaAutoFinance] Inventory URLs={len(all_links)} "
        f"posted_for_account={sum(1 for link in all_links if link in seen)} "
        f"eligible={len(eligible)}",
        flush=True,
    )
    # Return URLs in page order. Filtering and batching happen
    # during extraction so a skipped/failed URL is replenished from later batches.
    return all_links


def _text(driver, selector):
    try:
        return _clean(driver.find_element(By.CSS_SELECTOR, selector).text)
    except Exception:
        return ""


def _labelled_values(driver):
    values = {}
    for item in driver.find_elements(By.CSS_SELECTOR, ".info__item"):
        try:
            label = _clean(item.find_element(By.CSS_SELECTOR, ".info__label").text).lower()
            value = _clean(item.find_element(By.CSS_SELECTOR, ".info__value").text)
            if label:
                values[label] = value
        except Exception:
            continue
    return values


def _digits(value):
    return re.sub(r"[^0-9]", "", str(value or ""))


def _price_digits(value):
    match = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?", str(value or ""))
    if not match:
        return ""
    try:
        amount = int(float(match.group(0).replace(",", "")))
    except (TypeError, ValueError):
        return ""
    return str(amount) if amount > 0 else ""


def _extract_price(driver):
    selectors_and_attributes = (
        (".vehiclePricingHighlightAmount", ("textContent", "innerText", "title")),
        ("[data-dotagging-item-price]", ("data-dotagging-item-price",)),
        ("[data-msrp]", ("data-msrp",)),
        ("[data-price]", ("data-price",)),
        ("[itemprop='price']", ("content", "textContent")),
        ("meta[property='product:price:amount']", ("content",)),
    )
    for selector, attributes in selectors_and_attributes:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            for attribute in attributes:
                digits = _price_digits(element.get_attribute(attribute))
                if digits:
                    return digits

    # DealerOn pages may keep the offer only in JSON-LD even when the visible
    # price widget is rendered late or hidden at the current viewport size.
    for script in driver.find_elements(By.CSS_SELECTOR, "script[type='application/ld+json']"):
        raw = script.get_attribute("textContent") or ""
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "price" in item:
                    digits = _price_digits(item.get("price"))
                    if digits:
                        return digits
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    source = str(driver.page_source or "")
    patterns = (
        r"vehiclePricingHighlightAmount[^>]*>\s*\$?\s*([0-9][0-9,]*)",
        r'data-dotagging-item-price=["\']([0-9][0-9,.]*)',
        r'data-msrp=["\']([0-9][0-9,.]*)',
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            digits = _price_digits(match.group(1))
            if digits:
                return digits
    return ""


def _parse_title(driver):
    year = _text(driver, ".vehicle-details__title .vehicle-title__year")
    make_model = _text(driver, ".vehicle-details__title .vehicle-title__make-model")
    full_name = _clean(" ".join(filter(None, (year, make_model))))
    make = ""
    model = ""
    parts = make_model.split()
    if parts:
        make = parts[0]
        model = " ".join(parts[1:])
    if not year or not make_model:
        fallback = _text(driver, ".vehicle-details__title .vehicle-title__text")
        match = re.match(r"^\s*(\d{4})\s+(\S+)\s+(.+)$", fallback)
        if match:
            year, make, model = match.groups()
            full_name = fallback
    return year, make, model, full_name


def _is_bike_listing(title, labelled_values):
    body_value = str((labelled_values or {}).get("body style") or "")
    combined = _clean(f"{title} {body_value}").lower()
    bike_terms = (
        "e-bike",
        "ebike",
        "electric bike",
        "motorcycle",
        "motor bike",
        "motorbike",
        "sportbike",
        "sport bike",
        "dirt bike",
        "street bike",
        "touring bike",
        "scooter",
        "moped",
        "cruiser motorcycle",
    )
    if any(term in combined for term in bike_terms):
        return True
    motorcycle_makes = (
        "harley-davidson",
        "harley davidson",
        "ducati",
        "indian motorcycle",
        "ktm ",
        "husqvarna motorcycle",
        "victory motorcycle",
        "aprilia",
        "moto guzzi",
    )
    return any(make in f"{combined} " for make in motorcycle_makes)


def _image_links(driver):
    try:
        button = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#load-more-vehicles__btn"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
        driver.execute_script("arguments[0].click();", button)
        time.sleep(0.5)
    except Exception:
        pass
    links = []
    seen_images = set()
    selectors = (
        ".thumbnails--desktop a.thumbnail[href]",
        ".vehicle-images .hero-carousel__image[src]",
    )
    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            # Prefer the gallery anchor (normally the original/full-size file),
            # then the largest srcset candidate, and only then the displayed src.
            candidates = []
            if element.tag_name.lower() == "a":
                candidates.append(element.get_attribute("href") or "")
                try:
                    image = element.find_element(By.CSS_SELECTOR, "img")
                except Exception:
                    image = None
            else:
                image = element
            if image is not None:
                srcset = image.get_attribute("srcset") or image.get_attribute("data-srcset") or ""
                srcset_items = []
                for item in srcset.split(","):
                    parts = item.strip().split()
                    if not parts:
                        continue
                    size = 0
                    if len(parts) > 1:
                        try:
                            size = int(re.sub(r"[^0-9]", "", parts[-1]) or 0)
                        except ValueError:
                            size = 0
                    srcset_items.append((size, parts[0]))
                if srcset_items:
                    candidates.append(max(srcset_items, key=lambda item: item[0])[1])
                candidates.extend(
                    image.get_attribute(attribute) or ""
                    for attribute in ("data-src", "data-lazy", "data-original", "src")
                )

            for raw in candidates:
                link = urljoin(SITE_ORIGIN, raw)
                parsed = urlparse(link)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue
                # The same photo can appear in thumbnail and hero widgets.
                # Dedupe by its host/path while retaining the first (best) URL.
                image_key = (parsed.netloc.lower(), unquote(parsed.path).lower())
                if image_key in seen_images:
                    continue
                seen_images.add(image_key)
                links.append(link)
                break
            if len(links) >= 20:
                return links
    return links[:20]


def _download_images(driver, urls, listing_url):
    target = Path(screenshots_dir())
    target.mkdir(parents=True, exist_ok=True)
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in driver.get_cookies())
    user_agent = driver.execute_script("return navigator.userAgent")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = []
    seen_content = set()
    for index, url in enumerate(urls[:20], start=1):
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        path = target / f"california_{stamp}_{index:02d}_{digest}{suffix}"
        request = urllib.request.Request(url, headers={
            "User-Agent": user_agent,
            "Referer": listing_url,
            "Cookie": cookie_header,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                content = response.read()
            # Reject placeholders, broken responses, and tiny thumbnails.
            if not content_type.startswith("image/") or len(content) < 20_000:
                print(f"[CaliforniaAutoFinance] Skipping low-quality image {url}", flush=True)
                continue
            content_hash = hashlib.sha256(content).hexdigest()
            if content_hash in seen_content:
                continue
            seen_content.add(content_hash)
            path.write_bytes(content)
            saved.append(str(path))
        except Exception as exc:
            print(f"[CaliforniaAutoFinance] Image download failed {url}: {exc}", flush=True)
    return saved


def _dealer_comments(driver):
    """Return only the dealer-provided comment text, or an empty string."""
    containers = []
    selectors = (
        "#dealer-comments",
        "[data-section='dealer-comments']",
        ".dealer-comments",
    )
    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if element not in containers:
                containers.append(element)

    if not containers:
        # DealerOn layouts sometimes identify the section only by its heading.
        headings = driver.find_elements(
            By.XPATH,
            "//*[self::h2 or self::h3 or self::h4]["
            "translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')="
            "'dealer comments']",
        )
        for heading in headings:
            try:
                container = heading.find_element(
                    By.XPATH,
                    "ancestor::*[self::section or self::div][1]",
                )
                if container not in containers:
                    containers.append(container)
            except Exception:
                continue

    ignored_lines = {
        "comments",
        "dealer comments",
        "read more",
        "read more...",
        "hide comments",
        "more",
        "more...",
        "less",
    }
    candidates = []
    for container in containers:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", container)
        except Exception:
            pass
        try:
            controls = container.find_elements(
                By.XPATH,
                ".//*[self::button or self::a][contains(translate(normalize-space(.),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'more')]",
            )
            for control in controls:
                if control.is_displayed() and control.is_enabled():
                    driver.execute_script("arguments[0].click();", control)
                    time.sleep(0.4)
                    break
        except Exception:
            pass

        try:
            raw = container.get_attribute("textContent") or container.text or ""
        except Exception:
            raw = ""
        lines = []
        for line in str(raw).splitlines():
            cleaned = _clean(line)
            if cleaned and cleaned.lower() not in ignored_lines:
                lines.append(cleaned)
        value = "\n".join(dict.fromkeys(lines)).strip()
        if value:
            candidates.append(value)

    return max(candidates, key=len) if candidates else ""


def scrape_listing(driver, url):
    _navigate(driver, url)
    WebDriverWait(driver, ELEMENT_WAIT_SECONDS).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".vehicle-details__title, .details-header__title"))
    )
    year, make, model, title = _parse_title(driver)
    values = _labelled_values(driver)
    price = _extract_price(driver)
    mileage = re.sub(r"[^0-9]", "", values.get("mileage", ""))
    transmission_raw = values.get("transmission", "")
    transmission = "Manual" if "manual" in transmission_raw.lower() else "Automatic"
    description = _dealer_comments(driver)
    image_paths = _download_images(driver, _image_links(driver), url)
    is_bike = _is_bike_listing(title, values)
    payload = {
        "title": title or " ".join(filter(None, (year, make, model))),
        "vehicle_type": "Other" if is_bike else "Car",
        "year": year,
        "make": make,
        "model": model,
        "mileage": mileage,
        "price": price,
        "body_style": "Other",
        "exterior_color": "Other",
        "interior_color": "Other",
        "vehicle_condition": "Very good",
        "fuel_type": "Other",
        "transmission": transmission,
        "description": description,
        "description_optional": True,
        "location": "",
        "source_url": url,
    }
    for index, path in enumerate(image_paths[:20], start=1):
        payload[f"image_path{index}"] = path
    required = ("year", "make", "model", "mileage", "price")
    missing = [field for field in required if not payload.get(field)]
    if missing:
        raise ValueError(f"Missing listing field(s): {', '.join(missing)}")
    if not image_paths:
        raise ValueError("No vehicle images could be downloaded")
    return payload


def collect_four_listings(account, limit=4):
    driver = _new_driver()
    try:
        target_count = max(1, int(limit or 4))
        # Collect the whole ordered candidate pool. A URL that fails scraping
        # or resolves to a bike must not consume one of the four result slots.
        links = find_first_unposted_links(driver, account, limit=target_count)
        if not links:
            return []
        history = load_history(account)
        seen = set(history.get("links") or [])
        seen.update(
            str(item.get("url") or "")
            for item in (history.get("listings") or [])
            if isinstance(item, dict) and str(item.get("status") or "").lower() == "posted"
        )
        results = []
        for batch_start in range(0, len(links), 4):
            batch = links[batch_start:batch_start + 4]
            print(
                f"[CaliforniaAutoFinance] Checking URL batch "
                f"{batch_start + 1}-{batch_start + len(batch)}",
                flush=True,
            )
            for link in batch:
                if link in seen or _is_bike_url(link):
                    continue
                try:
                    listing = scrape_listing(driver, link)
                    if _is_bike_listing(listing.get("title", ""), {
                        "body style": listing.get("body_style", "")
                    }):
                        print(f"[CaliforniaAutoFinance] Skipping bike listing {link}", flush=True)
                        continue
                    results.append(listing)
                    if len(results) >= target_count:
                        return results
                except Exception as exc:
                    print(f"[CaliforniaAutoFinance] Listing failed {link}: {exc}", flush=True)
                    mark_history_status(account, link, "failed")
        return results
    finally:
        try:
            driver.quit()
        except Exception:
            pass
