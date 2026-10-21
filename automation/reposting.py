import os
import pickle
import re
import time
from urllib.parse import urlparse, parse_qs, urlsplit

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from automation.browser_settings import apply_background_mode_option
from automation.chromedriver import build_chrome_driver
from automation.runtime_paths import sessions_dir


SESSION_DIR = sessions_dir()


EDIT_FIELDS = {
    'vehicle_type': 'Vehicle type', 'year': 'Year', 'make': 'Make', 'model': 'Model',
    'mileage': 'Mileage', 'price': 'Price', 'body_style': 'Body style',
    'exterior_color': 'Exterior color', 'interior_color': 'Interior color',
    'vehicle_condition': 'Vehicle condition', 'fuel_type': 'Fuel type',
    'transmission': 'Transmission', 'description': 'Description', 'location': 'Location',
    'clean_title': 'This vehicle has a clean title.',
}
ITEM_EDIT_FIELDS = {'title': 'Title', 'category': 'Category', 'item_condition': 'Condition', 'sku': 'SKU'}
EXTRA_VEHICLE_FIELDS = {'vin': 'VIN', 'trim': 'Trim'}
REPOST_REQUIRED_FIELDS = ('title', 'vehicle_type', 'year', 'make', 'model', 'mileage', 'price',
                          'body_style',
                          'fuel_type', 'transmission', 'description', 'location')


def read_edit_form_snapshot(driver):
    """Read actual controls once, including empty/absent/disabled field states."""
    return driver.execute_script('''
        const fields = arguments[0];
        const norm = s => (s || '').replace(/colour/gi, 'color').replace(/\\s+/g, ' ').trim().toLowerCase();
        const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        const roots = [...document.querySelectorAll('[role="dialog"],[role="main"]')].filter(visible);
        const root = roots.reverse().find(el => el.querySelector('input:not([type="hidden"]),textarea,[contenteditable="true"]')) || document;
        const containers = [...root.querySelectorAll('label,[role="combobox"]')];
        const controls = [...root.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"]')];
        const result = {};
        for (const [key, label] of Object.entries(fields)) {
            const target = norm(label);
            const matchesLabel = value => norm(value) === target ||
                (key === 'vehicle_condition' && norm(value) === 'condition');
            let container = containers.find(el => matchesLabel(el.getAttribute('aria-label')) ||
                [...el.querySelectorAll('span')].some(span => matchesLabel(span.textContent)));
            const controlLabel = el => el.getAttribute('aria-label') || (el.getAttribute('aria-labelledby') || '').split(/\\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ') || [...(el.labels || [])].map(label => label.textContent).join(' ');
            let control = controls.find(el => matchesLabel(controlLabel(el)));
            if (!control && key === 'clean_title') {
                control = controls.find(el => el.type === 'checkbox' &&
                    norm(el.closest('label')?.textContent || el.parentElement?.parentElement?.textContent).includes(target));
                if (!control) control = [...root.querySelectorAll('[role="checkbox"]')].find(el =>
                    norm(controlLabel(el) || el.parentElement?.parentElement?.textContent).includes(target));
            }
            if (!container && control) container = control.closest('label') || control;
            if (!control && container) control = container.querySelector('input:not([type="hidden"]),textarea,[contenteditable="true"]');
            if (!control && key === 'description') control = root.querySelector('textarea');
            if (!container && control) container = control.closest('label') || control;
            if (!container && !control) { result[key] = {value:'',state:'unavailable'}; continue; }
            let value = '';
            if (control) value = control.type === 'checkbox' || control.getAttribute('role') === 'checkbox' ? (control.checked || control.getAttribute('aria-checked') === 'true' ? 'Yes' : 'No') : (control.value ?? control.innerText ?? '');
            else {
                const clone = container.cloneNode(true);
                for (const span of [...clone.querySelectorAll('span')]) {
                    if (matchesLabel(span.textContent)) span.remove();
                }
                value = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
                if (norm(value) === target) value = '';
            }
            if (/^(select|choose)(\\s|$)/i.test(value)) value = '';
            const disabled = !!control?.disabled || container.getAttribute('aria-disabled') === 'true';
            result[key] = {value:String(value).trim(), state:disabled ? 'disabled' : String(value).trim() ? 'read' : 'empty'};
        }
        return result;
    ''', dict(EDIT_FIELDS, **ITEM_EDIT_FIELDS, **EXTRA_VEHICLE_FIELDS)) or {}


def _item_edit_details(snapshot, listing, image_urls):
    """Read the item-style editor without inventing absent vehicle controls."""
    if (snapshot.get('title', {}).get('state', 'unavailable') == 'unavailable'
            or snapshot.get('category', {}).get('state', 'unavailable') == 'unavailable'
            or any(snapshot.get(field, {}).get('state', 'unavailable') != 'unavailable'
                   for field in ('vehicle_type', 'mileage', 'body_style', 'transmission'))):
        return None
    post = {field: value.get('value') or '' for field, value in snapshot.items()}
    for field in ('year', 'make', 'model'):
        post[field] = post.get(field) or str(listing.get(field) or '')
    post.update(listing_format='item', image_urls=image_urls,
                field_states={field: value.get('state', 'unavailable') for field, value in snapshot.items()})
    return post


def validate_repost_fields(post, edited_fields=None):
    """Fail before deleting a source when vehicle data is incomplete/unverified."""
    edited_fields = edited_fields or {}
    required = REPOST_REQUIRED_FIELDS
    missing = [field for field in required if not str(post.get(field) or '').strip()]
    uncertain = [field for field, state in (post.get('field_states') or {}).items()
                 if (field in EDIT_FIELDS or (field in EXTRA_VEHICLE_FIELDS and post.get(field))) and field not in edited_fields
                 and (field not in {'exterior_color', 'interior_color', 'vehicle_condition', 'vin', 'trim'} or str(post.get(field) or '').strip())
                 and state in {'unreadable', 'conflict', 'needs_review'}]
    if missing or uncertain:
        fields = ', '.join(dict.fromkeys(missing + uncertain))
        raise ValueError(f'Vehicle details are incomplete or need review: {fields}. The Facebook source was not deleted.')
    try:
        mileage = float(str(post['mileage']).replace(',', ''))
    except (TypeError, ValueError):
        mileage = 0
    if mileage <= 300:
        raise ValueError('Mileage must be greater than 300 for vehicle posting. The Facebook source was not deleted.')


def wait_for_edit_form_ready(driver, timeout=30):
    """Require populated, stable form controls rather than just an edit URL."""
    previous = None
    stable_since = None

    def ready(current):
        nonlocal previous, stable_since
        snapshot = read_edit_form_snapshot(current)
        price = snapshot.get('price', {})
        if not str(price.get('value') or '').strip() or price.get('state') not in {'read', 'disabled'}:
            previous, stable_since = None, None
            return False
        # A price alone can arrive before the rest of the form.
        if sum(value.get('state') != 'unavailable' for value in snapshot.values()) < 3:
            previous, stable_since = None, None
            return False
        if snapshot != previous:
            previous, stable_since = snapshot, time.monotonic()
            return False
        return snapshot if time.monotonic() - stable_since >= 1.5 else False

    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.5).until(ready)
    except Exception as exc:
        raise RuntimeError('The Facebook edit form did not finish loading or its fields could not be detected. Saved details were not replaced; retry the import.') from exc


def _combobox_label_xpath(label_text):
    normalized = str(label_text or "").strip()
    variants = {normalized}
    if normalized:
        variants.add(normalized.replace("color", "colour"))
        variants.add(normalized.replace("colour", "color"))
    clauses = []
    for v in sorted(variants):
        if v:
            safe = v.replace('"', '\\"')
            clauses.append(f".//span[normalize-space()=\"{safe}\"]")
    if not clauses:
        clauses = [".//span[normalize-space()='']"]
    return f"//label[@role='combobox'][{' or '.join(clauses)}]"


def _safe_input_value(driver, label_text):
    try:
        candidates = WebDriverWait(driver, 20).until(
            EC.presence_of_all_elements_located(
                (
                    By.XPATH,
                    f"//label[.//span[normalize-space()='{label_text}']]//input[not(@type='hidden')]",
                )
            )
        )
    except Exception:
        return ""

    # Prefer visible inputs with a non-empty value.
    best_value = ""
    for el in candidates:
        try:
            if not el.is_displayed():
                continue
        except Exception:
            continue
        try:
            value = (el.get_attribute("value") or "").strip()
        except Exception:
            value = ""
        if value:
            return value
        if not best_value:
            best_value = value

    return (best_value or "").strip()


def _safe_combobox_text(driver, label_text):
    try:
        label = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, _combobox_label_xpath(label_text)))
        )
        text = " ".join((label.text or "").split())
        if not text:
            return ""
        lower = text.lower()
        key = label_text.strip().lower()
        if lower.startswith(key):
            text = text[len(label_text):].strip(" :")
        return text
    except Exception:
        return ""


def _safe_description(driver):
    xpaths = [
        "//label[.//span[normalize-space()='Description']]//textarea",
        "//textarea[@aria-label='Description']",
        "//textarea",
    ]
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, xp))
            )
            val = (el.get_attribute("value") or el.text or "").strip()
            if val:
                return val
        except Exception:
            continue
    return ""


def _safe_image_urls(driver):
    def _img_src(img_el):
        try:
            return (
                driver.execute_script(
                    "return arguments[0].currentSrc || arguments[0].src || '';",
                    img_el,
                )
                or ""
            ).strip()
        except Exception:
            return (img_el.get_attribute("src") or "").strip()

    def _srcset_best(img_el):
        try:
            srcset = (img_el.get_attribute("srcset") or "").strip()
            if not srcset:
                return ""
            parts = [p.strip() for p in srcset.split(",") if p.strip()]
            best_url = ""
            best_score = -1
            for part in parts:
                bits = part.split()
                if not bits:
                    continue
                url = bits[0].strip()
                score = 1
                if len(bits) > 1:
                    token = bits[1].lower()
                    m = re.match(r"(\d+)", token)
                    if m:
                        score = int(m.group(1))
                if score >= best_score:
                    best_score = score
                    best_url = url
            return best_url
        except Exception:
            return ""

    def _is_large_image(img_el):
        try:
            w = driver.execute_script("return arguments[0].naturalWidth || arguments[0].width || 0;", img_el) or 0
            h = driver.execute_script("return arguments[0].naturalHeight || arguments[0].height || 0;", img_el) or 0
            return int(w) >= 250 and int(h) >= 250
        except Exception:
            return False

    def _is_listing_cdn_url(value):
        s = str(value or "").strip()
        return bool(s) and ("scontent" in s or "fbcdn.net" in s)

    urls = []

    # Item-style editors expose all attached photos with semantic alt text.
    # Read this form-scoped collection first: Preview repeats the first image
    # and page-wide scans can include the seller's profile photo.
    attached = driver.find_elements(By.XPATH,
        "//*[@role='form' and @aria-label='Marketplace']//img[starts-with(@alt,'Listing photo:')]")
    for img in attached:
        src = _srcset_best(img) or _img_src(img)
        if src and not src.startswith(('data:', 'blob:')) and _is_listing_cdn_url(src):
            urls.append(src)
    if urls:
        return list(dict.fromkeys(urls))

    # Priority 1: exact edit-page Preview region provided by user.
    try:
        preview_imgs = driver.find_elements(By.XPATH,
            "//div[@role='region' and @aria-label='Preview']//img[@src or @srcset]")
        for img in preview_imgs:
            src = _srcset_best(img) or _img_src(img)
            if not src:
                continue
            if src.startswith("data:") or src.startswith("blob:"):
                continue
            if _is_listing_cdn_url(src):
                urls.append(src)
    except Exception:
        pass

    candidates = []
    try:
        # Prefer images from listing-photos section near Add photos / Add photo uploader controls.
        candidates.extend(
            driver.find_elements(By.XPATH,
                        "((//span[normalize-space()='Add photos']/ancestor::*[.//img][1]) | "
                        "(//div[@aria-label='Add photo']/ancestor::*[.//img][1]) | "
                        "(//input[@type='file']/ancestor::*[.//img][1]))//img[@src]",
            )
        )
    except Exception:
        pass

    uploader_photos = bool(candidates)
    preview_urls = list(urls)
    if uploader_photos:
        urls = []

    if not candidates and not urls:
        try:
            # Fallback 1: edit page listing canvas/media region.
            candidates = driver.find_elements(By.XPATH,
                        "//a[contains(@href,'/marketplace/edit/?listing_id=')]/ancestor::*[.//img][1]//img[@src] | "
                        "//div[@role='main']//img[@src]",
            )
        except Exception:
            candidates = []

    if not candidates and not urls:
        try:
            # Fallback 2: broad page scan, later filtered.
            candidates = driver.find_elements(By.XPATH, "//img[@src or @srcset]")
        except Exception:
            candidates = []

    for img in candidates:
        src = _srcset_best(img) or _img_src(img)
        if not src:
            src = _srcset_best(img)
        if not src:
            continue
        if src.startswith("data:") or src.startswith("blob:"):
            continue
        if not _is_listing_cdn_url(src):
            continue
        if not uploader_photos and not _is_large_image(img):
            continue
        urls.append(src)

    if not urls:
        urls = preview_urls

    if not urls:
        # Last-resort parse of Preview region HTML for direct src links.
        try:
            preview_regions = driver.find_elements(By.XPATH, "//div[@role='region' and @aria-label='Preview']")
            for region in preview_regions:
                region_html = region.get_attribute("outerHTML") or ""
                for match in re.findall(r"https?://[^\"'\\s>]+", region_html):
                    m = str(match).replace("&amp;", "&").strip()
                    if _is_listing_cdn_url(m):
                        urls.append(m)
        except Exception:
            pass

    if not urls:
        # Last-resort parse of page HTML for fbcdn/scontent image URLs.
        try:
            html = driver.page_source or ""
            for match in re.findall(r"https?://[^\"'\\s>]+(?:scontent|fbcdn\\.net)[^\"'\\s>]*", html):
                m = str(match).strip()
                if m and ("scontent" in m or "fbcdn.net" in m):
                    urls.append(m)
        except Exception:
            pass

    # Deduplicate while preserving DOM order.
    seen = set()
    deduped = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


def highest_resolution_listing_images(driver, edit_url, urls):
    """Resolve signed full-size gallery URLs; never guess CDN resize parameters.

    Edit forms often expose only cropped 260px thumbnails. Match each photo
    by its CDN filename on the public listing so unrelated recommendations
    cannot enter the imported photo set. Preserve the edit form's photo order.
    """
    ids = parse_qs(urlsplit(edit_url).query).get('listing_id') or []
    if not urls or not ids or not str(ids[0]).isdigit():
        return urls
    expected = {urlsplit(url).path.rsplit('/', 1)[-1] for url in urls}
    original_handle = driver.current_window_handle
    gallery_handle = None
    try:
        # Keep the source edit form intact while resolving full-size photos.
        driver.switch_to.new_window('tab')
        gallery_handle = driver.current_window_handle
        driver.set_page_load_timeout(45)
        driver.get('https://www.facebook.com/marketplace/item/' + ids[0] + '/')
        def matching_images(current):
            images = current.execute_script('''
                return [...document.querySelectorAll('[role="main"] img')].map(img => ({
                  url:img.currentSrc || img.src || '', width:img.naturalWidth, height:img.naturalHeight}));
            ''') or []
            return [image for image in images if urlsplit(image.get('url') or '').path.rsplit('/', 1)[-1] in expected] or False
        images = WebDriverWait(driver, 15).until(matching_images)
        best = {}
        for image in images:
            url = image['url']
            if not url.startswith('https://') or not (urlsplit(url).hostname or '').endswith('.fbcdn.net'):
                continue
            name = urlsplit(url).path.rsplit('/', 1)[-1]
            score = int(image.get('width') or 0) * int(image.get('height') or 0)
            if name not in best or score > best[name][0]:
                best[name] = (score, url)
        return [best.get(urlsplit(url).path.rsplit('/', 1)[-1], (0, url))[1] for url in urls]
    except Exception as exc:
        print(f'[ListingImport] Full-size gallery unavailable; retaining edit photo URLs: {type(exc).__name__}', flush=True)
        return urls
    finally:
        if gallery_handle is not None:
            try:
                driver.close()
            finally:
                driver.switch_to.window(original_handle)


def _extract_listing_id(post_url):
    text = str(post_url or "").strip()
    if not text:
        return ""

    # Primary pattern: /marketplace/item/<digits>
    match = re.search(r"/marketplace/item/(\d+)", text)
    if match:
        return match.group(1)

    # Fallback: query param listing_id
    try:
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        listing_ids = query.get("listing_id") or []
        if listing_ids and str(listing_ids[0]).strip().isdigit():
            return str(listing_ids[0]).strip()
    except Exception:
        pass

    # Last fallback: any long digit token in URL.
    match = re.search(r"(\d{8,})", text)
    return match.group(1) if match else ""


def _read_marketplace_summary(driver):
    """Read vehicle details from the public Marketplace listing summary."""
    result = {
        "year": "",
        "make": "",
        "model": "",
        "mileage": "",
        "price": "",
        "exterior_color": "",
        "interior_color": "",
        "fuel_type": "",
        "transmission": "",
        "vehicle_condition": "",
        "description": "",
    }

    def texts():
        values = []
        try:
            elements = driver.find_elements(By.XPATH, "//h1 | //h2 | //span | //p")
        except Exception:
            elements = []
        for element in elements:
            try:
                value = " ".join((element.text or "").split()).strip()
            except Exception:
                continue
            if value and value not in values:
                values.append(value)
        return values

    values = texts()

    # The listing heading is normally: YYYY Make Model.
    for heading in driver.find_elements(By.TAG_NAME, "h1"):
        try:
            title_text = " ".join((heading.text or "").split()).strip()
        except Exception:
            title_text = ""
        match = re.match(r"^(\d{4})\s+([^\s]+)(?:\s+(.+))?$", title_text)
        if match:
            result["year"] = match.group(1)
            result["make"] = match.group(2).strip()
            result["model"] = (match.group(3) or "").strip()
            break

    for value in values:
        if not result["price"]:
            price_match = re.fullmatch(
                r"(?:[A-Z]{2})?\$\s*[\d,]+(?:\.\d{1,2})?", value
            )
            if price_match:
                result["price"] = price_match.group(0).strip()

        mileage_match = re.search(
            r"(?:driven|mileage)\s*[: ]\s*([\d,]+)\s*miles?", value, re.I
        )
        if mileage_match and not result["mileage"]:
            result["mileage"] = mileage_match.group(1).strip()

        transmission_match = re.search(
            r"\b(automatic|manual)\s+transmission\b", value, re.I
        )
        if transmission_match and not result["transmission"]:
            result["transmission"] = transmission_match.group(1).strip().title()

        color_match = re.search(
            r"exterior\s+color\s*:\s*(.*?)(?:\s*(?:·|Â·)\s*interior\s+color|$)",
            value,
            re.I,
        )
        if color_match and not result["exterior_color"]:
            result["exterior_color"] = color_match.group(1).strip()

        interior_match = re.search(r"interior\s+color\s*:\s*(.+)$", value, re.I)
        if interior_match and not result["interior_color"]:
            result["interior_color"] = interior_match.group(1).strip()

        fuel_match = re.search(r"fuel\s+type\s*:\s*(.+)$", value, re.I)
        if fuel_match and not result["fuel_type"]:
            result["fuel_type"] = fuel_match.group(1).strip()

        if not result["vehicle_condition"] and re.search(r"\bclean\s+title\b", value, re.I):
            result["vehicle_condition"] = "Clean title"

    # Seller's description is the first meaningful span following its h2.
    try:
        headings = driver.find_elements(
            By.XPATH,
            "//h2[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), \"seller's description\")]",
        )
        for heading in headings:
            following = heading.find_elements(By.XPATH, "following::span[normalize-space()]")
            for element in following:
                value = " ".join((element.text or "").split()).strip()
                if len(value) >= 20 and value.lower() not in {"see more", "see less"}:
                    result["description"] = value
                    break
            if result["description"]:
                break
    except Exception:
        pass

    return result


def _valid_model_text(value, allow_numeric=True):
    """Reject inventory containers and edit-form internal IDs as model names."""
    value = " ".join(str(value or "").split()).strip()
    if not value or len(value) > 100 or (value.isdecimal() and (not allow_numeric or len(value) > 8)):
        return ""
    if re.search(r"[$€£]|\b(?:MAD|USD|CAD|GBP|EUR|PKR)\s*[\d,]|\b(?:listed on|clicks on listing|listed on marketplace|renew your listing)\b", value, re.IGNORECASE):
        return ""
    return value


def _model_from_visible_listing_heading(driver, year, make, log_result=True, expected_model=None):
    """Return the model portion of a visible `Year Make Model` Marketplace h1."""
    year = " ".join(str(year or "").split()).strip()
    make = " ".join(str(make or "").split()).strip()
    if not year or not make:
        return ""

    prefix_pattern = re.compile(
        rf"^{re.escape(year)}\s+{re.escape(make)}(?:\s+|$)",
        re.IGNORECASE,
    )
    # Prefer the opened listing's dialog. A parent span on the selling page
    # can contain the titles, prices and metrics of every listing; only leaf
    # spans are eligible for the non-heading fallback.
    candidate_groups = [
        ("dialog heading", "//*[@role='dialog']//h1[normalize-space()] | //*[@role='dialog']//*[@role='heading'][normalize-space()]"),
        ("h1 heading", "//h1[normalize-space()]"),
        ("matching span fallback", "//span[not(.//span) and normalize-space()]"),
    ]

    seen_texts = set()
    for source_name, xpath in candidate_groups:
        try:
            candidates = driver.find_elements(By.XPATH, xpath)
        except Exception:
            candidates = []
        for candidate in candidates:
            try:
                if not candidate.is_displayed():
                    continue
                title_text = " ".join((candidate.text or "").split()).strip()
            except Exception:
                continue
            if not title_text or title_text in seen_texts:
                continue
            seen_texts.add(title_text)
            if not prefix_pattern.match(title_text):
                continue
            model = _valid_model_text(prefix_pattern.sub("", title_text, count=1))
            # Inventory spans can still contain the previous vehicle while
            # Facebook opens the selected card. Never use another model.
            if expected_model and _normalize_text(model) != _normalize_text(expected_model):
                continue
            if model:
                if log_result:
                    print(
                        f"[Reposting] Model read from visible {source_name}: {model!r} "
                        f"(text={title_text!r})",
                        flush=True,
                    )
                return model
    if log_result:
        print(
            f"[Reposting] Visible listing heading did not yield a model for year={year!r}, make={make!r}",
            flush=True,
        )
    return ""


def _mark_listing_pending_and_wait(driver, listing_name, listing_price="", timeout=30):
    target_name = str(listing_name or "").strip()
    if not target_name:
        raise ValueError("Listing name is required to mark listing as pending")

    driver.get("https://www.facebook.com/marketplace/you/selling")
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
    )

    matched_card = _find_listing_card_with_scroll(
        driver=driver,
        listing_name=target_name,
        listing_price=listing_price,
        require_pending=False,
        timeout=timeout,
    )

    # Fallback: if full "year make model" doesn't match, try "year make" with same price.
    if not matched_card and str(listing_price or "").strip():
        fallback_name = _fallback_listing_name_year_make(target_name)
        if fallback_name and fallback_name != target_name:
            matched_card = _find_listing_card_with_scroll(
                driver=driver,
                listing_name=fallback_name,
                listing_price=listing_price,
                require_pending=False,
                timeout=timeout,
            )

    if not matched_card:
        raise Exception(
            f"Listing not found for pending mark: name='{target_name}' price='{listing_price}'"
        )

    card_text = _normalize_text(matched_card.text or "")
    if "pending" in card_text:
        return True

    if not _safe_click(driver, matched_card):
        raise Exception("Unable to open matched listing before marking pending")

    pending_locators = [
        (
            By.XPATH,
            "//div[@role='button' and contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'mark as pending')]",
        ),
        (
            By.XPATH,
            "//span[normalize-space()='Mark as pending']/ancestor::div[@role='button'][1]",
        ),
    ]

    pending_btn = None
    for locator in pending_locators:
        try:
            pending_btn = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable(locator)
            )
            if pending_btn:
                break
        except Exception:
            continue

    if not pending_btn:
        raise Exception("Mark as pending button not found on selling page")

    if not _safe_click(driver, pending_btn):
        raise Exception("Unable to click Mark as pending button")

    became_invisible = False
    for locator in pending_locators:
        try:
            if WebDriverWait(driver, timeout).until(
                EC.invisibility_of_element_located(locator)
            ):
                became_invisible = True
                break
        except Exception:
            continue

    if not became_invisible:
        raise Exception("Mark as pending did not become invisible within timeout")

    return True


def _fallback_listing_name_year_make(listing_name: str) -> str:
    tokens = [t for t in str(listing_name or "").strip().split() if t]
    if not tokens:
        return ""

    # Prefer "YYYY Make" if first token is a year.
    if len(tokens) >= 2 and re.fullmatch(r"\d{4}", tokens[0]):
        return f"{tokens[0]} {tokens[1]}".strip()

    # Otherwise, best-effort first two tokens.
    if len(tokens) >= 2:
        return f"{tokens[0]} {tokens[1]}".strip()

    return tokens[0]


def _normalize_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _normalize_price(value):
    return re.sub(r"[^0-9]", "", str(value or ""))


def _scroll_down_page(driver, steps=1, jump_to_bottom=True):
    for _ in range(max(1, int(steps or 0))):
        try:
            if jump_to_bottom:
                driver.execute_script("window.scrollBy(0, Math.max(window.innerHeight, 800));")
            else:
                driver.execute_script('''
                    const input = document.querySelector('input[aria-label="Price"]') ||
                        [...document.querySelectorAll('label')].find(el => /price/i.test(el.textContent))?.querySelector('input');
                    let panel = input;
                    while (panel && !(panel.clientHeight > 100 && panel.scrollHeight > panel.clientHeight + 2 &&
                           /auto|scroll/.test(getComputedStyle(panel).overflowY))) panel = panel.parentElement;
                    panel = panel || document.scrollingElement;
                    panel.scrollTop += Math.max(100, panel.clientHeight * 0.75);
                ''')
        except Exception:
            pass
        try:
            if jump_to_bottom:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass


def _safe_click(driver, element):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    except Exception:
        pass

    try:
        element.click()
        return True
    except Exception:
        pass

    try:
        driver.execute_script("arguments[0].click();", element)
        return True
    except Exception:
        return False


def _scroll_listing_page_to_end(driver, max_duration=20, idle_rounds_needed=3):
    """
    Scroll the selling list feed until no more new content loads.
    Returns True when an end/idle condition is reached.
    """
    end_by = time.time() + max(5, int(max_duration))
    idle_rounds = 0
    last_height = 0

    while time.time() < end_by:
        _scroll_down_page(driver, steps=1)

        time.sleep(0.75)

        try:
            new_height = int(
                driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"
                )
                or 0
            )
        except Exception:
            new_height = last_height

        if new_height <= last_height:
            idle_rounds += 1
        else:
            idle_rounds = 0
            last_height = new_height

        if idle_rounds >= max(2, int(idle_rounds_needed)):
            return True

    return False


def _find_listing_card_with_scroll(
    driver,
    listing_name,
    listing_price="",
    require_pending=False,
    timeout=30,
    listing_url="",
):
    """
    Search every visible batch, then scroll down one viewport and pause so
    Facebook has time to render the next batch before searching again.
    """
    # Finding a listing is primarily end-of-page driven. A short caller
    # timeout (normally 40s) is not enough for long Marketplace inventories
    # when Facebook lazy-loads each batch. Keep a finite safety ceiling while
    # allowing the scan to reach the actual end.
    end_by = time.time() + max(300, int(timeout))
    idle_rounds = 0
    previous_position = -1
    previous_height = -1
    scan_round = 0

    while time.time() < end_by:
        scan_round += 1
        matched_card = _find_listing_card(
            driver=driver,
            listing_name=listing_name,
            listing_price=listing_price,
            require_pending=require_pending,
            listing_url=listing_url,
        )
        if matched_card:
            return matched_card

        # Give Facebook time to finish the current batch before requesting the
        # next one. This deliberately happens on every unsuccessful search.
        time.sleep(5)

        try:
            before = driver.execute_script(
                "return {y: window.pageYOffset || document.documentElement.scrollTop || 0, "
                "h: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)};"
            ) or {}
            driver.execute_script(
                "window.scrollBy(0, Math.max(500, window.innerHeight * 0.8));"
            )
            # Marketplace cards are lazy-rendered. Searching immediately after
            # scrolling can inspect the old batch and miss the requested card.
            time.sleep(1.5)
            after = driver.execute_script(
                "return {y: window.pageYOffset || document.documentElement.scrollTop || 0, "
                "h: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)};"
            ) or {}
            position = int(after.get("y") or 0)
            height = int(after.get("h") or 0)
            before_position = int(before.get("y") or 0)
            if position <= before_position and position <= previous_position and height <= previous_height:
                idle_rounds += 1
            else:
                idle_rounds = 0
            previous_position = position
            previous_height = height
            print(
                f"[Reposting] listing search scan={scan_round} "
                f"scroll_y={position} page_height={height} bottom_wait={idle_rounds}/4",
                flush=True,
            )
        except Exception:
            idle_rounds += 1
            time.sleep(1.5)

        # Do not stop on the first apparent bottom. Facebook can append another
        # batch several seconds later without immediately changing height.
        if idle_rounds >= 4:
            print(
                f"[Reposting] listing search reached page end after {scan_round} scans",
                flush=True,
            )
            break

    return None


def _find_listing_card(driver, listing_name, listing_price="", require_pending=False, listing_url=""):
    target_name = _normalize_text(listing_name)
    target_price = _normalize_price(listing_price)
    if not target_name:
        return None

    # Current Manage Listings rows are anchored by a semantic Select Listing
    # checkbox. The vehicle-title button, image and actions are sibling
    # branches, so a broad role=button lookup alone can miss the actual row.
    row_xpath = (
        "//input[translate(normalize-space(@aria-label),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='select listing']"
        "/ancestor::div[.//img and .//*[@role='button' and @aria-label]][1]"
    )
    candidates = []
    for row in driver.find_elements(By.XPATH, row_xpath):
        try:
            title_controls = row.find_elements(
                By.XPATH,
                ".//*[@role='button' and @aria-label and "
                "not(starts-with(@aria-label,'More options for '))]",
            )
            candidates.append((row, title_controls))
        except Exception:
            continue

    # Older Marketplace layout fallback.
    for card in driver.find_elements(By.XPATH, "//div[@role='button' and @aria-label]"):
        candidates.append((card, [card]))

    for card, title_controls in candidates:
        try:
            if listing_url and '/marketplace/item/' in listing_url:
                links = [str(link.get_attribute('href') or '').split('?', 1)[0].rstrip('/') for link in card.find_elements(By.CSS_SELECTOR, 'a[href]')]
                item_links = [link for link in links if '/marketplace/item/' in link]
                if item_links and listing_url.split('?', 1)[0].rstrip('/') not in item_links:
                    continue
            card_text = _normalize_text(card.text or "")
            labels = [
                _normalize_text(control.get_attribute("aria-label") or "")
                for control in title_controls
            ]
            labels = [label for label in labels if label]
            if not labels and not card_text:
                continue

            matches_name = (
                any(target_name == label or target_name in label for label in labels)
                or target_name in card_text
            )
            if not matches_name:
                continue

            if require_pending and "pending" not in card_text:
                continue

            if target_price:
                card_price = _normalize_price(card_text)
                if target_price not in card_price:
                    continue

            # Prefer the title control because clicking the surrounding row can
            # toggle its checkbox instead of opening the listing details.
            return next(
                (
                    control for control in title_controls
                    if target_name in _normalize_text(control.get_attribute("aria-label") or "")
                ),
                card,
            )
        except Exception:
            continue
    return None


def _open_manage_listings(driver, timeout=30):
    """Open Selling and switch to Facebook's Manage Listings view."""
    driver.get("https://www.facebook.com/marketplace/you/selling")
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, "//div[@role='main']"))
    )
    manage_locators = (
        (By.XPATH, "//*[@aria-label='Manage listings']"),
        (
            By.XPATH,
            "//*[self::span or self::div][translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='manage listings']"
            "/ancestor::*[@role='button'][1]",
        ),
    )
    for locator in manage_locators:
        try:
            control = WebDriverWait(driver, 10).until(EC.element_to_be_clickable(locator))
            if _safe_click(driver, control):
                time.sleep(3)
                return True
        except Exception:
            continue
    # Some accounts already land in Manage Listings and have no switch button.
    return bool(driver.find_elements(By.XPATH, "//input[@aria-label='Select Listing']"))


def _find_edit_listing_link(driver, timeout=20, clickable=True):
    """Find the Edit Listing link without depending on Facebook CSS classes."""
    end_by = time.time() + max(1, float(timeout))
    while time.time() < end_by:
        try:
            # The supplied Facebook layout uses an icon-only anchor and puts
            # the visible "Edit Listing" span in a sibling div. Query the DOM
            # directly and compare both aria-label and nearby text without
            # case sensitivity. Presence is enough; navigation has an href
            # fallback if Facebook's overlay intercepts the physical click.
            element = driver.execute_script(
                """
                const norm = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const links = [...document.querySelectorAll('a[href*="/marketplace/edit/"]')];
                const byAria = links.find(link => norm(link.getAttribute('aria-label')) === 'edit listing');
                if (byAria) return byAria;
                return links.find(link => {
                  const container = link.parentElement;
                  if (!container) return false;
                  return [...container.querySelectorAll('span, div')]
                    .some(node => norm(node.textContent) === 'edit listing');
                }) || null;
                """
            )
            if element:
                return element
        except Exception:
            pass
        time.sleep(0.25)
    return None


def _print_reposting_read_summary(post_data, edit_url=""):
    """Print every value produced by the Manage Listings repost reader."""
    fields = (
        "title", "vehicle_type", "year", "make", "model", "mileage", "price",
        "body_style", "exterior_color", "interior_color",
        "vehicle_condition", "fuel_type", "transmission", "description",
    )
    print("[Reposting] Complete listing read:", flush=True)
    print(f"[Reposting]   edit_url={str(edit_url or '')!r}", flush=True)
    for field in fields:
        value = post_data.get(field)
        if value not in (None, "", [], {}):
            print(f"[Reposting]   {field}={value!r}", flush=True)
        else:
            print(f"[Reposting]   {field}=MISSING (could not read)", flush=True)

    image_urls = [str(url).strip() for url in (post_data.get("image_urls") or []) if str(url).strip()]
    print(f"[Reposting]   image_count={len(image_urls)}", flush=True)
    if not image_urls:
        print("[Reposting]   image_urls=MISSING (could not read any images)", flush=True)
    else:
        for index, image_url in enumerate(image_urls, start=1):
            print(f"[Reposting]   image_{index}={image_url!r}", flush=True)


def recover_unreadable_fields(driver, post, fallback, fields=None, skip_confirmed_empty=False):
    """Recover missing HTML values once, excluding colors, VIN, and trim."""
    states = post.setdefault('field_states', {})
    excluded = {'exterior_color', 'interior_color', 'vehicle_condition', 'vin', 'trim'}
    fields = [key for key in (fields if fields is not None else EDIT_FIELDS) if key not in excluded]
    missing = [key for key in fields if not str(post.get(key) or '').strip()
               or states.get(key) in {'unavailable', 'unreadable', 'conflict'}]
    if skip_confirmed_empty:
        missing = [key for key in missing if states.get(key) != 'empty']
    post['field_sources'] = {key: 'primary' for key in fields if key not in missing}
    if not fallback or not missing:
        return
    print(f"[Import] Could not read {', '.join(key.replace('_', ' ') for key in missing)} from HTML. Taking screenshots and trying OCR.", flush=True)
    try:
        result = fallback(driver)
        recovered = result['posts'][0]
        recovered_states = recovered.get('field_states', {})
        for key in missing:
            state = recovered_states.get(key, 'unreadable')
            if state not in {'read', 'empty', 'disabled', 'needs_review'}:
                continue
            if state == 'needs_review' and not recovered.get(key):
                continue
            post[key] = recovered.get(key, '')
            states[key] = state
            post['field_sources'][key] = 'visual_fallback'
            print(f"[Import] OCR read {key.replace('_', ' ')}: {post[key]}", flush=True)
        for key in ('visual_read_report', 'visual_read_requires_review'):
            if key in recovered:
                post[key] = recovered[key]
        post['fallback_warnings'] = result.get('warnings', [])
    except Exception as exc:
        # A failed optional recovery must never discard the primary read.
        post['fallback_error'] = str(exc)
        print(f'[ListingImport] Visual fallback failed; primary values kept: {exc}', flush=True)


def _public_media_in_new_tab(driver, listing):
    """Resolve the selected item after navigation settles; always restore its tab."""
    from automation.public_listing_media import read_public_listing_media

    def exact_id(url):
        match = re.search(r'/marketplace/item/(\d+)(?:/|\?|$)', str(url or ''))
        if match:
            return match.group(1)
        values = parse_qs(urlsplit(str(url or '')).query).get('listing_id', [])
        return values[0] if values and values[0].isdigit() else ''

    def selected_item(current):
        # Selling cards may have no href, and clicking them may open a dialog
        # without changing the address bar. Its Edit link still identifies it.
        navigated = exact_id(current.current_url)
        if navigated:
            return navigated
        dialogs = [node for node in current.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
                   if node.is_displayed()]
        scope = dialogs[-1] if dialogs else current
        links = scope.find_elements(By.XPATH, ".//a[contains(@href,'/marketplace/edit/') and contains(@href,'listing_id=')]")
        ids = {exact_id(link.get_attribute('href')) for link in links if link.is_displayed()}
        ids.discard('')
        if len(ids) == 1:
            return next(iter(ids))
        return exact_id(listing.get('url')) or exact_id(listing.get('edit_url')) or False

    listing_id = WebDriverWait(driver, 20).until(selected_item,
        message='Selected listing did not expose its public URL or Edit listing ID within 20 seconds.')
    original = driver.current_window_handle
    previous_timeout = driver.timeouts.page_load
    opened = None
    try:
        driver.switch_to.new_window('tab')
        opened = driver.current_window_handle
        driver.set_page_load_timeout(30)
        public_url = 'https://www.facebook.com/marketplace/item/' + listing_id + '/'
        print(f'[Import] Opening public listing in separate tab: {public_url}', flush=True)
        driver.get(public_url)
        WebDriverWait(driver, 20).until(lambda current: exact_id(current.current_url) == listing_id,
            message='Public listing did not open within 20 seconds.')
        print(f'[Import] Public listing opened: {driver.current_url}', flush=True)
        return read_public_listing_media(driver, listing_id)
    finally:
        try:
            if opened is not None and opened != original:
                driver.close()
        finally:
            driver.switch_to.window(original)
            driver.set_page_load_timeout(previous_timeout)


def read_listing_from_manage(account, listing, driver=None, field_reader=None, visual_fallback=None,
                             listing_card=None, return_to_inventory=None, public_media=False):
    """Read one stored listing through Manage Listings -> Edit listing."""
    if not account:
        raise ValueError("Account is required")
    if not isinstance(listing, dict):
        raise ValueError("Listing data is required")

    listing_name = " ".join(
        str(listing.get(field) or "").strip()
        for field in ("year", "make", "model")
        if str(listing.get(field) or "").strip()
    ).strip()
    listing_price = str(listing.get("price") or "").strip()
    if not listing_name:
        raise ValueError("Listing title is required")

    media_urls, media_errors = [], []
    public_sources = {'image_urls': [], 'video_urls': [], 'video_errors': []}

    def with_videos(result):
        post = result['posts'][0]
        if public_media:
            post.update(public_sources)
            post['media_source'] = 'public_listing'
            return result
        from automation.listing_videos import discover
        final_urls, final_errors = discover(driver)
        post['video_urls'] = list(dict.fromkeys(media_urls + final_urls + (post.get('video_urls') or [])))
        post['video_errors'] = list(dict.fromkeys(media_errors + final_errors))
        return result

    owns_driver = driver is None
    fallback_used = False

    def visual_once(current):
        nonlocal fallback_used
        if fallback_used or visual_fallback is None:
            raise RuntimeError('Visual recovery was already attempted for this listing.')
        fallback_used = True
        return visual_fallback(current)

    try:
        if owns_driver:
            driver = _open_prelogged_facebook(account)
        card = listing_card
        if card is None:
            if not _open_manage_listings(driver):
                raise Exception("Manage Listings could not be opened")
            card = _find_listing_card_with_scroll(
                driver,
                listing_name=listing_name,
                listing_price=listing_price,
                require_pending=False,
                timeout=40,
                listing_url=str(listing.get('url') or ''),
            )
        if not card:
            raise Exception(f"Listing not found in Manage Listings: {listing_name}")
        if not _safe_click(driver, card):
            raise Exception("Selected listing could not be opened")
        print(f'[ListingImport] Clicked listing: {listing_name}', flush=True)

        if public_media:
            try:
                public_sources = _public_media_in_new_tab(driver, listing)
                print(f"[Import] Public gallery: {len(public_sources['image_urls'])} photos, {len(public_sources['video_urls'])} optional HD videos.", flush=True)
            except Exception as exc:
                message = f'Public media extraction failed: {type(exc).__name__}: {exc}'
                public_sources['photo_errors'] = [message]
                print(f'[Import] {message}. Continuing with Edit Listing details.', flush=True)

        # Read Model from the public listing heading before opening Edit.
        # Facebook's edit-form Model field can contain an internal numeric ID
        # instead of the vehicle model (for example, "237284798").
        listing_year = str(listing.get("year") or "").strip()
        listing_make = str(listing.get("make") or "").strip()
        try:
            WebDriverWait(driver, 10).until(
                lambda current: bool(
                    _model_from_visible_listing_heading(
                        current, listing_year, listing_make, log_result=False,
                        expected_model=listing.get('model')
                    )
                )
            )
        except Exception:
            pass
        heading_model = _model_from_visible_listing_heading(
            driver, listing_year, listing_make, expected_model=listing.get('model')
        )
        if heading_model:
            print(f"[Reposting] READ model={heading_model!r}", flush=True)
        else:
            print(
                "[Reposting] MISSING model: public listing heading did not provide it; "
                "using stored model fallback",
                flush=True,
            )

        edit_link = _find_edit_listing_link(driver, timeout=20, clickable=True)
        if not edit_link:
            raise Exception("Edit listing link was not found")
        edit_href = str(edit_link.get_attribute("href") or "").strip()
        print('[Import] Clicking Edit listing to open the edit page.', flush=True)
        clicked = _safe_click(driver, edit_link)
        if not clicked and not edit_href:
            raise Exception("Edit listing could not be opened")
        try:
            WebDriverWait(driver, 8).until(
                lambda current: "/marketplace/edit/" in str(current.current_url or "")
                or bool(current.find_elements(By.XPATH, "//*[@aria-label='Price']"))
            )
        except Exception:
            # Facebook can swallow both native and JavaScript clicks while an
            # overlay is animating. The anchor's href is the authoritative edit
            # destination, so open it directly after verifying click failure.
            if not edit_href:
                raise Exception("Edit listing could not be opened")
            driver.get(edit_href)
        WebDriverWait(driver, 30).until(
            lambda current: "/marketplace/edit/" in str(current.current_url or "")
            or bool(current.find_elements(By.XPATH, "//*[@aria-label='Price']"))
        )
        if not public_media:
            from automation.listing_videos import discover
            media_urls, media_errors = discover(driver)
        if field_reader is not None:
            WebDriverWait(driver, 30).until(lambda current: current.execute_script(
                "return document.readyState === 'complete' && !!document.querySelector('input,textarea');"))
            result = field_reader(driver)
            post = result['posts'][0]
            post['title'] = ' '.join(str(post.get(key) or '').strip() for key in ('year', 'make', 'model')).strip() or listing_name
            return with_videos(result)
        snapshot = wait_for_edit_form_ready(driver)
        print('[Import] Edit page opened. Taking listing details from HTML.', flush=True)
        # Some Facebook forms virtualize controls while scrolling. Retain the
        # fields read at the top instead of replacing them with absent controls.
        for _ in range(8):
            _scroll_down_page(driver, steps=1, jump_to_bottom=False)
            time.sleep(0.2)
            scrolled_snapshot = read_edit_form_snapshot(driver)
            for field, value in scrolled_snapshot.items():
                if value.get('state') != 'unavailable' and (
                    value.get('value') or snapshot.get(field, {}).get('state') == 'unavailable'
                ):
                    snapshot[field] = value

        item_post = _item_edit_details(snapshot, listing, [])
        if item_post is not None:
            item_post['image_urls'] = public_sources['image_urls'] if public_media else _safe_image_urls(driver)
            if visual_fallback is not None:
                recover_unreadable_fields(driver, item_post, visual_once,
                                          fields=('title', 'price', 'description'),
                                          skip_confirmed_empty=public_media)
            if not item_post.get('title') or not item_post.get('price'):
                raise RuntimeError('Item editor title or price could not be read; saved details were not replaced.')
            print('[ListingImport] Read item-style editor: title, price, description, category, condition, SKU and attached photos.', flush=True)
            return with_videos({'ok': True, 'edit_url': edit_href, 'listing': dict(listing), 'posts': [item_post]})

        if not any(snapshot.get(field, {}).get('state', 'unavailable') != 'unavailable'
                   for field in ('vehicle_type', 'year', 'make', 'model', 'mileage', 'body_style')):
            raise RuntimeError('Neither vehicle nor item edit controls were recognized; trying the visual reader.')
        print('[ListingImport] Using original vehicle editor reader.', flush=True)

        def read_value(field, reader):
            try:
                if field in snapshot and snapshot[field].get('state') != 'unavailable':
                    return snapshot[field].get('value') or ''
                if field in EDIT_FIELDS:
                    # The stable, scrolled snapshot already searched all control
                    # layouts. Do not spend 20 seconds per absent field retrying
                    # equivalent selectors; visual recovery runs once below.
                    return ''
                value = reader() or ""
                if value not in (None, "", [], {}):
                    if field in EDIT_FIELDS:
                        snapshot[field] = {'value': value, 'state': 'read'}
                    print(f"[Reposting] READ {field}={value!r}", flush=True)
                else:
                    print(f"[Reposting] MISSING {field}: no value found", flush=True)
                return value
            except Exception as exc:
                print(f"[Reposting] READ FAILED {field}: {exc}", flush=True)
                return ""

        post_data = {
            'vin': snapshot.get('vin', {}).get('value') or '',
            'trim': snapshot.get('trim', {}).get('value') or '',
            "vehicle_type": read_value("vehicle_type", lambda: _safe_combobox_text(driver, "Vehicle type")),
            "image_urls": public_sources['image_urls'] if public_media else read_value("image_urls", lambda: _safe_image_urls(driver)),
            "year": read_value("year", lambda: _safe_combobox_text(driver, "Year") or _safe_input_value(driver, "Year")) or str(listing.get("year") or ""),
            "make": read_value("make", lambda: _safe_input_value(driver, "Make") or _safe_combobox_text(driver, "Make")) or str(listing.get("make") or ""),
            "model": heading_model or _valid_model_text(snapshot.get('model', {}).get('value'), allow_numeric=False) or _valid_model_text(listing.get("model")),
            "mileage": read_value("mileage", lambda: _safe_input_value(driver, "Mileage")),
            "price": read_value("price", lambda: _safe_input_value(driver, "Price")) or listing_price,
            "body_style": read_value("body_style", lambda: _safe_combobox_text(driver, "Body style")),
            "exterior_color": read_value("exterior_color", lambda: _safe_combobox_text(driver, "Exterior colour") or _safe_combobox_text(driver, "Exterior color")),
            "interior_color": read_value("interior_color", lambda: _safe_combobox_text(driver, "Interior colour") or _safe_combobox_text(driver, "Interior color")),
            "vehicle_condition": read_value("vehicle_condition", lambda: _safe_combobox_text(driver, "Vehicle condition")),
            "fuel_type": read_value("fuel_type", lambda: _safe_combobox_text(driver, "Fuel type")),
            "transmission": read_value("transmission", lambda: _safe_combobox_text(driver, "Transmission")),
            "description": read_value("description", lambda: _safe_description(driver)),
            "location": read_value("location", lambda: _safe_input_value(driver, "Location")),
            "clean_title": read_value("clean_title", lambda: ''),
        }
        post_data['field_states'] = {key: value.get('state', 'unavailable') for key, value in snapshot.items()}
        if heading_model:
            post_data['field_states']['model'] = 'read'
        if visual_fallback is not None:
            recover_unreadable_fields(driver, post_data, visual_once, skip_confirmed_empty=public_media)
        _print_reposting_read_summary(post_data, edit_url=edit_href)
        post_data["title"] = " ".join(
            str(post_data.get(field) or "").strip()
            for field in ("year", "make", "model")
            if str(post_data.get(field) or "").strip()
        ).strip() or listing_name
        _print_reposting_read_summary(post_data, edit_url=edit_href)
        return with_videos({
            "ok": True,
            "edit_url": edit_href,
            "listing": dict(listing),
            "posts": [post_data],
        })
    except Exception as primary_error:
        if not fallback_used and visual_fallback is not None and driver is not None and (not public_media or public_sources is not None) and '/marketplace/edit/' in str(driver.current_url or ''):
            print('[Import] Could not finish reading HTML. Taking screenshots and trying OCR.', flush=True)
            result = visual_once(driver)
            if not result.get('ok'):
                raise RuntimeError('Primary and visual listing readers failed; saved details were not replaced.')
            post = result['posts'][0]
            post['field_sources'] = {key: 'visual_fallback' for key in EDIT_FIELDS}
            post['title'] = post.get('title') or ' '.join(str(post.get(key) or '').strip() for key in ('year', 'make', 'model')).strip() or listing_name
            return with_videos(result)
        raise
    finally:
        if driver and not owns_driver and return_to_inventory is not None:
            return_to_inventory(driver)
        elif driver and not owns_driver:
            # Return the shared import browser to inventory after each listing,
            # including a failed read.
            try:
                if not _open_manage_listings(driver):
                    print('[ListingImport] Could not return to Manage Listings.', flush=True)
            except Exception as exc:
                print(f'[ListingImport] Could not return to Manage Listings: {exc}', flush=True)
        if driver and owns_driver:
            try:
                driver.quit()
            except Exception:
                pass


def delete_reposted_listing(account, listing_name, listing_price="", listing_id=""):
    if not account:
        raise ValueError("Account is required")
    if not str(listing_name or "").strip():
        raise ValueError("Listing name is required")

    driver = None
    try:
        driver = _open_prelogged_facebook(account)
        if not _open_manage_listings(driver):
            raise Exception("Manage Listings could not be opened")

        target_listing_id = str(listing_id or "").strip()
        matched_card = None

        if target_listing_id:
            # A freshly posted clone can share the same title and price. Open
            # matching cards and verify their Edit Listing ID before deleting.
            for candidate_index in range(20):
                cards = []
                target_name = _normalize_text(listing_name)
                target_price = _normalize_price(listing_price)
                try:
                    visible_cards = driver.find_elements(By.XPATH, "//div[@role='button' and @aria-label]")
                    for candidate in visible_cards:
                        aria = _normalize_text(candidate.get_attribute("aria-label") or "")
                        text = _normalize_text(candidate.text or "")
                        if target_name not in aria and target_name not in text:
                            continue
                        if target_price and target_price not in _normalize_price(text):
                            continue
                        cards.append(candidate)
                except Exception:
                    cards = []
                if candidate_index >= len(cards):
                    if _scroll_listing_page_to_end(driver, max_duration=5, idle_rounds_needed=2):
                        break
                    continue
                candidate = cards[candidate_index]
                if not _safe_click(driver, candidate):
                    continue
                try:
                    edit_link = _find_edit_listing_link(driver, timeout=8, clickable=False)
                    if not edit_link:
                        raise Exception("Edit listing link was not found")
                    edit_href = str(edit_link.get_attribute("href") or "")
                    if re.search(rf"(?:[?&]listing_id=){re.escape(target_listing_id)}(?:&|$)", edit_href):
                        matched_card = candidate
                        break
                except Exception:
                    pass
                if not _open_manage_listings(driver):
                    break
        else:
            matched_card = _find_listing_card_with_scroll(
                driver=driver,
                listing_name=listing_name,
                listing_price=listing_price,
                require_pending=False,
                timeout=30,
            )

        if not matched_card and not target_listing_id and str(listing_price or "").strip():
            fallback_name = _fallback_listing_name_year_make(listing_name)
            if fallback_name and fallback_name != listing_name:
                matched_card = _find_listing_card_with_scroll(
                    driver=driver,
                    listing_name=fallback_name,
                    listing_price=listing_price,
                    require_pending=False,
                    timeout=30,
                )

        if not matched_card:
            raise Exception(
                f"Listing not found for name='{listing_name}' price='{listing_price}'"
            )

        # ID matching already opened the correct card. Title fallback has not.
        if not target_listing_id and not _safe_click(driver, matched_card):
            raise Exception("Unable to open matched listing")
        time.sleep(0.5)

        delete_btn = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    '//div[@role="button" and ('
                    '@aria-label="Delete marketplace listing" or '
                    '@aria-label="Delete Marketplace listing")]',
                )
            )
        )
        if not _safe_click(driver, delete_btn):
            raise Exception("Unable to click delete listing button")

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']"))
        )

        confirm_candidates = [
            (By.XPATH, "(//div[@role='dialog']//div[@role='button' and @aria-label='Delete'])[last()]"),
            (By.XPATH, "(//div[@role='dialog']//div[@role='button'][.//span[normalize-space()='Delete']])[last()]"),
            (By.XPATH, "(//div[@role='button' and @aria-label='Delete'])[last()]"),
        ]

        confirm_btn = None
        for locator in confirm_candidates:
            try:
                confirm_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(locator)
                )
                if confirm_btn:
                    break
            except Exception:
                continue

        if not confirm_btn:
            raise Exception("Confirm delete button not found")

        if not _safe_click(driver, confirm_btn):
            raise Exception("Unable to click confirm delete button")

        return True
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


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
    except Exception:
        pass


def _resolve_session_file(session_dir, account):
    direct = os.path.join(session_dir, f"{account}.pkl")
    if os.path.exists(direct):
        return direct

    account_text = str(account or "").strip()
    if not account_text:
        raise FileNotFoundError("Active account is empty")

    try:
        stems = [
            os.path.splitext(name)[0]
            for name in os.listdir(session_dir)
            if name.endswith(".pkl")
        ]
    except Exception:
        stems = []

    normalized_account = account_text.replace("@", "_")
    for stem in stems:
        if stem == normalized_account or stem.replace("@", "_") == normalized_account:
            return os.path.join(session_dir, f"{stem}.pkl")

    raise FileNotFoundError(
        f"Session file not found for account '{account_text}'. Available sessions: {', '.join(stems) or 'none'}"
    )


def _open_prelogged_facebook(account, force_headless=False, on_driver_ready=None):
    session_file = _resolve_session_file(SESSION_DIR, account)
    with open(session_file, "rb") as f:
        cookies = pickle.load(f)

    options = Options()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-blink-features=AutomationControlled")
    if force_headless:
        options.add_argument("--headless=new")
    else:
        apply_background_mode_option(options)
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = build_chrome_driver(options)
    try:
        if on_driver_ready is not None:
            on_driver_ready(driver)
        return _restore_facebook_cookies(driver, cookies, account)
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise


def _restore_facebook_cookies(driver, cookies, account):
    _apply_browser_safety_patches(driver)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

    driver.get("https://www.facebook.com/")
    time.sleep(1.0)

    valid = False
    for cookie in cookies:
        if "sameSite" not in cookie:
            cookie["sameSite"] = "Lax"
        cookie["domain"] = ".facebook.com"
        try:
            driver.add_cookie(cookie)
            if cookie.get("name") == "c_user":
                valid = True
        except Exception:
            continue

    if not valid:
        driver.quit()
        raise Exception("Invalid session: c_user cookie missing")

    driver.get("https://www.facebook.com/")
    time.sleep(1.0)

    if "login" in (driver.current_url or "").lower():
        driver.quit()
        raise Exception(f"Session expired or cookies invalid for {account}")

    return driver


def read_posts(url, account=None):
    """
    Open pre-logged Facebook for an account, visit a post URL, and wait.
    """
    target_url = str(url or "").strip()
    if not target_url:
        raise ValueError("URL is required")

    if not account:
        raise ValueError("Account is required")

    driver = None
    try:
        driver = _open_prelogged_facebook(account)
        driver.get(target_url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "h1"))
        )
        try:
            WebDriverWait(driver, 15).until(
                lambda current: any(
                    "about this vehicle" in " ".join((element.text or "").lower().split())
                    for element in current.find_elements(By.TAG_NAME, "h2")
                )
            )
        except Exception:
            pass
        # Read the stable public listing summary before opening the edit page.
        # Image extraction below remains unchanged and still uses the existing
        # preview/image readers.
        summary_data = _read_marketplace_summary(driver)
        print(f"[Reposting] Public summary extracted: {summary_data}", flush=True)
        try:
            edit_locators = [
                (
                    By.XPATH,
                    "//a[contains(@href, '/marketplace/edit/?listing_id=') and .//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'edit')]]",
                ),
                (
                    By.XPATH,
                    "//a[contains(@href, '/marketplace/edit/?listing_id=') and (contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'edit') or contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'edit post') or contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'edit listing'))]",
                ),
                (
                    By.XPATH,
                    "//a[.//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'edit')]]",
                ),
            ]

            edit_btn = None
            for locator in edit_locators:
                try:
                    edit_btn = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable(locator)
                    )
                    if edit_btn:
                        break
                except Exception:
                    continue

            if not edit_btn:
                raise Exception("Edit button not found")

            try:
                edit_btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", edit_btn)
        except Exception:
            pass

        time.sleep(0.5)
        _scroll_down_page(driver, steps=3)

        def _read_visible_field(reader):
            try:
                value = reader()
                return value if value is not None else ""
            except Exception:
                return ""

        post_data = {
            "vehicle_type": _read_visible_field(
                lambda: _safe_combobox_text(driver, "Vehicle type")
            ),
            "image_urls": _read_visible_field(lambda: _safe_image_urls(driver)),
            "year": summary_data.get("year") or _read_visible_field(
                lambda: _safe_combobox_text(driver, "Year") or _safe_input_value(driver, "Year")
            ),
            "make": summary_data.get("make") or _read_visible_field(
                lambda: _safe_input_value(driver, "Make") or _safe_combobox_text(driver, "Make")
            ),
            "model": summary_data.get("model") or _read_visible_field(
                lambda: _safe_input_value(driver, "Model") or _safe_combobox_text(driver, "Model")
            ),
            "mileage": summary_data.get("mileage") or _read_visible_field(
                lambda: _safe_input_value(driver, "Mileage")
            ),
            "price": summary_data.get("price") or _read_visible_field(
                lambda: _safe_input_value(driver, "Price")
            ),
            "body_style": _read_visible_field(
                lambda: _safe_combobox_text(driver, "Body style")
            ),
            "exterior_color": summary_data.get("exterior_color") or _read_visible_field(
                lambda: _safe_combobox_text(driver, "Exterior colour")
                or _safe_combobox_text(driver, "Exterior color")
            ),
            "interior_color": summary_data.get("interior_color") or _read_visible_field(
                lambda: _safe_combobox_text(driver, "Interior colour")
                or _safe_combobox_text(driver, "Interior color")
            ),
            "vehicle_condition": summary_data.get("vehicle_condition") or _read_visible_field(
                lambda: _safe_combobox_text(driver, "Vehicle condition")
            ),
            "fuel_type": summary_data.get("fuel_type") or _read_visible_field(
                lambda: _safe_combobox_text(driver, "Fuel type")
            ),
            "transmission": summary_data.get("transmission") or _read_visible_field(
                lambda: _safe_combobox_text(driver, "Transmission")
            ),
            "description": summary_data.get("description") or _read_visible_field(
                lambda: _safe_description(driver)
            ),
        }

        collected_posts = [post_data]
        print(f"Extracted image URLs count: {len(post_data.get('image_urls') or [])}")
        for idx, image_url in enumerate(post_data.get("image_urls") or [], start=1):
            print(f"Image URL {idx}: {image_url}")
        print("[Reposting] All extracted listing details:", flush=True)
        for field in (
            "year", "make", "model", "mileage", "price", "exterior_color",
            "interior_color", "fuel_type", "transmission", "vehicle_condition",
            "description",
        ):
            print(f"[Reposting]   {field}={post_data.get(field)!r}", flush=True)
        print(f"[Reposting] image_urls={len(post_data.get('image_urls') or [])}", flush=True)
        print(collected_posts, flush=True)

        listing_name_for_actions = " ".join(
            p for p in [post_data.get("year"), post_data.get("make"), post_data.get("model")] if p
        ).strip()
        listing_price_for_actions = post_data.get("price") or ""

        try:
            _mark_listing_pending_and_wait(
                driver,
                listing_name_for_actions,
                listing_price_for_actions,
            )
            print(
                f"[Reposting] Source marked pending: {listing_name_for_actions!r}",
                flush=True,
            )
        except Exception as pending_error:
            # Pending is an optional safety step. Keep the extracted listing
            # usable so the repost can still be published.
            print(
                f"[Reposting] Warning: could not mark source pending; continuing: {pending_error}",
                flush=True,
            )

        return {"ok": True, "url": target_url, "posts": collected_posts}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
