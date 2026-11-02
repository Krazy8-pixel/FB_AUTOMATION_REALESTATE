from automation.diagnostics import event as diagnostic_event, traced
"""Import a user's Marketplace listings for the Delete Listings page."""

import hashlib
from automation.task_control import cancellable
import json
import os
import re
import threading
import subprocess
from contextlib import contextmanager
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException, TimeoutException
from urllib3.exceptions import ReadTimeoutError, MaxRetryError

from automation.reposting import _open_prelogged_facebook
from automation.runtime_paths import data_dir
from automation import listing_store
from automation.delete_candidates import extract_delete_candidates
from automation.reposting import read_listing_from_manage, _find_edit_listing_link, _extract_listing_id, _normalize_price
from io import BytesIO
from PIL import Image
import requests


SELLING_URL = "https://www.facebook.com/marketplace/you/selling"
AVAILABLE_SELLING_URL = SELLING_URL + '?state=LIVE&status[0]=IN_STOCK'
AVAILABLE_FILTER_TIMEOUT = 120
AVAILABLE_URL_TIMEOUT = 120
IMPORT_DIR = data_dir("delete_listings")
_active_import_drivers_lock = threading.Lock()
_active_import_drivers = {}
_active_delete_drivers_lock = threading.Lock()
_active_delete_drivers = {}


def _shutdown_import_driver(driver):
    """Interrupt a stuck WebDriver request before attempting graceful cleanup."""
    try:
        process = getattr(getattr(driver, 'service', None), 'process', None)
        if process is not None and process.poll() is None:
            if os.name == 'nt' and isinstance(process.pid, int):
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, timeout=5,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                process.terminate()
    except Exception:
        pass
    try:
        driver.quit()
    except Exception:
        pass


def stop_listing_import_driver(account):
    """Request browser shutdown without blocking the Stop Import HTTP request."""
    with _active_import_drivers_lock:
        driver = _active_import_drivers.get(account)
    if driver is None:
        return False
    threading.Thread(target=_shutdown_import_driver, args=(driver,), daemon=True,
                     name='stop-listing-import').start()
    return True


def stop_listing_delete_driver(account):
    """Immediately close the active Facebook deletion browser for an account."""
    with _active_delete_drivers_lock:
        driver = _active_delete_drivers.get(account)
    if driver is None:
        return False
    # Chrome shutdown may hang; never perform it on the Stop HTTP request.
    threading.Thread(target=_shutdown_import_driver, args=(driver,), daemon=True,
                     name='stop-listing-delete').start()
    return True


class ListingImportCancelled(RuntimeError):
    pass


class NoActiveListings(BaseException):
    """Facebook explicitly reported that the account has no active listings."""


def _active_listing_count(driver):
    """Read the profile count only when its complete label is unambiguous."""
    lower = "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'"
    locators = (
        f"//span[contains(translate(normalize-space(.),{lower}),'active listing')]",
        f"//*[@aria-label and contains(translate(@aria-label,{lower}),'active listing')]",
        f"//*[self::div or self::p][not(*) and contains(translate(normalize-space(.),{lower}),'active listing')]",
    )
    for xpath in locators:
        try:
            for element in driver.find_elements(By.XPATH, xpath):
                if not element.is_displayed():
                    continue
                label = element.text or element.get_attribute('aria-label') or ''
                match = re.fullmatch(r'\s*([0-9][0-9,]*)\s+active\s+listings?\s*', label, re.I)
                if match:
                    return int(match.group(1).replace(',', ''))
        except Exception:
            continue
    return None


def _check_active_listing_count(driver):
    count = _active_listing_count(driver)
    if count == 0:
        raise NoActiveListings()
    return count


class _ImportStopSignal(BaseException):
    """Escape browser retry handlers that catch Exception, like CancelledError."""


class _ImportBrowserLost(BaseException):
    """Stop immediately rather than retrying commands on a dead browser."""


class _ListingReadTimedOut(BaseException):
    """Escape nested retry loops so one listing cannot consume the whole import."""


@contextmanager
def _listing_read_deadline(driver, seconds=120):
    deadline = time.monotonic() + seconds
    original = driver.execute

    def execute(command, params=None):
        if command not in {'quit', 'close'} and time.monotonic() >= deadline:
            raise _ListingReadTimedOut()
        return original(command, params)

    driver.execute = execute
    try:
        yield
    except _ListingReadTimedOut:
        raise RuntimeError('Listing read exceeded its 120-second limit; continuing to the next listing.') from None
    finally:
        driver.execute = original


def _guard_import_driver(driver, stop_event):
    original = driver.execute

    def execute(command, params=None):
        if command not in {'quit', 'close'} and stop_event.is_set():
            raise _ImportStopSignal()
        try:
            return original(command, params)
        except (InvalidSessionIdException, NoSuchWindowException, ReadTimeoutError, MaxRetryError) as exc:
            if command in {'quit', 'close'}:
                raise
            raise _ImportBrowserLost(str(exc)) from exc

    driver.execute = execute


def _safe_key(value):
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9_.-]+", "_", text) or "unknown"


def _xpath_literal(value):
    """Quote arbitrary text safely for use as an XPath string literal."""
    text = str(value or "")
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def listings_store_path(account):
    return IMPORT_DIR / f"listings_{_safe_key(account)}.json"


def load_listings(account):
    return listing_store.load(account, listings_store_path(account))


def save_listings(account, listings):
    listing_store.save(account, listings if isinstance(listings, list) else [])


def download_listing_images(driver, account, listing_id, urls, stop_event):
    """Keep the original CDN bytes; never resize or recompress downloaded photos."""
    folder = IMPORT_DIR / 'images' / hashlib.sha256(account.encode()).hexdigest()[:24] / listing_id
    folder.mkdir(parents=True, exist_ok=True)
    images, errors = [], []
    with requests.Session() as session:
        session.headers.update({'User-Agent': driver.execute_script('return navigator.userAgent'), 'Referer': 'https://www.facebook.com/'})
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', '.facebook.com'))
        for url in urls:
            if stop_event.is_set():
                raise ListingImportCancelled()
            try:
                from urllib.parse import urlsplit
                host = (urlsplit(url).hostname or '').lower()
                if not host.endswith('.fbcdn.net') or not url.startswith('https://'):
                    raise ValueError('Unsupported listing image URL')
                response = session.get(url, timeout=(5, 5), stream=True)
                try:
                    response.raise_for_status()
                    chunks = []
                    for chunk in response.iter_content(64 * 1024):
                        if stop_event.is_set():
                            raise ListingImportCancelled()
                        if chunk:
                            chunks.append(chunk)
                    content = b''.join(chunks)
                finally:
                    response.close()
                if stop_event.is_set():
                    raise ListingImportCancelled()
                with Image.open(BytesIO(content)) as picture:
                    width, height = picture.size
                    extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(picture.format)
                    picture.verify()
                if not extension:
                    raise ValueError('Unsupported photo format')
                # Keep filenames short: this project already lives in a long
                # Windows Downloads path. Full SHA256 + .tmp exceeded MAX_PATH.
                name = hashlib.sha256(content).hexdigest()[:24] + extension
                path = folder / name
                temporary = folder / (name + '.tmp')
                temporary.write_bytes(content)
                os.replace(temporary, path)
                if any(item['name'] == name for item in images):
                    continue
                images.append({'name': name, 'path': str(path.resolve()), 'url': url, 'width': width, 'height': height})
            except ListingImportCancelled:
                raise
            except Exception as exc:
                errors.append(str(exc))
    return images, errors


def _record_key(record):
    if listing_store.is_backup(record) or record.get('source_deleted'):
        return 'backup:' + str(record.get('id') or '')
    facebook_id = _extract_listing_id(record.get('url') or record.get('edit_url') or '')
    if facebook_id:
        return 'url:https://www.facebook.com/marketplace/item/' + facebook_id
    url = str(record.get('url') or '').strip().split('?', 1)[0].rstrip('/')
    if '/marketplace/item/' in url:
        return 'url:' + url.lower()
    text = "|".join(str(record.get(field) or "").strip().lower() for field in ("year", "make", "model"))
    if text.replace("|", ""):
        return "title:" + text
    url = str(record.get("url") or "").strip().split("?", 1)[0].rstrip("/")
    return "url:" + url.lower()


def _parse_click_count(*values):
    """Parse one unambiguous Marketplace click metric from scoped card text."""
    combined = "\n".join(str(value or "").strip() for value in values if str(value or "").strip())
    matches = re.findall(
        r"(?<!\d)([\d,.]+)\s*([kKmM]?)\s+clicks?\s+on\s+(?:the\s+)?listing\b",
        combined,
        re.IGNORECASE,
    )
    parsed = []
    for raw_number, suffix in matches:
        normalized = raw_number.replace(",", "")
        try:
            value = float(normalized)
        except (TypeError, ValueError):
            continue
        multiplier = {"k": 1_000, "m": 1_000_000}.get(str(suffix or "").lower(), 1)
        parsed.append(int(round(value * multiplier)))
    unique = set(parsed)
    # More than one distinct metric means the candidate accidentally spans
    # multiple cards. Do not attach either number to a listing.
    return next(iter(unique)) if len(unique) == 1 else None


def _candidate_listing_status(candidate):
    """Read card status badges, never action labels such as 'Mark as sold'."""
    explicit = str(candidate.get('status') or '').strip().casefold()
    text = str(candidate.get('text') or '')
    badges = [explicit] + [line.strip().casefold() for line in text.splitlines()]
    for status in ('sold', 'pending', 'active', 'available'):
        if any(re.match(r'^' + status + r'(?:$|\s*[Â·â€¢|â€“â€”-]\s*|\s+on\s+)', badge)
               for badge in badges):
            return status
    return ''


def _clean_card_title(value):
    """Keep the vehicle name and drop trim, price, and Marketplace card metadata."""
    title = " ".join(str(value or '').split())
    if not title:
        return ''
    # Marketplace often concatenates the title, trim, price, status, and click
    # summary into one accessible label. The bullet is its title/trim boundary.
    title = re.split(r'\s+[\u00b7\u2022|]\s+', title, maxsplit=1)[0]
    title = re.split(
        r',\s*(?=(?:MAD|USD|CAD|PKR|GBP|EUR|\$|Active\b|Sold\b|Pending\b|'
        r'Available\b|Listed\s+on\b|\d[\d,.]*\s+clicks?\b))',
        title, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    return title.strip(' ,')


def clean_imported_listing_fields(record):
    """Repair vehicle labels and dates polluted by concatenated card text."""
    record = dict(record or {})
    model = _clean_card_title(record.get('model'))
    year = str(record.get('year') or '').strip()
    make = str(record.get('make') or '').strip()
    prefix = ' '.join(part for part in (year, make) if part)
    if prefix and model.casefold().startswith(prefix.casefold() + ' '):
        model = model[len(prefix):].strip()
    record['model'] = model
    listed = str(record.get('listed_date') or '').strip()
    if listed:
        listed = re.split(r',\s*(?=(?:listed\s+on\s+marketplace|\d[\d,.]*\s+clicks?\b|active\b|sold\b|pending\b))',
                          listed, maxsplit=1, flags=re.IGNORECASE)[0]
        record['listed_date'] = listed.strip(' ,')
    return record


def _listing_from_candidate(candidate):
    href = str(candidate.get("href") or "").strip()
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    text = str(candidate.get("text") or "").strip()
    title = str(candidate.get("title") or "").strip()
    combined = "\n".join(part for part in (title, text) if part)
    lines = [" ".join(line.split()) for line in combined.splitlines() if line.strip()]
    title_line = next(
        (line for line in lines if re.match(r"^(19\d{2}|20\d{2})\s+", line)),
        " ".join(lines),
    )
    title_line = _clean_card_title(title_line)
    match = re.search(r"\b(19\d{2}|20\d{2})\s+([^\n|]+)", title_line)
    if match:
        year = match.group(1)
        words = match.group(2).strip().split()
        make = words[0] if words else ""
        model = " ".join(words[1:]).strip()
        for compound_make in ('Land Rover', 'Alfa Romeo', 'Aston Martin', 'Rolls Royce', 'Mercedes Benz'):
            if ' '.join(words).casefold().startswith(compound_make.casefold() + ' '):
                make = ' '.join(words[:2])
                model = ' '.join(words[2:]).strip()
                break
    else:
        year, make, model = "", "", title or combined
    if not (year or make or model or href):
        return None
    price_match = re.search(r"(?:(?:MAD|USD|CAD|PKR|GBP|EUR)\s*\$?\s*|\$\s*)([\d,]+(?:\.\d{1,2})?)", combined, re.IGNORECASE)
    listed_match = re.search(r"\bListed\s+on\s+([^\n\r|Â·,]+)", combined, re.IGNORECASE)
    clicks = _parse_click_count(candidate.get("click_text"), combined)
    images = [
        str(url).strip()
        for url in (candidate.get("images") or [])
        if str(url).strip()
    ]
    raw_id = href or f"{year}|{make}|{model}"
    listing_id = hashlib.sha1(raw_id.lower().encode("utf-8", "ignore")).hexdigest()[:20]
    record = {
        "id": listing_id,
        "listing_status": _candidate_listing_status(candidate),
        "year": year,
        "make": make,
        "model": model,
        "price": (re.search(r"(?:MAD|USD|CAD|PKR|GBP|EUR)", price_match.group(0), re.IGNORECASE).group(0).upper() + " " if re.search(r"(?:MAD|USD|CAD|PKR|GBP|EUR)", price_match.group(0), re.IGNORECASE) else "$" if "$" in price_match.group(0) else "") + price_match.group(1).strip() if price_match else "",
        "listed_date": listed_match.group(1).strip() if listed_match else "",
        "clicks": clicks if clicks is not None else 0,
        "clicks_confirmed": clicks is not None,
        "url": href,
        "images": images,
        "image": images[0] if images else "",
    }
    if clicks is not None:
        record["clicks_checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return record


def _matching_record_key(records_by_key, record):
    """Find an existing record even when Facebook adds/removes trim text."""
    key = _record_key(record)
    facebook_id = _extract_listing_id(record.get('url') or record.get('edit_url') or '')
    if facebook_id:
        for existing_key, existing in records_by_key.items():
            if listing_store.is_backup(existing) or (existing.get('source_deleted') and existing.get('facebook_reposted')):
                continue  # Shared backups are not live listings owned by this account.
            if _extract_listing_id(existing.get('url') or existing.get('edit_url') or '') == facebook_id:
                return existing_key
    if key in records_by_key:
        return key
    year = str(record.get("year") or "").strip().lower()
    make = str(record.get("make") or "").strip().lower()
    model = re.sub(r"\s+", " ", str(record.get("model") or "").strip().lower())
    if not (year and make and model):
        return key
    for existing_key, existing in records_by_key.items():
        pending_repost = existing.get('source_deleted') and existing.get('facebook_reposted')
        if listing_store.is_backup(existing):
            continue
        old_id = _extract_listing_id(existing.get('url') or existing.get('edit_url') or '')
        if facebook_id and old_id and not pending_repost:
            continue
        if str(existing.get("year") or "").strip().lower() != year:
            continue
        if str(existing.get("make") or "").strip().lower() != make:
            continue
        old_model = re.sub(r"\s+", " ", str(existing.get("model") or "").strip().lower())
        if old_model and (old_model == model or old_model.startswith(model) or model.startswith(old_model)):
            return existing_key
    return key


def _extract_visible_listing_candidates(driver, include_image_lookup=True, deduplicate=False):
    """Collect listing links/cards without relying on Facebook CSS classes."""
    candidates = []

    # Facebook's current Manage Listings card is split into sibling branches:
    # checkbox, photo, text, and actions. Selenium XPath against the inner
    # text button cannot see the sibling image. Walk upward from each semantic
    # checkbox in JavaScript until the complete row is found, then return the
    # title button itself as the clickable element.
    try:
        rows = driver.execute_script(
            """
            const results = [];
            for (const checkbox of document.querySelectorAll('input[aria-label="Select Listing"]')) {
              let row = checkbox.parentElement;
              while (row && row !== document.body) {
                const image = row.querySelector('img');
                const more = row.querySelector('[role="button"][aria-label^="More options for "]');
                if (image && more) break;
                row = row.parentElement;
              }
              if (!row || row === document.body) continue;
              const controls = [...row.querySelectorAll('[role="button"][aria-label]')];
              const titleControl = controls.find(el => {
                const label = (el.getAttribute('aria-label') || '').trim();
                return label && !label.startsWith('More options for ');
              });
              const links = [...row.querySelectorAll('a[href]')].map(a => a.href).filter(Boolean);
              const href = links.find(value => value.includes('/marketplace/item/') || value.includes('listing_id'))
                || links.find(value => value.includes('/marketplace/')) || '';
              const images = [...row.querySelectorAll('img')]
                .map(img => img.currentSrc || img.src || '')
                .filter(Boolean);
              const rowText = row.innerText || '';
               results.push({
                 href,
                 title: titleControl ? (titleControl.getAttribute('aria-label') || '') : '',
                 text: rowText,
                 images,
                 element: titleControl || row,
                 source: 'full_row_dom'
               });
            }
            return results;
            """
        ) or []
        candidates.extend(item for item in rows if isinstance(item, dict))
    except Exception as exc:
        print(f"[DeleteListings] full-row DOM scan failed: {exc}", flush=True)

    anchors = driver.find_elements(
        By.XPATH,
        "//a[contains(@href,'/marketplace/item/') or contains(@href,'listing_id')]",
    )
    for anchor in anchors:
        try:
            if not anchor.is_displayed():
                continue
            href = anchor.get_attribute("href") or ""
            title = anchor.text or anchor.get_attribute("aria-label") or ""
            card_text = title
            images = []
            try:
                card = anchor.find_element(
                    By.XPATH,
                    "./ancestor::*[@role='article' or @role='button'][1]",
                )
                card_text = card.text or title
                images = [
                    image.get_attribute("src") or image.get_attribute("currentSrc") or ""
                    for image in card.find_elements(By.XPATH, ".//img[@src]")
                ]
            except Exception:
                pass
            candidates.append({"href": href, "title": title, "text": card_text, "click_text": "", "images": images, "element": anchor})
        except Exception:
            continue

    # The current Manage Listings layout renders each row as nested
    # role=button elements rather than an anchor. Start at its semantic
    # checkbox, then move to the smallest ancestor containing the image,
    # listing details, and More options control. This avoids Facebook's
    # generated CSS class names.
    row_locator = (
        "//input[@aria-label='Select Listing']/ancestor::div["
        ".//img and .//*[@role='button' and starts-with(@aria-label,'More options for ')]]"
        "[1]"
    )
    for row in driver.find_elements(By.XPATH, row_locator):
        try:
            if not row.is_displayed():
                continue
            title_controls = row.find_elements(
                By.XPATH,
                ".//*[@role='button' and @aria-label and "
                "not(starts-with(@aria-label,'More options for '))]",
            )
            title = next(
                (str(control.get_attribute("aria-label") or "").strip()
                 for control in title_controls
                 if str(control.get_attribute("aria-label") or "").strip()),
                "",
            )
            href_values = [
                str(link.get_attribute("href") or "").strip()
                for link in row.find_elements(By.XPATH, ".//a[@href]")
            ]
            href_values.extend(
                str(row.get_attribute(name) or "").strip()
                for name in ("data-href", "data-url", "data-permalink")
            )
            href = next(
                (value for value in href_values if "/marketplace/item/" in value or "listing_id" in value),
                next((value for value in href_values if "/marketplace/" in value), ""),
            )
            images = [
                image.get_attribute("src") or image.get_attribute("currentSrc") or ""
                for image in row.find_elements(By.XPATH, ".//img[@src]")
            ]
            candidates.append({
                "href": href,
                "title": title,
                "text": row.text or "",
                "click_text": "",
                "images": images,
                "element": row,
            })
        except Exception:
            continue

    # Fallback for older layouts without the Select Listing checkbox.
    card_locator = (
        "//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'listed on')]/ancestor::*[@role='button'][1]"
    )
    for card in driver.find_elements(By.XPATH, card_locator):
        try:
            if not card.is_displayed():
                continue
            href = ""
            # Depending on the Marketplace layout the listing URL is either
            # an anchor inside the card or stored as a data/aria attribute on
            # the card itself. Prefer an actual marketplace item URL, then
            # fall back to the first usable Facebook marketplace link.
            links = card.find_elements(By.XPATH, ".//a[@href]")
            href_values = [str(link.get_attribute("href") or "").strip() for link in links]
            href_values.extend(
                str(card.get_attribute(name) or "").strip()
                for name in ("data-href", "data-url", "data-permalink", "aria-label")
            )
            href = next(
                (value for value in href_values if "/marketplace/item/" in value or "listing_id" in value),
                next((value for value in href_values if "/marketplace/" in value), ""),
            )
            images = [
                image.get_attribute("src") or image.get_attribute("currentSrc") or ""
                for image in card.find_elements(By.XPATH, ".//img[@src]")
            ]
            candidates.append({"href": href, "title": card.get_attribute("aria-label") or "", "text": card.text or "", "click_text": "", "images": images, "element": card})
        except Exception:
            continue

    # The same card can be returned by the full-row, anchor, and fallback
    # queries. Collapse those DOM duplicates before processing scan results.
    if deduplicate:
        deduplicated = []
        seen = set()
        for candidate in candidates:
            href = str(candidate.get('href') or '').strip()
            listing_id = _extract_listing_id(href)
            if listing_id:
                identity = ('facebook_id', listing_id)
            else:
                title = re.sub(r'\s+', ' ', str(candidate.get('title') or '').strip().casefold())
                text = re.sub(r'\s+', ' ', str(candidate.get('text') or '').strip().casefold())
                identity = ('text', title or text[:240])
            if not identity[1] or identity in seen:
                continue
            seen.add(identity)
            deduplicated.append(candidate)
        candidates = deduplicated

    if not include_image_lookup:
        return candidates

    # In Facebook's markup the title is also the exact aria-label of the
    # sibling div that owns the listing photo. Once a title is known, directly
    # select the img inside that div; no parent/card inference is needed.
    for candidate in candidates:
        if candidate.get("images"):
            continue
        title = str(candidate.get("title") or "").strip()
        if not title:
            continue
        try:
            image_elements = driver.find_elements(
                By.XPATH,
                f"//div[@aria-label={_xpath_literal(title)} and .//img]//img",
            )
            urls = []
            for image_element in image_elements:
                url = str(
                    image_element.get_attribute("src")
                    or image_element.get_attribute("currentSrc")
                    or ""
                ).strip()
                if url and url not in urls:
                    urls.append(url)
            candidate["images"] = urls
            if urls:
                print(f"[DeleteListings] image captured for {title}", flush=True)
        except Exception as exc:
            print(f"[DeleteListings] image lookup failed for {title}: {exc}", flush=True)
    return candidates


def _wait_for_inventory(driver, timeout=30, candidate_reader=None):
    def ready(current):
        candidates = candidate_reader(current) if candidate_reader else _extract_visible_listing_candidates(current, include_image_lookup=False)
        if candidates:
            return True
        return current.execute_script("return /(?:you haven.t listed anything|you have no listings|no listings yet|no listings found|no results found)/i.test(document.querySelector('[role=main]')?.innerText || '');") is True
    return WebDriverWait(driver, timeout).until(ready)


def _click_manage_listings(driver, settle_seconds=5, timeout=20, candidate_reader=None):
    locators = (
        (By.XPATH, "//*[@aria-label='Manage listings']"),
        (
            By.XPATH,
            "//*[self::span or self::div][translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='manage listings']"
            "/ancestor::*[@role='button'][1]",
        ),
    )
    for locator in locators:
        try:
            element = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(locator))
            try:
                element.click()
            except Exception:
                driver.execute_script("arguments[0].click();", element)
            print("[DeleteListings] Manage listings clicked", flush=True)
            _wait_for_inventory(driver, timeout=timeout, candidate_reader=candidate_reader)
            return True
        except Exception:
            continue
    print("[DeleteListings] Manage listings control not found; scanning current page", flush=True)
    return False


@cancellable
def _set_available_filter(driver, check_active_count=False):
    """Use semantic labels; Facebook varies between 'and' and '&'."""
    try:
        WebDriverWait(driver, AVAILABLE_FILTER_TIMEOUT, poll_frequency=0.5).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
        )
    except Exception as exc:
        print(f'[Import] Selling page HTML did not become ready for filters: {type(exc).__name__}.', flush=True)
        return False
    if check_active_count:
        _check_active_listing_count(driver)
    deadline = time.monotonic() + AVAILABLE_FILTER_TIMEOUT

    def remaining():
        return max(0.1, deadline - time.monotonic())

    lower = "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'"
    available = (
        f"//*[@role='radio' and contains(translate(@aria-label,{lower}),'available')]",
        f"//span[contains(translate(normalize-space(.),{lower}),'available')]/ancestor::*[@role='radio'][1]",
        f"//*[@role='radio' and contains(translate(normalize-space(.),{lower}),'available')]",
    )

    def choices(current):
        for xpath in available:
            yield from current.find_elements(By.XPATH, xpath)

    def choose(current):
        return next((item for item in choices(current) if item.is_displayed()), False)

    try:
        if not choose(driver):
            # Filters is a heading in the supplied layout. Status is the button
            # that actually reveals the radio options; its aria-label is empty.
            openers = (
                f"//*[@role='button' and translate(normalize-space(@aria-label),{lower})='status']",
                f"//span[translate(normalize-space(.),{lower})='status']/ancestor::*[@role='button'][1]",
                f"//*[@role='button' and translate(normalize-space(@aria-label),{lower})='filters']",
                f"//span[translate(normalize-space(.),{lower})='filters']/ancestor::*[@role='button'][1]",
            )
            def open_options(current):
                if choose(current):
                    return True
                for index, xpath in enumerate(openers):
                    control = next((item for item in current.find_elements(By.XPATH, xpath)
                                    if item.is_displayed() and item.is_enabled()), None)
                    if control is not None and control.get_attribute('aria-expanded') != 'true':
                        print('[Import] Opening the Status filter.', flush=True)
                        control.click()
                        # An expandable Filters panel may reveal Status first.
                        return index < 2
                return False
            WebDriverWait(driver, remaining()).until(open_options)
        radio = WebDriverWait(driver, remaining()).until(choose)
        if not radio:
            return False
        if radio.get_attribute('aria-checked') != 'true':
            print('[Import] Selecting Available in the Selling page filters.', flush=True)
            radio.click()
        # Facebook can close the menu after selecting a radio. The checked
        # option may be hidden while still confirming the selected status.
        def selected(current):
            return any(item.get_attribute('aria-checked') == 'true' for item in choices(current))
        WebDriverWait(driver, remaining()).until(selected)
        # Check real DOM state as well, so an unsuccessful click cannot select this mode.
        if not selected(driver):
            return False
        print('[Import] Available filter selected. Waiting for listing cards.', flush=True)
        _wait_for_inventory(driver, timeout=remaining())

        def available_results(current):
            if check_active_count:
                _check_active_listing_count(current)
            visible = _extract_visible_listing_candidates(current, include_image_lookup=False)
            if visible:
                return not any(_candidate_listing_status(candidate) == 'sold' for candidate in visible)
            return current.execute_script("return /(?:you haven.t listed anything|you have no listings|no listings yet|no listings found|no results found)/i.test(document.querySelector('[role=main]')?.innerText || '');") is True

        WebDriverWait(driver, remaining()).until(available_results)
        return True
    except Exception as exc:
        print(f'[Import] Could not set the Available filter: {type(exc).__name__}.', flush=True)
        return False


def _open_available_url(driver, timeout=AVAILABLE_URL_TIMEOUT, check_active_count=False):
    print('[Import] Opening the Available listings URL. Waiting for listings to load.', flush=True)
    original_page_timeout = getattr(getattr(driver, 'timeouts', None), 'page_load', None)
    client_config = getattr(getattr(driver, 'command_executor', None), '_client_config', None)
    original_client_timeout = getattr(client_config, 'timeout', None) if client_config is not None else None
    try:
        if client_config is not None:
            client_config.timeout = max(timeout + 30, 90)
        driver.set_page_load_timeout(timeout)
        try:
            driver.get(AVAILABLE_SELLING_URL)
        except TimeoutException:
            # Facebook can leave background requests open after the filtered
            # inventory is already usable. Stop loading and verify the URL and
            # listing cards instead of rejecting a successful navigation.
            try:
                driver.execute_script('window.stop();')
            except Exception:
                pass

        # Do not treat an incomplete document as an empty inventory. Wait for
        # Facebook's Selling page root before checking cards or empty states.
        WebDriverWait(driver, timeout, poll_frequency=0.5).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
        )

        def ready(current):
            if check_active_count:
                _check_active_listing_count(current)
            url = urlsplit(str(current.current_url or ''))
            query = parse_qs(url.query)
            if url.path.rstrip('/') != '/marketplace/you/selling' or query.get('status[0]') != ['IN_STOCK']:
                return False
            candidates = _extract_visible_listing_candidates(current, include_image_lookup=False)
            if candidates:
                return not any(_candidate_listing_status(candidate) == 'sold' for candidate in candidates)
            return current.execute_script("return /(?:you haven.t listed anything|you have no listings|no listings yet|no listings found|no results found)/i.test(document.querySelector('[role=main]')?.innerText || '');") is True

        WebDriverWait(driver, timeout, poll_frequency=0.5).until(ready)
        if not ready(driver):
            return False
        print('[Import] Available listings page is ready. Reading listings without Manage Listings.', flush=True)
        return True
    except Exception as exc:
        print(f'[Import] Available listings URL did not load: {type(exc).__name__}.', flush=True)
        return False
    finally:
        try:
            driver.set_page_load_timeout(original_page_timeout or 30)
        except Exception:
            pass
        if client_config is not None and original_client_timeout is not None:
            try:
                client_config.timeout = original_client_timeout
            except Exception:
                pass


@traced('IMPORT')
def import_listings_from_facebook(account, stop_event=None, progress_callback=None, replace_existing=False, only_new=False, use_visual_reader=None):
    """Merge live listings into saved backups; never discard absent listings."""
    if not account:
        raise ValueError("Account is required")
    stop_event = stop_event or threading.Event()
    existing = load_listings(account).get("listings") or []
    by_key = {}
    by_identity = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        key = _record_key(item)
        facebook_id = _extract_listing_id(item.get('url') or item.get('edit_url') or '')
        identity = ('backup', str(item.get('id'))) if listing_store.is_backup(item) else (('facebook', facebook_id) if facebook_id and not (item.get('source_deleted') and item.get('facebook_reposted')) else ('local', str(item.get('id'))))
        if identity in by_identity:
            kept = by_identity[identity]
            for field, value in item.items():
                if value and not kept.get(field):
                    kept[field] = value
            continue
        by_identity[identity] = item
        if key in by_key:
            # Preserve every existing backup even when older imports used
            # identical vehicle titles without a Facebook item URL.
            key += ':' + str(item['id'])
        by_key[key] = item
    processed_ids = set()
    skipped_sold = set()
    seen_cards = set()
    visible_keys = set()
    visible_ids = set()
    original_ids = {str(record['id']) for record in by_key.values()}
    sold_facebook_ids = set()
    # A Facebook card can appear through several nested DOM candidates and
    # again on later scroll scans. The first candidate containing a confirmed
    # metric is the card-local value; never let a later duplicate overwrite it.
    clicks_confirmed_this_import = set()
    driver = None
    listing_number = 0

    def register_driver(opened):
        nonlocal driver
        driver = opened
        _guard_import_driver(opened, stop_event)
        opened.set_page_load_timeout(AVAILABLE_URL_TIMEOUT)
        opened.set_script_timeout(AVAILABLE_URL_TIMEOUT)
        # Bound a hung ChromeDriver HTTP call as well as our polling loops.
        client_config = getattr(opened.command_executor, '_client_config', None)
        if client_config is not None:
            # Let browser navigation and page-readiness waits finish before the
            # HTTP connection times out. A shorter timeout strands the session.
            client_config.timeout = AVAILABLE_URL_TIMEOUT + 30
        with _active_import_drivers_lock:
            _active_import_drivers[account] = opened
        if stop_event.is_set():
            raise _ImportStopSignal()

    try:
        if stop_event.is_set():
            raise ListingImportCancelled()
        opened = _open_prelogged_facebook(account, force_headless=bool(only_new), on_driver_ready=register_driver)
        # Also support injected browser factories that do not invoke the hook.
        if driver is None:
            register_driver(opened)
        if stop_event.is_set():
            raise ListingImportCancelled()
        # Start with the filtered URL: it reaches the available inventory
        # directly without opening filters or loading the unfiltered rows.
        filtered = _open_available_url(driver, check_active_count=True)
        via_url = filtered
        if not filtered:
            driver.get(SELLING_URL)
            WebDriverWait(driver, AVAILABLE_FILTER_TIMEOUT, poll_frequency=0.5).until(
                EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
            )
            filtered = _set_available_filter(driver, check_active_count=True)
        filter_ready = filtered
        if filtered:
            method_name = 'Direct Available URL' if via_url else 'Available filter on Selling page'
            print(f'[Import] Listing extraction method: {method_name}.', flush=True)
            print('[Import] Reading available listings directly from Selling.', flush=True)
        else:
            diagnostic_event('IMPORT', 'FILTER_FALLBACK', account=account,
                             method='Manage Listings', reason='Available UI filter and URL did not become ready')
            print('[Import] Filter and URL methods timed out. Falling back to Manage Listings and skipping sold listings.', flush=True)
            driver.get(SELLING_URL)
            _check_active_listing_count(driver)
            _click_manage_listings(driver)

        count = _check_active_listing_count(driver)
        if count is not None:
            print(f'[Import] Facebook reports {count} active listings for {account}.', flush=True)

        def return_to_selling(current):
            nonlocal filter_ready, via_url, filtered
            filter_ready = False
            # Recovery also prefers the direct Available URL before the UI filter.
            filter_ready = _open_available_url(current, check_active_count=True)
            via_url = filter_ready
            if not filter_ready:
                current.get(SELLING_URL)
                WebDriverWait(current, AVAILABLE_FILTER_TIMEOUT, poll_frequency=0.5).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
                )
                filter_ready = _set_available_filter(current, check_active_count=True)
                via_url = False
            if not filter_ready:
                diagnostic_event('IMPORT', 'FILTER_RESTORE_FALLBACK', account=account,
                                 method='Manage Listings')
                print('[Import] Could not restore the Available filter. Continuing with Manage Listings and skipping sold listings.', flush=True)
                current.get(SELLING_URL)
                _click_manage_listings(current)
                filter_ready = True
                filtered = False
                via_url = False

        idle_rounds = 0
        no_new_rounds = 0
        previous_height = 0
        previous_position = -1
        while not stop_event.is_set() and idle_rounds < 4 and no_new_rounds < 4:
            new_count = 0
            opened_listing = False
            candidates = _extract_visible_listing_candidates(driver)
            scan_keys = {(_record_key(record), '' if filtered else record.get('listing_status', ''))
                         for candidate in candidates
                         if (record := _listing_from_candidate(candidate)) is not None}
            found_new_cards = bool(scan_keys - seen_cards)
            seen_cards.update(scan_keys)
            # Full rows carry status badges; nested links may omit them. Inspect
            # all candidates first so an unlabelled duplicate cannot import sold.
            for candidate in ([] if filtered else candidates):
                if _candidate_listing_status(candidate) != 'sold':
                    continue
                sold_record = _listing_from_candidate(candidate)
                if not sold_record:
                    continue
                sold_id = _extract_listing_id(sold_record.get('url') or '')
                # A title (even an exact title) is not evidence that a sold
                # vehicle is the same listing as an active vehicle. Never use
                # the fuzzy/title matching path for exclusion or removal.
                if sold_id:
                    sold_facebook_ids.add(sold_id)
                    for key, saved in list(by_key.items()):
                        if saved.get('source_deleted') and saved.get('facebook_reposted'):
                            continue  # Its URL identifies the old deleted source.
                        saved_id = _extract_listing_id(saved.get('url') or saved.get('edit_url') or '')
                        if saved_id != sold_id:
                            continue
                        if str(saved['id']) in original_ids:
                            saved.update(listing_status='sold', facebook_present=True, source_deleted=False)
                            visible_ids.add(str(saved['id']))
                        else:
                            visible_ids.discard(str(saved['id']))
                            by_key.pop(key)
                            print(f"[ListingImport] Excluded confirmed sold listing id={sold_id}; saved_count={len(by_key)}", flush=True)
                        new_count += 1
            for candidate in candidates:
                record = _listing_from_candidate(candidate)
                if not record:
                    continue
                # Use the same scoped full-row click parser as Refresh Clicks.
                # The metric and listing details therefore come from the same
                # DOM candidate; no later cross-listing rematch is required.
                confirmed_clicks = _confirmed_click_count(candidate)
                record["clicks"] = confirmed_clicks if confirmed_clicks is not None else 0
                record["clicks_confirmed"] = confirmed_clicks is not None
                if confirmed_clicks is not None:
                    record["clicks_checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                else:
                    record.pop("clicks_checked_at", None)
                facebook_id = _extract_listing_id(record.get('url') or '')
                if not filtered and (record.get('listing_status') == 'sold' or (facebook_id and facebook_id in sold_facebook_ids)):
                    sold_key = _record_key(record)
                    if sold_key not in skipped_sold:
                        print(f"[Import] Skipping sold listing: {record.get('year', '')} {record.get('make', '')} {record.get('model', '')}", flush=True)
                        skipped_sold.add(sold_key)
                    continue
                if filtered:
                    record['listing_status'] = 'available'
                key = _matching_record_key(by_key, record)
                visible_keys.add(key)
                if key not in by_key:
                    if any(item.get('id') == record['id'] for item in by_key.values()):
                        # An archived source and its new repost can share a title
                        # and generic card URL. Give the new live record its own ID.
                        record['id'] = hashlib.sha1(f"{record['id']}|{time.time_ns()}".encode()).hexdigest()[:20]
                    by_key[key] = record
                    if record.get("clicks_confirmed"):
                        clicks_confirmed_this_import.add(key)
                    new_count += 1
                    print(
                        f"[DeleteListings] imported {record.get('year','')} "
                        f"{record.get('make','')} {record.get('model','')} "
                        f"price={record.get('price') or '-'} "
                        f"url={record.get('url') or '-'} "
                        f"image={'yes' if record.get('image') else 'no'}",
                        flush=True,
                    )
                else:
                    # Enrich an existing deduplicated record with fields that
                    # may only appear after the listing card finishes loading.
                    existing_record = by_key[key]
                    existing_record['listing_status'] = record.get('listing_status') or existing_record.get('listing_status', '')
                    if existing_record.get('source_deleted') and existing_record.get('facebook_reposted'):
                        existing_record['url'] = record.get('url') or ''
                        existing_record['edit_url'] = ''
                    existing_record.update(source_deleted=False, facebook_present=True)
                    changed = False
                    for field in ("year", "make", "model", "price", "listed_date", "url", "images", "image"):
                        if record.get(field) and not existing_record.get(field):
                            existing_record[field] = record[field]
                            changed = True
                    cleaned_existing = clean_imported_listing_fields(existing_record)
                    for field in ('model', 'listed_date'):
                        if cleaned_existing.get(field) and cleaned_existing.get(field) != existing_record.get(field):
                            existing_record[field] = cleaned_existing[field]
                            changed = True
                    # If an older import saved the entire Marketplace row as
                    # the model, replace it with the newly parsed vehicle name.
                    if record.get('model') and _clean_card_title(existing_record.get('model')) != existing_record.get('model'):
                        existing_record['model'] = record['model']
                        changed = True
                    # Partial duplicate candidates are ignored. Only a DOM row
                    # that explicitly contains the click metric may write it.
                    if record.get("clicks_confirmed") and key not in clicks_confirmed_this_import:
                        existing_record["clicks"] = record["clicks"]
                        existing_record["clicks_confirmed"] = True
                        existing_record["clicks_checked_at"] = record["clicks_checked_at"]
                        clicks_confirmed_this_import.add(key)
                        changed = True
                    elif (
                        record.get("clicks_confirmed")
                        and key in clicks_confirmed_this_import
                        and existing_record.get("clicks") != record.get("clicks")
                    ):
                        print(
                            f"[DeleteListings] ignored duplicate click overwrite "
                            f"title={existing_record.get('year','')} {existing_record.get('make','')} "
                            f"{existing_record.get('model','')} kept={existing_record.get('clicks')} "
                            f"ignored={record.get('clicks')}",
                            flush=True,
                        )
                    if changed:
                        new_count += 1
                        print(
                            f"[DeleteListings] enriched {existing_record.get('year','')} "
                            f"{existing_record.get('make','')} {existing_record.get('model','')} "
                            f"url={existing_record.get('url') or '-'} "
                            f"image={'yes' if existing_record.get('image') else 'no'}",
                            flush=True,
                        )
                visible_ids.add(str(by_key[key]['id']))
                record = by_key[key]
                if str(record['id']) in processed_ids:
                    continue
                processed_ids.add(str(record['id']))
                listing_name = ' '.join(str(record.get(field) or '') for field in ('year', 'make', 'model')).strip()
                if only_new and record.get('details') and record.get('downloaded_images') and not record.get('detail_error') and not record.get('image_errors') and all(os.path.isfile(photo['path']) for photo in record['downloaded_images']):
                    continue
                listing_number += 1
                method_name = ('Direct Available URL' if via_url else 'Available filter on Selling page') if filtered else 'Manage Listings'
                print(f'[Import] Listing {listing_number}: extracting with {method_name}.', flush=True)
                print(f"[Import] Listing {listing_number}: clicking {listing_name} (not sold).", flush=True)
                save_listings(account, list(by_key.values()))
                scan_y = driver.execute_script('return window.scrollY;') or 0
                if progress_callback:
                    progress_callback({'total_count': len(by_key), 'message': f'Reading full details and photos {len(processed_ids)}/{len(by_key)}...'})
                try:
                    visual = True if use_visual_reader is None else use_visual_reader
                    with _listing_read_deadline(driver):
                        if visual:
                            from automation.visual_listing_reader import read_import_fallback_page
                            result = read_listing_from_manage(account, record, driver=driver, visual_fallback=lambda current: read_import_fallback_page(current, include_media=False), listing_card=candidate.get('element'), return_to_inventory=return_to_selling if filtered else None, public_media=True)
                        else:
                            result = read_listing_from_manage(account, record, driver=driver, listing_card=candidate.get('element'), return_to_inventory=return_to_selling if filtered else None, public_media=True)
                    details = result['posts'][0]
                    from automation.listing_videos import download as download_videos
                    videos, video_errors = download_videos(driver, account, record['id'],
                        details.get('video_urls') or [], IMPORT_DIR, stop_event)
                    # Preserve usable backups if Facebook no longer exposes a source.
                    previous = [video for video in record.get('downloaded_videos', [])
                                if video.get('path') and os.path.isfile(video['path'])]
                    record['downloaded_videos'] = videos or previous
                    record['video_errors'] = (details.get('video_errors') or []) + video_errors
                    print(f"[Import] Listing {listing_number}: downloading photos.", flush=True)
                    photos, errors = download_listing_images(driver, account, record['id'], details.get('image_urls') or [], stop_event)
                    errors = (details.get('photo_errors') or []) + errors
                    if (errors or not photos) and record.get('downloaded_images'):
                        # Keep media backups while still saving freshly read details.
                        photos = record['downloaded_images']
                    record.update(details=details, downloaded_images=photos, edit_url=result.get('edit_url'),
                                  details_imported_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                  detail_error='', image_errors=errors)
                    if not photos:
                        record['detail_error'] = 'No photos downloaded; re-import before reposting.'
                    from automation.reposting import REPOST_REQUIRED_FIELDS
                    missing_fields = [key for key in REPOST_REQUIRED_FIELDS if not str(details.get(key) or '').strip()]
                    uncertain_fields = [key for key, state in (details.get('field_states') or {}).items()
                                        if state in {'unreadable', 'conflict', 'needs_review'}]
                    video_issues = record.get('video_errors') or []
                    diagnostic_event('IMPORT_LISTING', 'REVIEW' if missing_fields or uncertain_fields or errors or not photos or video_issues else 'SAVED',
                        account=account, listing_id=record['id'], title=details.get('title', listing_name),
                        missing_fields=missing_fields, uncertain_fields=uncertain_fields,
                        photo_sources=len(details.get('image_urls') or []), photos_saved=len(photos), photo_errors=errors,
                        videos_saved=len(record.get('downloaded_videos') or []), video_issues=record.get('video_errors') or [])
                    if progress_callback and (missing_fields or uncertain_fields or errors or not photos or video_issues):
                        problems = []
                        if missing_fields:
                            problems.append('Empty required Facebook fields: ' + ', '.join(missing_fields))
                        if uncertain_fields:
                            problems.append('Facebook fields need review: ' + ', '.join(uncertain_fields))
                        if errors:
                            problems.append('Photo errors: ' + '; '.join(map(str, errors)))
                        if not photos:
                            problems.append('No photos were saved.')
                        if video_issues:
                            problems.append('Video errors: ' + '; '.join(map(str, video_issues)))
                        progress_callback({'failure': True, 'listing_id': record.get('id'),
                                           'title': details.get('title') or listing_name,
                                           'error': '\n'.join(problems), 'data': details,
                                           'photo_count': len(photos), 'video_count': len(record.get('downloaded_videos') or []),
                                           'photos': [item.get('name','') for item in photos],
                                           'videos': [item.get('name','') for item in record.get('downloaded_videos') or []],
                                           'message': 'Listing import needs review.'})

                except Exception as exc:
                    if stop_event.is_set():
                        raise ListingImportCancelled() from exc
                    record['detail_error'] = str(exc)
                    diagnostic_event('IMPORT_LISTING', 'FAILED', account=account, listing_id=record['id'], error=str(exc))
                    if progress_callback:
                        progress_callback({'failure': True, 'listing_id': record.get('id'), 'title': listing_name,
                                           'error': f'{type(exc).__name__}: {exc}', 'data': record.get('details') or record,
                                           'photo_count': len(record.get('downloaded_images') or []),
                                           'video_count': len(record.get('downloaded_videos') or []),
                                           'photos': [item.get('name','') for item in record.get('downloaded_images') or []],
                                           'videos': [item.get('name','') for item in record.get('downloaded_videos') or []],
                                           'message': 'Listing detail import failed.'})
                    print(f"[Import] Listing {listing_number}: could not finish taking details: {exc}. Keeping the backup and moving to the next listing.", flush=True)
                    if stop_event.is_set():
                        raise ListingImportCancelled()
                if stop_event.is_set():
                    raise ListingImportCancelled()
                if filtered and not filter_ready:
                    raise RuntimeError('The Available filter could not be restored. Saved listings were kept; restart Import.')
                save_listings(account, list(by_key.values()))
                if not record.get('detail_error'):
                    from automation.reposting import REPOST_REQUIRED_FIELDS
                    missing = [field.replace('_', ' ') for field in REPOST_REQUIRED_FIELDS
                               if not str(details.get(field) or '').strip()]
                    if missing or details.get('visual_read_requires_review') or errors or record.get('video_errors'):
                        message = 'Please check: ' + ', '.join(missing) if missing else 'Please review the saved details and photos.'
                        if record.get('video_errors'):
                            message += ' Video capture needs review; see the video issue in diagnostics.'
                        print(f"[Import] Listing {listing_number}: details saved. {message}", flush=True)
                    else:
                        print(f"[Import] Listing {listing_number}: details saved; {len(photos)} photos and {len(record.get('downloaded_videos') or [])} videos captured.", flush=True)
                print('[Import] Moving to the next listing.', flush=True)
                driver.execute_script('window.scrollTo(0, arguments[0]);', scan_y)
                # Returning to inventory already waits for listing cards.
                opened_listing = True
                break  # Re-scan fresh elements after returning from the editor.
            if new_count:
                save_listings(account, list(by_key.values()))
            if progress_callback:
                progress_callback({
                    "event": "listing",
                    "new_count": new_count,
                    "total_count": len(by_key),
                    "message": f"Found {len(by_key)} listings...",
                })

            if opened_listing:
                idle_rounds = 0
                no_new_rounds = 0
                continue

            no_new_rounds = 0 if found_new_cards or previous_height == 0 else no_new_rounds + 1

            try:
                print('[ListingImport] Current listings processed; scrolling for more.', flush=True)
                height = int(driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
                ) or 0)
                position = int(driver.execute_script('return window.scrollY;') or 0)
                driver.execute_script('window.scrollBy(0, Math.max(500, window.innerHeight * 0.8));')
            except Exception as exc:
                raise RuntimeError('Listing scan could not finish; missing-listing statuses were not changed.') from exc
            time.sleep(1)
            idle_rounds = idle_rounds + 1 if height <= previous_height and position <= previous_position else 0
            previous_height = height
            previous_position = position

        if stop_event.is_set():
            raise ListingImportCancelled()
        # Absence is meaningful only after a successful complete inventory scan.
        current_url = str(driver.current_url or '')
        if any(part in current_url for part in ('/login', '/checkpoint', '/recover')):
            raise RuntimeError('Facebook session needs verification; missing-listing statuses were not changed.')
        confirmed_empty = False
        if not visible_ids:
            confirmed_empty = driver.execute_script("return /(?:you haven.t listed anything|you have no listings|no listings yet|no listings found)/i.test(document.querySelector('[role=main]')?.innerText || '');") is True
        if (visible_ids or confirmed_empty) and not filtered and (idle_rounds >= 4 or confirmed_empty):
            checked_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            for record in by_key.values():
                if listing_store.is_backup(record):
                    continue
                present = str(record['id']) in visible_ids
                record['facebook_present'] = present
                record['facebook_presence_checked_at'] = checked_at
                if not present:
                    record['source_deleted'] = True
                    record['facebook_reposted'] = False
        if stop_event.is_set():
            raise ListingImportCancelled()
        save_listings(account, list(by_key.values()))
        print(f'[Import] Finished. Processed {listing_number} listings. Sold listings were skipped.', flush=True)
        return list(by_key.values())
    except _ImportStopSignal:
        raise ListingImportCancelled() from None
    except _ImportBrowserLost as exc:
        print('[Import] Chrome stopped responding or closed. Import stopped; saved listings were kept. Start Import again to open a fresh browser.', flush=True)
        raise RuntimeError('Chrome stopped responding or closed. Saved listings were kept. Start Import again to open a fresh browser.') from exc
    finally:
        with _active_import_drivers_lock:
            if _active_import_drivers.get(account) is driver:
                _active_import_drivers.pop(account, None)
        if driver:
            if stop_event.is_set():
                threading.Thread(target=_shutdown_import_driver, args=(driver,), daemon=True).start()
            else:
                try:
                    driver.quit()
                except Exception:
                    pass


def _confirmed_click_count(candidate):
    """Return a rendered click count, or None when Facebook has not shown it."""
    combined = "\n".join(
        str(candidate.get(field) or "").strip()
        for field in ("title", "text")
        if str(candidate.get(field) or "").strip()
    )
    return _parse_click_count(candidate.get("click_text"), combined)


def listing_click_refresh_total(account):
    """Count unique live records included by the Refresh Clicks job."""
    stored = load_listings(account).get('listings') or []
    live = [item for item in stored if isinstance(item, dict) and not listing_store.is_backup(item)]
    return len({str(item.get('id') or _record_key(item)) for item in live})


def _find_listing_search_box(driver):
    """Find Selling's listing search using its visible text before attributes."""
    lower = "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'"
    locators = (
        (By.XPATH,
         f"//span[contains(translate(normalize-space(.),{lower}),'search your listings')]"
         "/ancestor::label[1]//input | "
         f"//span[contains(translate(normalize-space(.),{lower}),'search your listings')]/following::input[1]"),
        (By.XPATH, f"//label[.//span[contains(translate(normalize-space(.),{lower}),'search your listings')]]//input"),
        (By.XPATH, f"//input[contains(translate(@placeholder,{lower}),'search your listings')]"),
        (By.XPATH, f"//input[contains(translate(@aria-label,{lower}),'search your listings')]"),
        (By.CSS_SELECTOR, "input[placeholder*='Search your listings']"),
        (By.CSS_SELECTOR, "input[aria-label*='Search your listings']"),
        (By.CSS_SELECTOR, "input[role='searchbox']"),
    )
    for locator in locators:
        try:
            for element in driver.find_elements(*locator):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue
    return False


def _click_saved_facebook_id(saved):
    return (_extract_listing_id(saved.get('url') or '') or
            _extract_listing_id(saved.get('edit_url') or ''))


def _click_candidate_matches_listing(candidate, saved):
    candidate_id = _extract_listing_id(candidate.get('href') or '')
    saved_id = _click_saved_facebook_id(saved)
    if candidate_id and saved_id:
        return candidate_id == saved_id
    current = _listing_from_candidate(candidate)
    if not current:
        return False
    normalize = lambda value: re.sub(r'\s+', ' ', str(value or '').strip().casefold())
    if normalize(current.get('year')) != normalize(saved.get('year')):
        return False
    if normalize(current.get('make')) != normalize(saved.get('make')):
        return False
    current_model = normalize(current.get('model'))
    saved_model = normalize(saved.get('model'))
    return bool(current_model and saved_model and
                (current_model == saved_model or current_model.startswith(saved_model + ' ')))


def _click_scan_candidates(driver):
    """Use full rows when available, retaining separate same-title listings."""
    candidates = _extract_visible_listing_candidates(driver, include_image_lookup=False)
    rows = [item for item in candidates if item.get('source') == 'full_row_dom']
    return rows or candidates


def _scan_listing_click_counts(driver, listings, progress_callback=None):
    """Read click metrics from inventory cards, leaving uncertain matches for search."""
    cards = {}

    def collect():
        for candidate in _click_scan_candidates(driver):
            clicks = _confirmed_click_count(candidate)
            element = candidate.get('element')
            if clicks is None or element is None:
                continue
            try:
                if not element.is_displayed():
                    continue
            except Exception:
                continue
            facebook_id = _extract_listing_id(candidate.get('href') or '')
            element_id = getattr(element, 'id', None)
            if not facebook_id and not element_id:
                continue
            identity = ('facebook', facebook_id) if facebook_id else (
                'element', element_id, str(candidate.get('title') or ''), str(candidate.get('text') or ''))
            if identity in cards and cards[identity]['clicks'] != clicks:
                cards[identity]['conflict'] = True
            elif identity not in cards:
                cards[identity] = {'candidate': dict(candidate), 'clicks': clicks, 'conflict': False}

    def position():
        return driver.execute_script(
            "return {y: window.scrollY || 0, height: Math.max(document.body.scrollHeight, "
            "document.documentElement.scrollHeight), viewport: window.innerHeight || 0};")

    driver.execute_script('window.scrollTo(0, 0);')
    collect()
    started = time.monotonic()
    saved_ids = [_click_saved_facebook_id(item) for item in listings]
    while time.monotonic() - started < 90:
        if (all(saved_ids) and len(set(saved_ids)) == len(saved_ids) and
                all(('facebook', facebook_id) in cards and not cards[('facebook', facebook_id)]['conflict']
                    for facebook_id in saved_ids)):
            break
        if progress_callback:
            progress_callback({'confirmed_count': 0, 'processed_count': 0, 'failed_count': 0,
                               'total_count': len(listings),
                               'message': f'Scanning Facebook listings... {len(cards)} cards read'})
        state = position()
        at_bottom = state['y'] + state['viewport'] >= state['height'] - 5
        if at_bottom:
            before = set(cards)

            def more_loaded(current):
                collect()
                new_state = position()
                return new_state['height'] > state['height'] or bool(set(cards) - before)

            try:
                WebDriverWait(driver, 5, poll_frequency=0.5).until(more_loaded)
            except TimeoutException:
                break
        driver.execute_script('window.scrollBy(0, Math.max(400, window.innerHeight * 0.8));')
        time.sleep(0.25)
        collect()

    possible = {index: [] for index in range(len(listings))}
    for card in cards.values():
        if card['conflict']:
            continue
        candidate = card['candidate']
        matches = [index for index, saved in enumerate(listings)
                   if _click_candidate_matches_listing(candidate, saved)]
        candidate_id = _extract_listing_id(candidate.get('href') or '')
        exact = [index for index in matches if candidate_id and candidate_id ==
                 _click_saved_facebook_id(listings[index])]
        if exact:
            matches = exact
        elif len(matches) > 1:
            price = _normalize_price((_listing_from_candidate(candidate) or {}).get('price'))
            if price and all(_normalize_price(listings[index].get('price') or
                                              (listings[index].get('details') or {}).get('price'))
                             for index in matches):
                matches = [index for index in matches if _normalize_price(
                    listings[index].get('price') or (listings[index].get('details') or {}).get('price')) == price]
        if len(matches) == 1:
            possible[matches[0]].append(card)

    resolved = {}
    for index, candidates in possible.items():
        saved_id = _click_saved_facebook_id(listings[index])
        exact = [card for card in candidates if saved_id and saved_id ==
                 _extract_listing_id(card['candidate'].get('href') or '')]
        choices = exact or candidates
        if len(choices) == 1:
            resolved[index] = choices[0]['clicks']
    return resolved


def _search_listing_click_metric(driver, saved, query, timeout=30, known_listings=None):
    search_box = WebDriverWait(driver, timeout, poll_frequency=0.4).until(_find_listing_search_box)
    search_box.click()
    try:
        search_box.clear()
    except Exception:
        search_box.send_keys(Keys.CONTROL, 'a')
        search_box.send_keys(Keys.BACKSPACE)
    search_box.send_keys(query)
    search_box.send_keys(Keys.ENTER)
    # Facebook debounces this field; allow the search request to start before
    # reading the result cards, while keeping the common path quick.
    time.sleep(0.8)

    def matched_result(current):
        candidates, seen = [], set()
        for candidate in _click_scan_candidates(current):
            facebook_id = _extract_listing_id(candidate.get('href') or '')
            element_id = getattr(candidate.get('element'), 'id', None)
            identity = ('facebook', facebook_id) if facebook_id else ('element', element_id or id(candidate))
            if identity not in seen:
                seen.add(identity)
                candidates.append(candidate)
        matches = [candidate for candidate in candidates
                   if _click_candidate_matches_listing(candidate, saved)
                   and _confirmed_click_count(candidate) is not None]
        if known_listings:
            safe_matches = []
            for candidate in matches:
                candidate_id = _extract_listing_id(candidate.get('href') or '')
                matching_saved = [item for item in known_listings
                                  if _click_candidate_matches_listing(candidate, item)]
                exact_saved = [item for item in matching_saved if candidate_id and candidate_id ==
                               _click_saved_facebook_id(item)]
                if exact_saved:
                    matching_saved = exact_saved
                elif len(matching_saved) > 1:
                    price = _normalize_price((_listing_from_candidate(candidate) or {}).get('price'))
                    if price and all(_normalize_price(item.get('price') or
                                                      (item.get('details') or {}).get('price'))
                                     for item in matching_saved):
                        matching_saved = [item for item in matching_saved if _normalize_price(
                            item.get('price') or (item.get('details') or {}).get('price')) == price]
                if len(matching_saved) == 1 and matching_saved[0].get('id') == saved.get('id'):
                    safe_matches.append(candidate)
            matches = safe_matches
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            saved_price = _normalize_price(saved.get('price') or (saved.get('details') or {}).get('price'))
            if saved_price:
                price_matches = [candidate for candidate in matches
                                 if _normalize_price(_listing_from_candidate(candidate).get('price')) == saved_price]
                if len(price_matches) == 1:
                    return price_matches[0]
            return {'_ambiguous': len(matches)}
        try:
            no_results = current.execute_script(
                "return /(?:no results|couldn.t find|no listings found)/i.test(document.querySelector('[role=main]')?.innerText || '');"
            )
            if no_results:
                return {'_not_found': True}
        except Exception:
            pass
        return False

    result = WebDriverWait(driver, timeout, poll_frequency=0.5).until(matched_result)
    if result.get('_ambiguous'):
        raise RuntimeError(f"Search returned {result['_ambiguous']} matching cards; cannot safely choose one")
    if result.get('_not_found'):
        raise LookupError('Facebook search returned no matching listing')
    return _confirmed_click_count(result)


def _open_listing_search_page(driver, prefer_url=True, log_prefix='RefreshClicks'):
    """Use the filtered URL first; open Manage Listings only when needed."""
    if prefer_url and _open_available_url(driver):
        try:
            WebDriverWait(driver, 60, poll_frequency=0.4).until(_find_listing_search_box)
            return 'Direct Available URL'
        except TimeoutException:
            print(f'[{log_prefix}] Available page has no usable search; opening Manage Listings.', flush=True)

    print(f'[{log_prefix}] Falling back to Selling / Manage Listings.', flush=True)
    try:
        driver.get(SELLING_URL)
    except TimeoutException:
        driver.execute_script('window.stop();')
    WebDriverWait(driver, 60, poll_frequency=0.4).until(
        EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))

    def manage_or_search(current):
        # Poll both labels together: missing controls must not cost two full
        # timeouts. Some accounts already expose search without a view switch.
        xpath = (
            "//*[@aria-label='Manage listings'] | "
            "//*[self::span or self::div][translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='manage listings']"
            "/ancestor::*[@role='button'][1]"
        )
        for control in current.find_elements(By.XPATH, xpath):
            try:
                if control.is_displayed() and control.is_enabled():
                    return ('manage', control)
            except Exception:
                continue
        search = _find_listing_search_box(current)
        return ('search', search) if search else False

    kind, control = WebDriverWait(driver, 60, poll_frequency=0.4).until(manage_or_search)
    if kind == 'manage':
        if not _click_element_safely(driver, control):
            raise RuntimeError('Manage Listings control could not be clicked')
        WebDriverWait(driver, 60, poll_frequency=0.4).until(_find_listing_search_box)
    return 'Manage Listings' if kind == 'manage' else 'Selling search'


def _open_click_refresh_search_page(driver, prefer_url=True):
    return _open_listing_search_page(driver, prefer_url=prefer_url)


@cancellable
def refresh_listing_clicks_from_facebook(account, progress_callback=None):
    """Scan inventory click counts, then search for listings not resolved there."""
    if not account:
        raise ValueError("Account is required")
    stored = load_listings(account).get("listings") or []
    listings = []
    seen_listing_ids = set()
    for item in stored:
        if isinstance(item, dict) and not listing_store.is_backup(item):
            identity = str(item.get('id') or _record_key(item))
            if identity not in seen_listing_ids:
                listings.append(clean_imported_listing_fields(item))
                seen_listing_ids.add(identity)
    print(
        f"[RefreshClicks] starting account={account} stored_listings={len(listings)} headless=yes",
        flush=True,
    )
    diagnostic_event('REFRESH_CLICKS', 'START', account=account, total=len(listings))
    if not listings:
        print(f"[RefreshClicks] stopped account={account}: no stored listings", flush=True)
        diagnostic_event('REFRESH_CLICKS', 'FINISHED', account=account, confirmed=0, total=0,
                         note='No saved listings to refresh')
        return []
    driver = None
    confirmed_keys = set()
    failed_keys = set()
    try:
        print(f"[RefreshClicks] opening saved Facebook session account={account}", flush=True)
        driver = _open_prelogged_facebook(account, force_headless=True)
        driver.set_page_load_timeout(AVAILABLE_URL_TIMEOUT)
        driver.set_script_timeout(60)
        client_config = getattr(driver.command_executor, '_client_config', None)
        if client_config is not None:
            client_config.timeout = AVAILABLE_URL_TIMEOUT + 30
        with _active_import_drivers_lock:
            _active_import_drivers[account] = driver
        search_method = _open_click_refresh_search_page(driver)
        via_url = search_method == 'Direct Available URL'
        print(f"[RefreshClicks] search ready via {search_method}", flush=True)
        diagnostic_event('REFRESH_CLICKS', 'SEARCH_PAGE_READY', account=account,
                         method=search_method,
                         current_url=str(getattr(driver, 'current_url', '') or '')[:300])
        started_at = time.monotonic()
        try:
            print('[RefreshClicks] scanning listing cards before searching missing listings', flush=True)
            scanned_clicks = _scan_listing_click_counts(driver, listings, progress_callback)
            print(f'[RefreshClicks] scan resolved {len(scanned_clicks)}/{len(listings)} listings', flush=True)
            diagnostic_event('REFRESH_CLICKS', 'SCAN_FINISHED', account=account,
                             matched=len(scanned_clicks), total=len(listings))
        except Exception as exc:
            print(f'[RefreshClicks] scan unavailable; using search: {type(exc).__name__}: {exc}', flush=True)
            diagnostic_event('REFRESH_CLICKS', 'SCAN_FAILED', account=account,
                             error_type=type(exc).__name__, error=str(exc))
            scanned_clicks = {}
        diagnostic_event('REFRESH_CLICKS', 'SEARCH_STARTED', account=account,
                         method='search_year_make_model', total=len(listings) - len(scanned_clicks), timeout_seconds=30)
        for index, target in enumerate(listings, start=1):
            key = str(target.get('id') or _record_key(target))
            title = str(target.get('title') or
                        f"{target.get('year', '')} {target.get('make', '')} {target.get('model', '')}").strip()
            query_parts = [str(target.get(field) or '').strip() for field in ('year', 'make', 'model')]
            query = ' '.join(part for part in query_parts if part)
            try:
                if index - 1 in scanned_clicks:
                    clicks = scanned_clicks[index - 1]
                else:
                    if not all(query_parts):
                        raise ValueError('Saved listing is missing its year, make, or model')
                    print(f"[RefreshClicks] searching missing listing {index}/{len(listings)}: {query}", flush=True)
                    diagnostic_event('REFRESH_CLICKS_LISTING', 'SEARCHING', account=account,
                                     listing_id=target.get('id'), title=title, query=query,
                                     index=index, total=len(listings), timeout_seconds=30)
                    try:
                        clicks = _search_listing_click_metric(driver, target, query, timeout=30, known_listings=listings)
                    except (LookupError, TimeoutException):
                        if not via_url:
                            raise
                        # Available can hide pending listings or omit click metrics.
                        via_url = False
                        search_method = _open_click_refresh_search_page(driver, prefer_url=False)
                        diagnostic_event('REFRESH_CLICKS', 'SEARCH_PAGE_FALLBACK',
                                         account=account, method=search_method, listing_id=target.get('id'))
                        clicks = _search_listing_click_metric(driver, target, query, timeout=30, known_listings=listings)
                previous_clicks = target.get('clicks')
                checked_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
                listing_store.update_fields(account, target['id'], {
                    'clicks': clicks, 'clicks_confirmed': True, 'clicks_checked_at': checked_at})
                confirmed_keys.add(key)
                diagnostic_event('REFRESH_CLICKS_LISTING', 'SAVED', account=account,
                                 listing_id=target.get('id'), title=title, query=query,
                                 clicks=clicks, previous_clicks=previous_clicks)
                print(f"[RefreshClicks] saved {index}/{len(listings)} id={target.get('id') or '-'} "
                      f"clicks={previous_clicks}->{clicks}", flush=True)
            except Exception as exc:
                failed_keys.add(key)
                reason = f'{type(exc).__name__}: {exc}'
                diagnostic_event('REFRESH_CLICKS_LISTING', 'SKIPPED', account=account,
                                 listing_id=target.get('id'), title=title, query=query,
                                 index=index, total=len(listings), error_type=type(exc).__name__,
                                 error=str(exc))
                print(f"[RefreshClicks] skipped {index}/{len(listings)} id={target.get('id') or '-'} "
                      f"query={query!r}: {reason}", flush=True)
            finally:
                if progress_callback:
                    progress_callback({
                        'confirmed_count': len(confirmed_keys),
                        'processed_count': index,
                        'failed_count': len(failed_keys),
                        'total_count': len(listings),
                        'listing_id': target.get('id'),
                        'title': title,
                        'status': 'failed' if key in failed_keys else 'success',
                        'error': reason if key in failed_keys else '',
                        'data': target,
                        'message': f"Refreshing clicks... {index}/{len(listings)}",
                    })
        print(
            f"[RefreshClicks] finished account={account} "
            f"confirmed={len(confirmed_keys)}/{len(listings)} skipped={len(failed_keys)} "
            f"elapsed={round(time.monotonic() - started_at, 1)}s",
            flush=True,
        )
        diagnostic_event('REFRESH_CLICKS', 'FINISHED' if not failed_keys else 'REVIEW',
                         account=account, confirmed=len(confirmed_keys), skipped=len(failed_keys),
                         total=len(listings), elapsed_seconds=round(time.monotonic() - started_at, 1))
        return listings
    except Exception as exc:
        print(f"[RefreshClicks] failed account={account}: {type(exc).__name__}: {exc}", flush=True)
        diagnostic_event('REFRESH_CLICKS', 'STOPPED', account=account,
                         confirmed=len(confirmed_keys), total=len(listings),
                         error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        with _active_import_drivers_lock:
            if _active_import_drivers.get(account) is driver:
                _active_import_drivers.pop(account, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        print(f"[RefreshClicks] browser closed account={account}", flush=True)


def _listing_text_key(record):
    return tuple(
        re.sub(r"[^a-z0-9]+", " ", str(record.get(field) or "").lower()).strip()
        for field in ("year", "make", "model")
    )


def _same_listing(stored, visible):
    """Match a stored selection to a Marketplace card by vehicle details."""
    details_only = bool(stored.get('_match_by_vehicle_details_only'))
    stored_url = str(stored.get("url") or "").split("?", 1)[0].rstrip("/").lower()
    visible_url = str(visible.get("url") or "").split("?", 1)[0].rstrip("/").lower()
    if not details_only and '/marketplace/item/' in stored_url and stored_url == visible_url:
        return True
    if not details_only and stored.get('_require_source_url'):
        return False
    if (details_only or stored.get('_source_listing_id')) and stored.get('price') and visible.get('price'):
        if _normalize_price(stored['price']) != _normalize_price(visible['price']):
            return False
    sy, sm, sx = _listing_text_key(stored)
    vy, vm, vx = _listing_text_key(visible)
    if not (sy and sm and sx and vy and vm and vx):
        return False
    if sy != vy or sm != vm:
        return False
    return sx == vx if details_only else sx == vx or sx.startswith(vx) or vx.startswith(sx)


def _verify_repost_source(driver, stored, timeout=10):
    source_id = stored.get('_source_listing_id')
    if not source_id:
        return True
    link = _find_edit_listing_link(driver, timeout=timeout, clickable=False)
    return bool(link and _extract_listing_id(link.get_attribute('href')) == str(source_id))


def _click_element_safely(driver, element):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    except Exception:
        pass
    try:
        element.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            return False


def _complete_facebook_delete_dialog(driver, timeout=25, on_delete_started=None):
    """Click Delete listing, then the enabled confirmation Delete button."""
    lower = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    upper = "abcdefghijklmnopqrstuvwxyz"
    # Prefer Facebook's stable semantic label for the trash icon. This is
    # scoped to the exact action and avoids accidentally clicking a status
    # control such as Mark as pending.
    aria_delete_xpath = (
        "//*[@role='button' and translate(normalize-space(@aria-label), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='delete marketplace listing']"
    )
    delete_listing_text = (
        "//*[self::span or self::div][translate(normalize-space(.), '"
        f"{lower}', '{upper}')='delete listing']"
    )
    # The icon is a sibling of the text span in Facebook's current markup,
    # so locate the text first and then the nearest container's button.
    delete_icon_xpath = (
        delete_listing_text
        + "/ancestor::div[.//*[@role='button']][1]//*[@role='button'][1]"
    )
    def _find_delete_control(current):
        for locator in (aria_delete_xpath, delete_icon_xpath):
            for control in current.find_elements(By.XPATH, locator):
                if control.is_displayed() and control.is_enabled():
                    return control
        return False

    menu_item = WebDriverWait(driver, timeout, poll_frequency=0.4).until(_find_delete_control)
    print("[DeleteListings] found Delete listing control", flush=True)
    if on_delete_started:
        on_delete_started()
    if not _click_element_safely(driver, menu_item):
        raise RuntimeError("Delete listing control could not be clicked")
    print("[DeleteListings] clicked Delete listing", flush=True)

    # Facebook can render a disabled background Delete button as well as the
    # enabled confirmation button. Exclude aria-disabled/hidden controls and
    # choose the last visible text-matching button.
    confirm_xpath = (
        "//*[@role='button' and not(@aria-disabled='true') and not(@aria-hidden='true') "
        "and (.//*[self::span or self::div][translate(normalize-space(.), '"
        f"{lower}', '{upper}')='delete'] or "
        "translate(normalize-space(@aria-label), '"
        f"{lower}', '{upper}')='delete')]"
    )

    def _enabled_confirmation(current_driver):
        candidates = current_driver.find_elements(By.XPATH, confirm_xpath)
        visible = [candidate for candidate in candidates if candidate.is_displayed() and candidate.is_enabled()]
        return visible[-1] if visible else False

    confirm_button = WebDriverWait(driver, timeout, poll_frequency=0.4).until(_enabled_confirmation)
    if not _click_element_safely(driver, confirm_button):
        raise RuntimeError("Delete confirmation could not be clicked")
    print("[DeleteListings] clicked confirmation Delete; waiting 5 seconds", flush=True)
    time.sleep(5)
    WebDriverWait(driver, timeout, poll_frequency=0.4).until(EC.invisibility_of_element(confirm_button))


def _open_delete_selling_page(driver):
    """Open an unfiltered Selling page before a search or fallback scan."""
    try:
        driver.get(SELLING_URL)
    except TimeoutException:
        driver.execute_script('window.stop();')
    WebDriverWait(driver, 60, poll_frequency=0.4).until(
        EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))


def _search_delete_candidate(driver, stored, stop_event, timeout=45):
    """Search Selling by year, make and model and return a matching card."""
    cleaned = clean_imported_listing_fields(stored)
    parts = [str(cleaned.get(field) or '').strip() for field in ('year', 'make', 'model')]
    if not all(parts):
        raise ValueError('Cannot search: the saved listing is missing its year, make or model.')
    query = ' '.join(parts)
    search_box = WebDriverWait(driver, timeout, poll_frequency=0.4).until(_find_listing_search_box)
    if stop_event.is_set():
        raise ListingImportCancelled()
    search_box.click()
    search_box.send_keys(Keys.CONTROL, 'a')
    search_box.send_keys(Keys.BACKSPACE)
    search_box.send_keys(query)
    search_box.send_keys(Keys.ENTER)
    print(f'[DeleteListings] Searching Selling for {query}', flush=True)
    # Allow Facebook's debounced search to begin before inspecting the cards.
    time.sleep(1)

    def matching_result(current):
        if stop_event.is_set():
            raise ListingImportCancelled()
        for candidate in extract_delete_candidates(current):
            visible = _listing_from_candidate(candidate)
            element = candidate.get('element')
            if visible and element is not None and _same_listing(cleaned, visible):
                try:
                    if element.is_displayed() and element.is_enabled():
                        return candidate
                except Exception:
                    continue
        try:
            if current.execute_script(
                "return /(?:no results|couldn.t find|no listings found)/i.test(document.querySelector('[role=main]')?.innerText || '');"
            ):
                return {'_not_found': True}
        except Exception:
            pass
        return False

    try:
        result = WebDriverWait(driver, timeout, poll_frequency=0.5).until(matching_result)
    except TimeoutException:
        return None
    return None if result.get('_not_found') else result


def _scan_delete_candidate(driver, stored, stop_event, timeout=120):
    """Use the existing Manage Listings card scan after search fails."""
    deadline = time.monotonic() + timeout
    idle_rounds = 0
    previous_height = -1
    while not stop_event.is_set() and idle_rounds < 8 and time.monotonic() < deadline:
        for candidate in extract_delete_candidates(driver):
            visible = _listing_from_candidate(candidate)
            element = candidate.get('element')
            if visible and element is not None and _same_listing(stored, visible):
                try:
                    if element.is_displayed() and element.is_enabled():
                        return candidate
                except Exception:
                    continue
        height = int(driver.execute_script(
            'return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);') or 0)
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        if stop_event.wait(1):
            break
        idle_rounds = idle_rounds + 1 if height <= previous_height else 0
        previous_height = height
    if stop_event.is_set():
        raise ListingImportCancelled()
    if time.monotonic() >= deadline and idle_rounds < 8:
        raise TimeoutException('Manage Listings scan timed out before it could finish.')
    return None


def _open_delete_candidate(driver, candidate, timeout=45):
    element = candidate['element']
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    WebDriverWait(driver, timeout, poll_frequency=0.4).until(
        lambda current: element.is_displayed() and element.is_enabled())
    if not _click_element_safely(driver, element):
        raise RuntimeError('The matching Facebook listing could not be opened.')


def _delete_selected_with_search(account, selected, stop_event, progress_callback):
    """Try Selling search first, then scan Manage Listings for each selection."""
    driver = None
    clicked = 0
    deleted_ids = []
    not_found = []

    def progress(stored, status, method, message, error=''):
        if progress_callback:
            progress_callback({'clicked': clicked, 'deleted': len(deleted_ids), 'total': len(selected),
                               'listing_id': str(stored.get('id') or ''), 'status': status,
                               'method': method, 'detail': message, 'error': error, 'message': message,
                               'confirmed_deleted': status == 'deleted'})

    try:
        driver = _open_prelogged_facebook(account)
        with _active_delete_drivers_lock:
            _active_delete_drivers[account] = driver
        driver.set_page_load_timeout(65)
        for stored in selected:
            if stop_event.is_set():
                break
            title = ' '.join(str(stored.get(field) or '').strip() for field in ('year', 'make', 'model')).strip()
            if not all(str(stored.get(field) or '').strip() for field in ('year', 'make', 'model')):
                progress(stored, 'failed', 'Selling search', 'Could not search this listing.',
                         'The saved listing is missing its year, make or model.')
                continue
            progress(stored, 'searching', 'Selling search', f'Searching Facebook for {title}...')
            candidate = None
            search_error = ''
            search_checked = False
            search_found = False
            action_started = [False]
            try:
                _open_delete_selling_page(driver)
                candidate = _search_delete_candidate(driver, stored, stop_event)
                search_checked = True
                search_found = candidate is not None
                if candidate is not None:
                    _open_delete_candidate(driver, candidate)
                    if stop_event.is_set():
                        break
                    _complete_facebook_delete_dialog(driver, timeout=45,
                        on_delete_started=lambda: action_started.__setitem__(0, True))
                    clicked += 1
                    deleted_ids.append(str(stored['id']))
                    progress(stored, 'deleted', 'Selling search', 'Deleted using Facebook search.')
                    diagnostic_event('DELETE_LISTING', 'DELETED', account=account,
                                     listing_id=stored.get('id'), title=title, method='Selling search')
                    continue
                search_error = 'Search did not find a matching listing.'
            except ListingImportCancelled:
                break
            except Exception as exc:
                if stop_event.is_set():
                    break
                search_error = f'{type(exc).__name__}: {exc}'
                if action_started[0]:
                    progress(stored, 'failed', 'Selling search', 'Facebook did not confirm deletion.', search_error)
                    diagnostic_event('DELETE_LISTING', 'FAILED', account=account,
                                     listing_id=stored.get('id'), title=title, method='Selling search', error=search_error)
                    continue
            print(f'[DeleteListings] Selling search failed for {title}: {search_error}. Trying Manage Listings.', flush=True)
            progress(stored, 'fallback', 'Manage Listings', f'Search did not finish; checking Manage Listings for {title}...')
            try:
                _open_delete_selling_page(driver)
                manage_opened = _click_manage_listings(driver, timeout=45, candidate_reader=extract_delete_candidates)
                if not manage_opened:
                    _wait_for_inventory(driver, timeout=45, candidate_reader=extract_delete_candidates)
                candidate = _scan_delete_candidate(driver, stored, stop_event)
                if candidate is None:
                    if not manage_opened:
                        raise RuntimeError('Could not open Manage Listings to confirm whether the listing is present.')
                    if search_found:
                        raise RuntimeError('Facebook search found the listing but could not open it; Manage Listings did not find it.')
                    if not search_checked:
                        raise RuntimeError(f'Selling search failed ({search_error}); Manage Listings did not find the listing.')
                    not_found.append(str(stored.get('id') or _record_key(stored)))
                    progress(stored, 'failed', 'Manage Listings', 'Listing not found by either method.',
                             'Listing not present on Facebook.')
                    diagnostic_event('DELETE_LISTING', 'NOT_FOUND', account=account,
                                     listing_id=stored.get('id'), title=title, search_error=search_error)
                    continue
                _open_delete_candidate(driver, candidate)
                if stop_event.is_set():
                    break
                clicked += 1
                _complete_facebook_delete_dialog(driver, timeout=45)
                deleted_ids.append(str(stored['id']))
                progress(stored, 'deleted', 'Manage Listings', 'Deleted using Manage Listings.')
                diagnostic_event('DELETE_LISTING', 'DELETED', account=account,
                                 listing_id=stored.get('id'), title=title, method='Manage Listings')
            except ListingImportCancelled:
                break
            except Exception as exc:
                if stop_event.is_set():
                    break
                error = f'{type(exc).__name__}: {exc}'
                progress(stored, 'failed', 'Manage Listings', 'Facebook deletion failed.', error)
                diagnostic_event('DELETE_LISTING', 'FAILED', account=account,
                                 listing_id=stored.get('id'), title=title, method='Manage Listings', error=error)
        stopped = stop_event.is_set()
        diagnostic_event('DELETE_LISTINGS', 'STOPPED' if stopped else
                         'FINISHED' if len(deleted_ids) == len(selected) else 'REVIEW',
                         account=account, total=len(selected), deleted=len(deleted_ids), not_found=len(not_found))
        return {'clicked': clicked, 'deleted': len(deleted_ids), 'deleted_ids': deleted_ids,
                'not_found': len(not_found), 'not_found_ids': not_found, 'stopped': stopped}
    finally:
        with _active_delete_drivers_lock:
            if _active_delete_drivers.get(account) is driver:
                _active_delete_drivers.pop(account, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


@cancellable
def delete_selected_listings_from_facebook(account, selected_listings, stop_event=None, progress_callback=None):
    """Find each selected listing by vehicle details and delete it on Facebook."""
    if not account:
        raise ValueError("Account is required")
    selected = [item for item in (selected_listings or []) if isinstance(item, dict)]
    if not selected:
        diagnostic_event('DELETE_LISTINGS', 'FINISHED', account=account, total=0, deleted=0)
        return {"clicked": 0, "not_found": 0}
    stop_event = stop_event or threading.Event()
    if all(item.get('_match_by_vehicle_details_only') for item in selected):
        diagnostic_event('DELETE_LISTINGS', 'START', account=account, total=len(selected))
        return _delete_selected_with_search(account, selected, stop_event, progress_callback)
    diagnostic_event('DELETE_LISTINGS', 'START', account=account, total=len(selected))
    driver = None
    clicked = 0
    deleted = 0
    deleted_ids = []
    not_found = []
    try:
        driver = _open_prelogged_facebook(account)
        with _active_delete_drivers_lock:
            _active_delete_drivers[account] = driver
        if stop_event.is_set():
            return {"clicked": 0, "deleted": 0, "deleted_ids": [], "not_found": 0, "stopped": True}
        driver.get(SELLING_URL)
        WebDriverWait(driver, 25).until(EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))
        _click_manage_listings(driver, timeout=25, candidate_reader=extract_delete_candidates)
        for stored in selected:
            if stop_event.is_set():
                break
            found = False
            idle_rounds = 0
            previous_height = -1
            while not stop_event.is_set() and idle_rounds < 4 and not found:
                retry_source = False
                candidates = extract_delete_candidates(driver)
                match_occurrence = max(1, int(stored.get("_match_occurrence") or 1))
                if match_occurrence > 1:
                    # Reposting creates an identical new card above the old
                    # source. Count only the one-per-checkbox DOM rows so the
                    # same card discovered by fallback selectors is not
                    # mistakenly treated as the second listing.
                    row_matches = []
                    for candidate in candidates:
                        if candidate.get("source") != "full_row_dom":
                            continue
                        visible = _listing_from_candidate(candidate)
                        if visible and _same_listing(stored, visible):
                            row_matches.append(candidate)
                    # Never accumulate occurrences across scans: Facebook may
                    # re-render the first/new row with a different Selenium
                    # element ID, which could otherwise count it twice. Only
                    # delete when a real second row exists in this same scan.
                    candidates = (
                        [row_matches[match_occurrence - 1]]
                        if len(row_matches) >= match_occurrence
                        else []
                    )
                    if not candidates:
                        print(
                            f"[DeleteListings] waiting for matching listing occurrence "
                            f"{match_occurrence}; visible_matches={len(row_matches)}; "
                            "first/new listing will not be deleted",
                            flush=True,
                        )
                for candidate in candidates:
                    visible = _listing_from_candidate(candidate)
                    if not visible or not _same_listing(stored, visible):
                        continue
                    element = candidate.get("element")
                    if element is None:
                        continue
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                        WebDriverWait(driver, 25).until(lambda d: element.is_displayed() and element.is_enabled())
                        try:
                            element.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", element)
                        print(
                            f"[DeleteListings] clicked {stored.get('year','')} {stored.get('make','')} "
                            f"{stored.get('model','')} â€” waiting for listing controls",
                            flush=True,
                        )
                        if not stored.get('_match_by_vehicle_details_only'):
                            WebDriverWait(driver, 25).until(EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'/marketplace/edit/') and contains(@href,'listing_id=')]")))
                        if stop_event.is_set():
                            break
                        if not _verify_repost_source(driver, stored, timeout=25):
                            # Repost source cleanup still requires the saved Facebook ID.
                            stored['_match_occurrence'] = match_occurrence + 1
                            driver.get(SELLING_URL)
                            WebDriverWait(driver, 25).until(EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))
                            _click_manage_listings(driver, timeout=25, candidate_reader=extract_delete_candidates)
                            retry_source = True
                            break
                        clicked += 1
                        _complete_facebook_delete_dialog(driver)
                        deleted += 1
                        if stored.get("id"):
                            deleted_ids.append(str(stored.get("id")))
                        diagnostic_event('DELETE_LISTING', 'DELETED', account=account,
                                         listing_id=stored.get('id'), title=stored.get('title') or
                                         f"{stored.get('year','')} {stored.get('make','')} {stored.get('model','')}")
                        found = True
                        if progress_callback:
                            progress_callback({"clicked": clicked, "deleted": deleted, "total": len(selected),
                                               "listing_id": str(stored.get('id') or ''), "confirmed_deleted": True,
                                               "message": "Listing deleted."})
                    except Exception as exc:
                        print(f"[DeleteListings] delete sequence failed for matched listing: {exc}", flush=True)
                        diagnostic_event('DELETE_LISTING', 'FAILED', account=account,
                                         listing_id=stored.get('id'), title=stored.get('title') or
                                         f"{stored.get('year','')} {stored.get('make','')} {stored.get('model','')}",
                                         error_type=type(exc).__name__, error=str(exc))
                        if progress_callback:
                            progress_callback({"clicked": clicked, "deleted": deleted, "total": len(selected),
                                               "listing_id": str(stored.get('id') or ''), "status": "failed",
                                               "error": str(exc), "message": "Facebook did not confirm this listing deletion."})
                        found = True
                    break
                if retry_source:
                    idle_rounds = 0
                    continue
                if found or stop_event.is_set():
                    break
                try:
                    height = int(driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);") or 0)
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                except Exception:
                    height = previous_height
                time.sleep(1)
                idle_rounds = idle_rounds + 1 if height <= previous_height else 0
                previous_height = height
            if not found:
                not_found.append(stored.get("id") or _record_key(stored))
                if progress_callback:
                    progress_callback({"clicked": clicked, "deleted": deleted, "total": len(selected),
                                       "listing_id": str(stored.get('id') or ''), "status": "not_found",
                                       "error": "Listing not present on Facebook.",
                                       "message": "Listing not found."})
                diagnostic_event('DELETE_LISTING', 'NOT_FOUND', account=account,
                                 listing_id=stored.get('id'), title=stored.get('title') or
                                 f"{stored.get('year','')} {stored.get('make','')} {stored.get('model','')}")
                print(
                    f"[DeleteListings] Listing not present on Facebook: {stored.get('year','')} "
                    f"{stored.get('make','')} {stored.get('model','')}", flush=True,
                )
            # The click may have opened the listing detail/editor. Return to
            # Manage Listings before matching the next selected item.
            if found and not stop_event.is_set() and len(not_found) + clicked < len(selected):
                driver.get(SELLING_URL)
                WebDriverWait(driver, 25).until(EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))
                _click_manage_listings(driver, timeout=25, candidate_reader=extract_delete_candidates)
        result = {
            "clicked": clicked,
            "deleted": deleted,
            "deleted_ids": deleted_ids,
            "not_found": len(not_found),
            "not_found_ids": [str(value) for value in not_found],
            "stopped": stop_event.is_set(),
        }
        complete = deleted == len(selected)
        diagnostic_event('DELETE_LISTINGS', 'STOPPED' if stop_event.is_set() else
                         'FINISHED' if complete else 'REVIEW', account=account, total=len(selected),
                         deleted=deleted, not_found=len(not_found), stopped=stop_event.is_set())
        return result
    except Exception as exc:
        diagnostic_event('DELETE_LISTINGS', 'STOPPED', account=account, total=len(selected),
                         deleted=deleted, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        with _active_delete_drivers_lock:
            if _active_delete_drivers.get(account) is driver:
                _active_delete_drivers.pop(account, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
