"""Import a user's Marketplace listings for the Delete Listings page."""

import hashlib
from automation.task_control import cancellable
import json
import os
import re
import threading
import subprocess
import time
from datetime import datetime, timezone

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from automation.reposting import _open_prelogged_facebook
from automation.runtime_paths import data_dir
from automation import listing_store
from automation.reposting import read_listing_from_manage, _find_edit_listing_link, _extract_listing_id, _normalize_price
from io import BytesIO
from PIL import Image
import requests


SELLING_URL = "https://www.facebook.com/marketplace/you/selling"
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
    try:
        driver.quit()
    except Exception:
        pass
    try:
        process = getattr(getattr(driver, "service", None), "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
    except Exception:
        pass
    return True


class ListingImportCancelled(RuntimeError):
    pass


class _ImportStopSignal(BaseException):
    """Escape browser retry handlers that catch Exception, like CancelledError."""


def _guard_import_driver(driver, stop_event):
    original = driver.execute

    def execute(command, params=None):
        if command not in {'quit', 'close'} and stop_event.is_set():
            raise _ImportStopSignal()
        return original(command, params)

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
    if record.get('source_deleted'):
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
        if any(re.match(r'^' + status + r'(?:$|\s*[·•|–—-]\s*|\s+on\s+)', badge)
               for badge in badges):
            return status
    return ''


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
    match = re.search(r"\b(19\d{2}|20\d{2})\s+([^\n|]+)", title_line)
    if match:
        year = match.group(1)
        words = match.group(2).strip().split()
        make = words[0] if words else ""
        model = " ".join(words[1:]).strip()
    else:
        year, make, model = "", "", title or combined
    if not (year or make or model or href):
        return None
    price_match = re.search(r"(?:[A-Z]{2})?\$\s*[\d,]+(?:\.\d{1,2})?", combined)
    listed_match = re.search(r"\bListed\s+on\s+([^\n\r|]+)", combined, re.IGNORECASE)
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
        "price": price_match.group(0).strip() if price_match else "",
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
            if existing.get('source_deleted') and existing.get('facebook_reposted'):
                continue  # The stored URL belongs to the deleted pre-repost source.
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
        if existing.get('source_deleted') and not pending_repost:
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


def _extract_visible_listing_candidates(driver, include_image_lookup=True):
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


def _click_manage_listings(driver, settle_seconds=5):
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
            element = WebDriverWait(driver, 20).until(EC.element_to_be_clickable(locator))
            try:
                element.click()
            except Exception:
                driver.execute_script("arguments[0].click();", element)
            print("[DeleteListings] Manage listings clicked", flush=True)
            time.sleep(max(0, float(settle_seconds)))
            return True
        except Exception:
            continue
    print("[DeleteListings] Manage listings control not found; scanning current page", flush=True)
    return False


@cancellable
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
        identity = ('facebook', facebook_id) if facebook_id and not (item.get('source_deleted') and item.get('facebook_reposted')) else ('local', str(item.get('id')))
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
    visible_keys = set()
    visible_ids = set()
    original_ids = {str(record['id']) for record in by_key.values()}
    sold_keys = set()
    # A Facebook card can appear through several nested DOM candidates and
    # again on later scroll scans. The first candidate containing a confirmed
    # metric is the card-local value; never let a later duplicate overwrite it.
    clicks_confirmed_this_import = set()
    driver = None

    def register_driver(opened):
        nonlocal driver
        driver = opened
        _guard_import_driver(opened, stop_event)
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
        driver.get(SELLING_URL)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
        )
        _click_manage_listings(driver)

        idle_rounds = 0
        previous_height = 0
        while not stop_event.is_set() and idle_rounds < 4:
            new_count = 0
            candidates = _extract_visible_listing_candidates(driver)
            # Full rows carry status badges; nested links may omit them. Inspect
            # all candidates first so an unlabelled duplicate cannot import sold.
            for candidate in candidates:
                if _candidate_listing_status(candidate) != 'sold':
                    continue
                sold_record = _listing_from_candidate(candidate)
                if not sold_record:
                    continue
                key = _matching_record_key(by_key, sold_record)
                sold_keys.add(key)
                saved = by_key.get(key)
                if saved and str(saved['id']) in original_ids:
                    # Keep previous backups without rereading sold details/photos.
                    saved.update(listing_status='sold', facebook_present=True, source_deleted=False)
                    visible_ids.add(str(saved['id']))
                elif saved:
                    # A partial card from an earlier scroll may lack its badge.
                    visible_ids.discard(str(saved['id']))
                    by_key.pop(key)
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
                key = _matching_record_key(by_key, record)
                if key in sold_keys or record.get('listing_status') == 'sold':
                    continue
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
            if new_count:
                save_listings(account, list(by_key.values()))
            if progress_callback:
                progress_callback({
                    "event": "listing",
                    "new_count": new_count,
                    "total_count": len(by_key),
                    "message": f"Found {len(by_key)} listings...",
                })

            try:
                height = int(driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
                ) or 0)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception as exc:
                raise RuntimeError('Listing scan could not finish; missing-listing statuses were not changed.') from exc
            time.sleep(1)
            idle_rounds = idle_rounds + 1 if height <= previous_height else 0
            previous_height = height

        if stop_event.is_set():
            raise ListingImportCancelled()
        # Collect cards first so opening edit pages cannot disrupt scroll discovery.
        for index, record in enumerate(by_key.values(), 1):
            if stop_event.is_set():
                raise ListingImportCancelled()
            if str(record['id']) not in visible_ids or record.get('source_deleted') or record.get('listing_status') == 'sold':
                continue
            if only_new and record.get('details') and record.get('downloaded_images') and not record.get('detail_error') and not record.get('image_errors') and all(os.path.isfile(photo['path']) for photo in record['downloaded_images']):
                continue
            if progress_callback:
                progress_callback({'total_count': len(by_key), 'message': f'Reading full details and photos {index}/{len(by_key)}...'})
            try:
                import sys
                visual = not getattr(sys, 'frozen', False) if use_visual_reader is None else use_visual_reader
                if visual:
                    from automation.visual_listing_reader import read_import_fallback_page
                    result = read_listing_from_manage(account, record, driver=driver, visual_fallback=read_import_fallback_page)
                else:
                    result = read_listing_from_manage(account, record, driver=driver)
                details = result['posts'][0]
                photos, errors = download_listing_images(driver, account, record['id'], details.get('image_urls') or [], stop_event)
                if (errors or not photos) and record.get('downloaded_images'):
                    # A transient download error must not erase an existing backup.
                    raise RuntimeError('Photo refresh failed; previously saved details and photos were kept.')
                record.update(details=details, downloaded_images=photos, edit_url=result.get('edit_url'),
                              details_imported_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              detail_error='', image_errors=errors)
                if not photos:
                    record['detail_error'] = 'No photos downloaded; re-import before reposting.'
            except Exception as exc:
                if stop_event.is_set():
                    raise ListingImportCancelled() from exc
                record['detail_error'] = str(exc)
            if stop_event.is_set():
                raise ListingImportCancelled()
            save_listings(account, list(by_key.values()))
        # Absence is meaningful only after a successful complete inventory scan.
        current_url = str(driver.current_url or '')
        if any(part in current_url for part in ('/login', '/checkpoint', '/recover')):
            raise RuntimeError('Facebook session needs verification; missing-listing statuses were not changed.')
        confirmed_empty = False
        if not visible_ids:
            confirmed_empty = driver.execute_script("return /(?:you haven.t listed anything|you have no listings|no listings yet|no listings found)/i.test(document.querySelector('[role=main]')?.innerText || '');") is True
        if visible_ids or confirmed_empty:
            checked_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            for record in by_key.values():
                present = str(record['id']) in visible_ids
                record['facebook_present'] = present
                record['facebook_presence_checked_at'] = checked_at
                if not present:
                    record['source_deleted'] = True
                    record['facebook_reposted'] = False
        if stop_event.is_set():
            raise ListingImportCancelled()
        save_listings(account, list(by_key.values()))
        return list(by_key.values())
    except _ImportStopSignal:
        raise ListingImportCancelled() from None
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


@cancellable
def refresh_listing_clicks_from_facebook(account, progress_callback=None):
    """Refresh only confirmed click counts and persist every listing immediately."""
    if not account:
        raise ValueError("Account is required")
    stored = load_listings(account).get("listings") or []
    listings = [dict(item) for item in stored if isinstance(item, dict)]
    print(
        f"[RefreshClicks] starting account={account} stored_listings={len(listings)} headless=yes",
        flush=True,
    )
    if not listings:
        print(f"[RefreshClicks] stopped account={account}: no stored listings", flush=True)
        return []
    by_key = {_record_key(item): item for item in listings}
    driver = None
    confirmed_keys = set()
    try:
        print(f"[RefreshClicks] opening saved Facebook session account={account}", flush=True)
        driver = _open_prelogged_facebook(account, force_headless=True)
        with _active_import_drivers_lock:
            _active_import_drivers[account] = driver
        print(f"[RefreshClicks] opening selling page url={SELLING_URL}", flush=True)
        driver.get(SELLING_URL)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
        )
        print(f"[RefreshClicks] selling page ready account={account}", flush=True)
        _click_manage_listings(driver, settle_seconds=1.5)

        idle_rounds = 0
        previous_height = 0
        scan_round = 0
        while idle_rounds < 3 and len(confirmed_keys) < len(listings):
            scan_round += 1
            candidates = _extract_visible_listing_candidates(driver, include_image_lookup=False)
            print(
                f"[RefreshClicks] scan={scan_round} candidates={len(candidates)} "
                f"confirmed={len(confirmed_keys)}/{len(listings)}",
                flush=True,
            )
            missing_metric = 0
            unmatched = 0
            for candidate in candidates:
                clicks = _confirmed_click_count(candidate)
                if clicks is None:
                    missing_metric += 1
                    continue
                record = _listing_from_candidate(candidate)
                if not record:
                    unmatched += 1
                    continue
                key = _matching_record_key(by_key, record)
                target = by_key.get(key)
                if target is None:
                    unmatched += 1
                    print(
                        f"[RefreshClicks] no stored match title="
                        f"{record.get('year','')} {record.get('make','')} {record.get('model','')} "
                        f"clicks={clicks}",
                        flush=True,
                    )
                    continue
                if key in confirmed_keys:
                    continue
                confirmed_keys.add(key)
                previous_clicks = target.get("clicks")
                changed = previous_clicks != clicks
                target["clicks"] = clicks
                target["clicks_confirmed"] = True
                target["clicks_checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                # Save after every confirmed row so the polling UI updates
                # progressively instead of waiting for the entire page scan.
                listing_store.update_fields(account, target['id'], {
                    'clicks': clicks, 'clicks_confirmed': True, 'clicks_checked_at': target['clicks_checked_at']})
                print(
                    f"[RefreshClicks] confirmed id={target.get('id') or '-'} "
                    f"title={target.get('year','')} {target.get('make','')} {target.get('model','')} "
                    f"clicks={previous_clicks}->{clicks} changed={'yes' if changed else 'no'} "
                    f"progress={len(confirmed_keys)}/{len(listings)}",
                    flush=True,
                )
                if progress_callback:
                    progress_callback({
                        "confirmed_count": len(confirmed_keys),
                        "total_count": len(listings),
                        "listing_id": target.get("id"),
                        "clicks": clicks,
                        "changed": changed,
                        "message": f"Refreshing clicks... {len(confirmed_keys)}/{len(listings)}",
                    })
            print(
                f"[RefreshClicks] scan={scan_round} missing_click_text={missing_metric} "
                f"unmatched={unmatched}",
                flush=True,
            )

            try:
                height = int(driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
                ) or 0)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                height = previous_height
            time.sleep(0.7)
            idle_rounds = idle_rounds + 1 if height <= previous_height else 0
            previous_height = height
        print(
            f"[RefreshClicks] finished account={account} "
            f"confirmed={len(confirmed_keys)}/{len(listings)} scans={scan_round}",
            flush=True,
        )
        return listings
    except Exception as exc:
        print(f"[RefreshClicks] failed account={account}: {type(exc).__name__}: {exc}", flush=True)
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
    """Match a stored selection to a Marketplace card without CSS classes."""
    stored_url = str(stored.get("url") or "").split("?", 1)[0].rstrip("/").lower()
    visible_url = str(visible.get("url") or "").split("?", 1)[0].rstrip("/").lower()
    if '/marketplace/item/' in stored_url and stored_url == visible_url:
        return True
    if stored.get('_require_source_url'):
        return False
    if stored.get('_source_listing_id') and stored.get('price') and visible.get('price'):
        if _normalize_price(stored['price']) != _normalize_price(visible['price']):
            return False
    sy, sm, sx = _listing_text_key(stored)
    vy, vm, vx = _listing_text_key(visible)
    if not (sy and sm and sx and vy and vm and vx):
        return False
    if sy != vy or sm != vm:
        return False
    return sx == vx or sx.startswith(vx) or vx.startswith(sx)


def _verify_repost_source(driver, stored):
    source_id = stored.get('_source_listing_id')
    if not source_id:
        return True
    link = _find_edit_listing_link(driver, timeout=10, clickable=False)
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


def _complete_facebook_delete_dialog(driver):
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
    try:
        menu_item = WebDriverWait(driver, 15).until(
            EC.visibility_of_element_located((By.XPATH, aria_delete_xpath))
        )
        print("[DeleteListings] found Delete marketplace listing icon by aria-label", flush=True)
    except Exception:
        menu_item = WebDriverWait(driver, 15).until(
            EC.visibility_of_element_located((By.XPATH, delete_icon_xpath))
        )
        print("[DeleteListings] found Delete listing icon by span text fallback", flush=True)
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

    confirm_button = WebDriverWait(driver, 15).until(_enabled_confirmation)
    if not _click_element_safely(driver, confirm_button):
        raise RuntimeError("Delete confirmation could not be clicked")
    print("[DeleteListings] clicked confirmation Delete; waiting 5 seconds", flush=True)
    time.sleep(5)
    WebDriverWait(driver, 15).until(EC.invisibility_of_element(confirm_button))


@cancellable
def delete_selected_listings_from_facebook(account, selected_listings, stop_event=None, progress_callback=None):
    """Find each selected source, verify its ID and complete Facebook deletion."""
    if not account:
        raise ValueError("Account is required")
    selected = [item for item in (selected_listings or []) if isinstance(item, dict)]
    if not selected:
        return {"clicked": 0, "not_found": 0}
    stop_event = stop_event or threading.Event()
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
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))
        _click_manage_listings(driver)
        for stored in selected:
            if stop_event.is_set():
                break
            found = False
            idle_rounds = 0
            previous_height = -1
            while not stop_event.is_set() and idle_rounds < 4 and not found:
                retry_source = False
                candidates = _extract_visible_listing_candidates(driver)
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
                        WebDriverWait(driver, 15).until(lambda d: element.is_displayed() and element.is_enabled())
                        try:
                            element.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", element)
                        print(
                            f"[DeleteListings] clicked {stored.get('year','')} {stored.get('make','')} "
                            f"{stored.get('model','')} — waiting 10 seconds before next step",
                            flush=True,
                        )
                        if stop_event.wait(10):
                            break
                        if not _verify_repost_source(driver, stored):
                            # Several listings can share a title. Try the next
                            # card, always verifying its Edit listing ID first.
                            stored['_match_occurrence'] = match_occurrence + 1
                            driver.get(SELLING_URL)
                            WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))
                            _click_manage_listings(driver)
                            retry_source = True
                            break
                        clicked += 1
                        _complete_facebook_delete_dialog(driver)
                        deleted += 1
                        if stored.get("id"):
                            deleted_ids.append(str(stored.get("id")))
                        found = True
                        if progress_callback:
                            progress_callback({"clicked": clicked, "deleted": deleted, "total": len(selected), "message": "Listing deleted."})
                    except Exception as exc:
                        print(f"[DeleteListings] delete sequence failed for matched listing: {exc}", flush=True)
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
                print(
                    f"[DeleteListings] listing not found: {stored.get('year','')} "
                    f"{stored.get('make','')} {stored.get('model','')}", flush=True,
                )
            # The click may have opened the listing detail/editor. Return to
            # Manage Listings before matching the next selected item.
            if found and not stop_event.is_set() and len(not_found) + clicked < len(selected):
                driver.get(SELLING_URL)
                WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, "//div[@role='main']")))
                _click_manage_listings(driver)
        return {
            "clicked": clicked,
            "deleted": deleted,
            "deleted_ids": deleted_ids,
            "not_found": len(not_found),
            "stopped": stop_event.is_set(),
        }
    finally:
        with _active_delete_drivers_lock:
            if _active_delete_drivers.get(account) is driver:
                _active_delete_drivers.pop(account, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
