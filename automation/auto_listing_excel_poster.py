from automation.diagnostics import event as diagnostic_event, traced
import base64
from automation.task_control import cancellable, cancellable_sleep
import builtins as _builtins
import requests
import sys
 
import re,json
import shutil
import pandas as pd
from pathlib import Path
 
import os, time
from datetime import datetime
from selenium import webdriver
import hashlib
import unicodedata
from difflib import SequenceMatcher
 
 
 
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
 
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import re 
from urllib.parse import unquote, urlsplit, urlunsplit
from automation.cdp_login import close_posting_tab, open_posting_tab
from automation.runtime_paths import runtime_log_path, screenshots_dir


RUNTIME_LOG_PATH = runtime_log_path()


def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _builtins.print(*args, **kwargs)
    try:
        message = " ".join(str(a) for a in args)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(RUNTIME_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [Poster] {message}\n")
    except Exception:
        pass
 
 
# ðŸ“˜ Helper Functions
def _normalize_header(value):
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_empty(value):
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return str(value).strip() == ""


def _clean_value(value):
    if _is_empty(value):
        return ""
    return str(value).strip()


def _clean_optional_value(value):
    cleaned = _clean_value(value)
    return cleaned if cleaned else None


def _norm_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null", "nan"}:
        return ""
    return text


FACEBOOK_MARKETPLACE_COLORS = (
    "Black", "Blue", "Brown", "Gold", "Green", "Gray", "Pink", "Purple",
    "Red", "Silver", "Orange", "White", "Yellow", "Charcoal", "Off white",
    "Tan", "Beige", "Burgundy", "Turquoise",
)


def _closest_facebook_color(value):
    """Map arbitrary dealer colour text to one mandatory Marketplace option."""
    raw = _norm_text(value)
    normalized = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
    exact = {color.casefold(): color for color in FACEBOOK_MARKETPLACE_COLORS}
    if normalized in exact:
        return exact[normalized]

    # Longer/specific phrases come first. Word boundaries prevent values such
    # as "brownstone" from accidentally matching "brown".
    aliases = (
        (("off white", "offwhite", "ivory", "cream", "pearl"), "Off white"),
        (("charcoal", "graphite", "gunmetal", "slate"), "Charcoal"),
        (("burgundy", "maroon", "wine"), "Burgundy"),
        (("turquoise", "teal", "aqua", "cyan"), "Turquoise"),
        (("beige", "sand", "khaki"), "Beige"),
        (("tan", "camel"), "Tan"),
        (("gray", "grey"), "Gray"),
        (("silver", "aluminum", "aluminium"), "Silver"),
        (("gold", "champagne"), "Gold"),
        (("orange",), "Orange"),
        (("purple", "violet"), "Purple"),
        (("pink",), "Pink"),
        (("brown", "bronze", "copper"), "Brown"),
        (("green", "olive"), "Green"),
        (("blue", "navy", "cobalt"), "Blue"),
        (("red", "scarlet", "crimson"), "Red"),
        (("yellow",), "Yellow"),
        (("white", "snow"), "White"),
        (("black", "midnight"), "Black"),
    )
    padded = f" {normalized} "
    matches = []
    for alias_order, (names, facebook_color) in enumerate(aliases):
        for name in names:
            position = padded.find(f" {name} ")
            if position >= 0:
                matches.append((position, alias_order, facebook_color))
    if matches:
        # For compound values such as "Black / Red", use the first colour
        # stated by the source, which is normally the primary colour.
        return min(matches)[2]

    # Safely recover misspellings such as "sliver", "gren", and "charcol".
    # Compare individual words and the full normalized value against both the
    # Facebook names and known aliases. Require a strong score and a clear lead
    # over the next colour so an ambiguous paint name is never guessed wildly.
    fuzzy_inputs = [normalized] + normalized.split()
    fuzzy_terms = []
    for names, facebook_color in aliases:
        fuzzy_terms.extend((name, facebook_color) for name in names)
    fuzzy_terms.extend(
        (re.sub(r"[^a-z0-9]+", " ", color.casefold()).strip(), color)
        for color in FACEBOOK_MARKETPLACE_COLORS
    )
    scores_by_color = {}
    for source_term in fuzzy_inputs:
        if len(source_term) < 3:
            continue
        for known_term, facebook_color in fuzzy_terms:
            score = SequenceMatcher(None, source_term, known_term).ratio()
            scores_by_color[facebook_color] = max(
                score, scores_by_color.get(facebook_color, 0.0)
            )
    ranked = sorted(
        ((score, color) for color, score in scores_by_color.items()),
        reverse=True,
    )
    if ranked:
        best_score, best_color = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score >= 0.78 and best_score - second_score >= 0.08:
            return best_color

    # Facebook provides no Other option. Gray is the neutral deterministic
    # fallback, ensuring both mandatory colour fields always receive a value.
    return "Gray"


def _normalize_marketplace_vehicle_settings(data):
    normalized = dict(data or {})
    vehicle_type = _norm_text(normalized.get("vehicle_type")).casefold()
    other_vehicle_types = {"other", "motorcycle", "motorcycle/bike", "motorbike", "bike"}
    normalized["vehicle_type"] = "Other" if vehicle_type in other_vehicle_types else "Car"
    normalized["vehicle_condition"] = _norm_text(normalized.get('vehicle_condition'))
    # Colours can come from Excel, manual posting, scraper integrations, and
    # other callers. Preserve the supplied values regardless of source.

    transmission = _norm_text(normalized.get("transmission")).casefold()
    normalized["transmission"] = (
        "Manual transmission" if transmission.startswith("manual") else "Automatic transmission"
    )

    fuel = _norm_text(normalized.get("fuel_type")).casefold().replace("-", " ")
    fuel = re.sub(r"\s+", " ", fuel).strip()
    fuel_aliases = {
        "gas": "Gasoline",
        "gasoline": "Gasoline",
        "gasoline fuel": "Gasoline",
        "diesal": "Diesel",
        "diesel": "Diesel",
        "diesel fuel": "Diesel",
        "electric": "Electric",
        "electronic": "Electric",
        "electroic": "Electric",
        "petrol": "Petrol",
        "hybrid": "Hybrid",
        "flex": "Flex",
        "flex fuel": "Flex",
        "plug in hybrid": "Plug-in hybrid",
        "plugin hybrid": "Plug-in hybrid",
        "other": "Other",
    }
    normalized["fuel_type"] = fuel_aliases.get(fuel, _norm_text(normalized.get("fuel_type")) or "Other")
    return normalized


def _find_first_column(columns, aliases):
    alias_set = {_normalize_header(a) for a in aliases}
    for col in columns:
        n = _normalize_header(col)
        if n in alias_set:
            return col
    for col in columns:
        n = _normalize_header(col)
        for alias in alias_set:
            if alias and (alias in n or n in alias):
                return col
    return None


def _combobox_label_xpath(label_text):
    """
    Match comboboxes regardless of color/colour spelling or whether Facebook
    puts role="combobox" on the label itself or on an element inside it.
    """
    normalized = str(label_text or "").strip()
    variants = {normalized}
    if normalized:
        variants.add(normalized.replace("color", "colour"))
        variants.add(normalized.replace("colour", "color"))
    text_clauses = []
    aria_clauses = []
    for v in sorted(variants):
        if v:
            safe = v.lower().replace('"', '\\"')
            text_clauses.append(
                ".//*[self::span or self::div][translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                f"\"{safe}\"]"
            )
            aria_clauses.append(
                "translate(normalize-space(@aria-label), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                f"\"{safe}\""
            )
    if not text_clauses:
        text_clauses = [".//*[self::span or self::div][normalize-space()='']"]
    label_condition = " or ".join(text_clauses)
    aria_condition = " or ".join(aria_clauses) or "normalize-space(@aria-label)=''"
    return (
        f"//label[@role='combobox'][{label_condition}]"
        f" | //label[{label_condition}]//*[@role='combobox']"
        f" | //*[@role='combobox' and ({aria_condition})]"
        f" | //*[@role='combobox'][{label_condition}]"
    )


def _visible_vehicle_type_choices(driver):
    """Read only the vehicle menu, including text-only listbox choices."""
    return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length))
          && getComputedStyle(el).visibility !== 'hidden';
        const norm = value => (value || '').replace(/\s+/g, ' ').trim();
        const combos = [...document.querySelectorAll('[role="combobox"]')].filter(el => visible(el)
          && (norm(el.getAttribute('aria-label')).toLowerCase() === 'vehicle type'
            || [...el.querySelectorAll('span,div')].some(n => norm(n.textContent).toLowerCase() === 'vehicle type')));
        const combo = combos[0];
        if (!combo) return [];
        const ids = norm((combo.getAttribute('aria-controls') || '') + ' ' + (combo.getAttribute('aria-owns') || '')).split(' ').filter(Boolean);
        const owned = ids.map(id => document.getElementById(id)).filter(visible);
        const roots = ids.length ? owned : [...document.querySelectorAll('[role="listbox"],[role="menu"]')].filter(visible);
        const known = new Set(['car', 'car/van', 'motorcycle', 'power sport', 'motorhome/caravan',
          'trailer', 'boat', 'commercial/industrial', 'other']);
        const choices = [], seen = new Set();
        const add = el => {
          const text = norm(el.innerText || el.textContent);
          if (!visible(el) || el.closest('[role="combobox"]') || el.getAttribute('aria-disabled') === 'true'
            || !(known.has(text.toLowerCase()) || text.toLowerCase().startsWith('car'))
            || seen.has(text.toLowerCase())) return;
          seen.add(text.toLowerCase()); choices.push([el, text]);
        };
        for (const root of roots) {
          const items = [...root.querySelectorAll('[role="option"],[role="menuitem"],[role="radio"],[role="button"]')];
          items.forEach(add);
          // Facebook may omit option roles. Use exact leaf text within the menu,
          // never a matching value displayed in the closed combobox or page.
          [...root.querySelectorAll('span,div')].filter(el =>
            ![...el.children].some(child => norm(child.textContent) === norm(el.textContent))).forEach(add);
        }
        if (!ids.length && !roots.length && combo.getAttribute('aria-expanded') !== 'false') {
          [...document.querySelectorAll('[role="option"],[role="menuitem"]')].forEach(add);
        }
        return choices;
    """) or []


def _open_vehicle_type_menu(driver, timeout=25):
    """Avoid toggling an open menu; retry clicks lost during form re-renders."""
    last_error = None
    for method in ('javascript', 'native', 'actions', 'keyboard'):
        if _visible_vehicle_type_choices(driver):
            return
        try:
            combo = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(
                (By.XPATH, _combobox_label_xpath('Vehicle type'))))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", combo)
            # An expanded-but-empty menu may still be rendering. Wait before
            # resetting it so a second click doesn't close a working menu.
            if combo.get_attribute('aria-expanded') == 'true':
                try:
                    WebDriverWait(driver, timeout).until(lambda d: _visible_vehicle_type_choices(d))
                    return
                except Exception:
                    combo.send_keys(Keys.ESCAPE)
                    combo = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(
                        (By.XPATH, _combobox_label_xpath('Vehicle type'))))
            if method == 'native':
                combo.click()
            elif method == 'actions':
                ActionChains(driver).move_to_element(combo).pause(0.15).click().perform()
            elif method == 'keyboard':
                combo.send_keys(Keys.SPACE)
            else:
                driver.execute_script('arguments[0].click();', combo)
            WebDriverWait(driver, timeout).until(lambda d: _visible_vehicle_type_choices(d))
            return
        except Exception as exc:
            last_error = exc
            print(f'[AutoListing] Vehicle type menu did not open with {method}; retrying.')
    raise RuntimeError('Vehicle type menu did not show any selectable choices after four opening attempts') from last_error


def _marketplace_combo(driver, label):
    for combo in driver.find_elements(By.XPATH, _combobox_label_xpath(label)):
        if combo.is_displayed() and combo.is_enabled() and combo.get_attribute('aria-disabled') != 'true':
            return combo
    return False


def _marketplace_combo_choices(driver, label):
    combo = _marketplace_combo(driver, label)
    if not combo:
        return []
    return driver.execute_script(r"""
      const combo=arguments[0], norm=v=>(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>!!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length))
        && getComputedStyle(el).visibility !== 'hidden';
      const ids=norm((combo.getAttribute('aria-controls')||'')+' '+(combo.getAttribute('aria-owns')||'')).split(' ').filter(Boolean);
      const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(visible)
        : [...document.querySelectorAll('[role="listbox"],[role="menu"]')].filter(visible);
      let items=[];
      for(const root of roots) {
        let options=[...root.querySelectorAll('[role="option"],[role="menuitem"],[role="radio"],[role="button"]')].filter(visible);
        if(!options.length) options=[...root.querySelectorAll('span,div')].filter(el=>visible(el)
          && ![...el.children].some(child=>norm(child.textContent)===norm(el.textContent)));
        items.push(...options);
      }
      if(!ids.length && !roots.length && combo.getAttribute('aria-expanded')!=='false')
        items=[...document.querySelectorAll('[role="option"]')].filter(visible);
      const seen=new Set(), result=[];
      for(const el of items) {
        const text=norm(el.innerText||el.textContent);
        if(text && el.getAttribute('aria-disabled')!=='true' && !seen.has(text.toLowerCase())) {
          seen.add(text.toLowerCase()); result.push([el,text]);
        }
      }
      return result;
    """, combo) or []


def _marketplace_combo_value(driver, label):
    combo = _marketplace_combo(driver, label)
    if not combo:
        return ''
    return _norm_text(driver.execute_script(r"""
      const el=arguments[0], label=arguments[1], norm=v=>(v||'').replace(/\s+/g,' ').trim();
      const input=el.matches('input') ? el : el.querySelector('input');
      if(input && norm(input.value)) return norm(input.value);
      const clone=el.cloneNode(true);
      clone.querySelectorAll('[role="option"],[role="listbox"],[role="menu"]').forEach(n=>n.remove());
      let value=norm(clone.textContent);
      for(const name of [label,label.replace('colour','color'),label.replace('color','colour')])
        if(value.toLowerCase().startsWith(name.toLowerCase())) {value=value.slice(name.length).trim(); break;}
      return value;
    """, combo, label))


def _open_marketplace_combo(driver, label, timeout=25):
    last_error = None
    # CDP Chrome may be behind another window; DOM clicks still reach React.
    for method in ('javascript', 'native', 'actions', 'keyboard'):
        if _marketplace_combo_choices(driver, label):
            return
        try:
            combo = WebDriverWait(driver, timeout).until(lambda d: _marketplace_combo(d, label))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", combo)
            if combo.get_attribute('aria-expanded') == 'true':
                try:
                    WebDriverWait(driver, timeout).until(lambda d: _marketplace_combo_choices(d, label))
                    return
                except Exception:
                    combo.send_keys(Keys.ESCAPE)
                    combo = WebDriverWait(driver, timeout).until(lambda d: _marketplace_combo(d, label))
            if method == 'javascript':
                driver.execute_script('arguments[0].click();', combo)
            elif method == 'native':
                combo.click()
            elif method == 'actions':
                ActionChains(driver).move_to_element(combo).click().perform()
            else:
                combo.send_keys(Keys.SPACE)
            WebDriverWait(driver, timeout).until(lambda d: _marketplace_combo_choices(d, label))
            return
        except Exception as exc:
            last_error = exc
            print(f'[AutoListing] {label} menu did not open with {method}; retrying.')
    raise RuntimeError(f'{label} menu did not show selectable choices after four opening attempts') from last_error


def _select_marketplace_combo(driver, label, desired, fallback=None, timeout=25, exact_only=False):
    """Wait for the menu, try the source value and then verify the fallback."""
    if label.casefold() == 'vehicle type':
        fallback = None
    candidates = []
    values = [desired]
    if label.casefold() == 'body style' and _norm_text(desired).casefold() == 'saloon':
        values.append('Sedan')
    values.append(fallback)
    for value in values:
        if _norm_text(value) and _norm_text(value).casefold() not in [v.casefold() for v in candidates]:
            candidates.append(_norm_text(value))
    wait_seconds = timeout
    last_error = None
    available = []
    for candidate in candidates:
        selected = _marketplace_combo_value(driver, label)
        if selected.casefold() == candidate.casefold():
            return selected
        for method in ('javascript', 'native', 'actions', 'keyboard'):
            try:
                _open_marketplace_combo(driver, label, wait_seconds)
                choices = _marketplace_combo_choices(driver, label)
                available = [text for _, text in choices]
                matches = [(el, text) for el, text in choices if text.casefold() == candidate.casefold()]
                if not matches and not exact_only:
                    matches = [(el, text) for el, text in choices if text.casefold().startswith(candidate.casefold())]
                if not matches:
                    # Keep the existing menu open while moving to Other.
                    break
                option, text = matches[0]
                driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", option)
                if method == 'javascript':
                    driver.execute_script(
                        "arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                        "arguments[0].dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));arguments[0].click();", option)
                elif method == 'native':
                    option.click()
                elif method == 'actions':
                    ActionChains(driver).move_to_element(option).click().perform()
                else:
                    option.send_keys(Keys.ENTER)
                WebDriverWait(driver, wait_seconds).until(
                    lambda d: _marketplace_combo_value(d, label).casefold() == text.casefold())
                print(f'[AutoListing] {label} retained after {method} selection: {text}')
                return text
            except Exception as exc:
                last_error = exc
                print(f'[AutoListing] {label} selection did not retain {candidate!r} with {method}; retrying.')
    raise RuntimeError(f'No matching {label} option retained for {desired!r} or fallback {fallback!r}. '
                       f'Visible choices: {", ".join(available) or "none"}') from last_error


def _fill_optional_color(driver, label, value, timeout=25, strict=False):
    """Use one shared time budget for finding, opening and selecting a color."""
    deadline = time.monotonic() + timeout
    combo = None
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError(f'{label} exceeded {timeout} seconds')
        return seconds
    desired = _closest_facebook_color(value)
    if not _norm_text(value):
        return None
    try:
        combo = WebDriverWait(driver, remaining()).until(lambda d: _marketplace_combo(d, label))
        if _marketplace_combo_value(driver, label).casefold() == desired.casefold():
            return desired
        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", combo)
        choices = WebDriverWait(driver, remaining()).until(lambda d: _marketplace_combo_choices(d, label))
        match = next(((el, text) for el, text in choices if text.casefold() == desired.casefold()), None)
        if match is None and not strict:
            match = next(((el, text) for el, text in choices if text.casefold() == 'gray'), None)
        if match is None:
            raise ValueError('No matching color option available')
        option, selected = match
        remaining()
        driver.execute_script("""
            const option=arguments[0], color=arguments[1].trim().toLowerCase();
            const span=[...option.querySelectorAll('span')].find(el =>
                (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase() === color);
            (span || option).click();
        """, option, selected)
        WebDriverWait(driver, remaining()).until(lambda d: _marketplace_combo_value(d, label).casefold() == selected.casefold())
        print(f'[AutoListing] {label}: {selected}', flush=True)
        return selected
    except Exception as exc:
        if combo is not None:
            try:
                combo.send_keys(Keys.ESCAPE)
            except Exception:
                pass
        print(f'[AutoListing][FIELD SKIPPED] {label}: {exc}', flush=True)
        if strict:
            raise RuntimeError(f'Saved {label} could not be retained: {value}') from exc
        return None


def _fill_saved_text_field(driver, label, value):
    value = _norm_text(value)
    if not value:
        return
    controls = driver.find_elements(By.XPATH, _input_for_label_xpath(label))
    control = next((item for item in controls if item.is_displayed()), None)
    if control is None:
        _select_marketplace_combo(driver, label, value, timeout=25, exact_only=True)
        return
    control.clear()
    control.send_keys(value)
    if _norm_text(control.get_attribute('value')) != value:
        raise RuntimeError(f'Saved {label} could not be retained.')


def _input_for_label_xpath(label_text):
    safe = str(label_text or "").strip().lower().replace('"', '\\"')
    return (
        "//label[.//span[translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
        f"\"{safe}\"]]//input"
    )


def _fill_optional_trim(driver, model_input, model_value, trim_value):
    """Try an existing Trim control once; otherwise continue with Model alone."""
    from automation.task_control import check_cancelled
    trim_value = _norm_text(trim_value)
    if not trim_value:
        return model_value
    combo = None
    try:
        check_cancelled()
        combo = _marketplace_combo(driver, 'Trim')
        if combo:
            if _marketplace_combo_value(driver, 'Trim').casefold() != trim_value.casefold():
                driver.execute_script('arguments[0].click();', combo)
                choices = WebDriverWait(driver, 25).until(lambda current: _marketplace_combo_choices(current, 'Trim'))
                option = next((element for element, text in choices if text.casefold() == trim_value.casefold()), None)
                if option is None:
                    raise ValueError('Saved trim is not offered')
                driver.execute_script('arguments[0].click();', option)
                WebDriverWait(driver, 25).until(
                    lambda current: _marketplace_combo_value(current, 'Trim').casefold() == trim_value.casefold())
        else:
            control = next((element for element in driver.find_elements(By.XPATH, _input_for_label_xpath('Trim'))
                            if element.is_displayed() and element.is_enabled()), None)
            if control is None:
                raise ValueError('No visible Trim field')
            control.clear()
            control.send_keys(trim_value)
            if _norm_text(control.get_attribute('value')) != trim_value:
                raise ValueError('Trim value was not retained')
        print(f'[AutoListing] Optional Trim filled: {trim_value}')
        return model_value
    except Exception as exc:
        check_cancelled()
        if combo is not None and combo:
            try:
                combo.send_keys(Keys.ESCAPE)
            except Exception:
                check_cancelled()
        print(f'[AutoListing] Optional Trim unavailable; skipped, keeping Model unchanged: {exc}')
    return model_value


def _input_for_any_label_xpath(*label_texts):
    return " | ".join(_input_for_label_xpath(label) for label in label_texts if str(label or "").strip())


def _find_image_columns(columns):
    """Find all columns that contain image paths/URLs with flexible keyword matching."""
    image_aliases = [
        # Basic image terms
        "image", "images", "photo", "photos", "picture", "pictures", "pic", "pics", "img", "imgs",
        # Image with variations (underscore, no space, camelCase)
        "image url", "image_url", "imageurl", "image link", "image_link", "imagelink",
        "image path", "image_path", "imagepath", "image file", "imagefile",
        "photo url", "photo_url", "photourl", "photo link", "photo_link", "photolink",
        "photo path", "photo_path", "photopath", "photo file", "photofile",
        "picture url", "picture_url", "pictureurl", "picture link", "picture_link",
        "picture path", "picture_path", "picturepath", "pic url", "pic_url",
        "pic link", "pic_link", "pic path", "pic_path",
        # Cover/additional images
        "cover image", "cover_image", "coverphoto", "main image", "main_image",
        # Vehicle specific
        "vehicle image", "vehicle photo", "car image", "car photo",
        # Single word variations
        "imaged", "pix", "pixs",
    ]
    matched = []
    for col in columns:
        n = _normalize_header(col)
        if any(alias in n for alias in image_aliases):
            matched.append(col)
    return list(dict.fromkeys(matched))[:10]


def read_excel_flexible(excel_path):
    frames = []
    try:
        workbook = pd.read_excel(excel_path, sheet_name=None, dtype=object)
        if isinstance(workbook, dict):
            frames = [df for df in workbook.values() if isinstance(df, pd.DataFrame)]
        elif isinstance(workbook, pd.DataFrame):
            frames = [workbook]
    except Exception:
        # Fallback for csv-like uploads renamed as excel.
        try:
            csv_df = pd.read_csv(excel_path, dtype=object)
            frames = [csv_df]
        except Exception:
            return []

    posts = []
    for df in frames:
        if df is None or df.empty:
            continue

        df = df.dropna(how="all")
        if df.empty:
            continue

        columns = list(df.columns)
        vehicle_type_col = _find_first_column(columns, [
            "vehicle type", "vehicle", "type", "car type", "listing type"
        ])
        year_col = _find_first_column(columns, [
            "year", "model year", "vehicle year", "car year"
        ])
        make_col = _find_first_column(columns, [
            "make", "brand", "car make", "vehicle make"
        ])
        model_col = _find_first_column(columns, [
            "model", "car model", "vehicle model", "trim model"
        ])
        mileage_col = _find_first_column(columns, [
            "mileage", "miles", "odometer", "kilometers", "kms", "km"
        ])
        price_col = _find_first_column(columns, [
            "price", "amount", "cost", "listing price", "sale price", "asking price"
        ])
        body_style_col = _find_first_column(columns, [
            "body style", "body", "style", "vehicle body", "car body"
        ])
        exterior_color_col = _find_first_column(columns, [
            "exterior color", "exterior colour",
            "outside color", "outside colour",
            "outer color", "outer colour",
            "color exterior", "colour exterior"
        ])
        interior_color_col = _find_first_column(columns, [
            "interior color", "interior colour",
            "inside color", "inside colour",
            "inner color", "inner colour",
            "color interior", "colour interior"
        ])
        vehicle_condition_col = _find_first_column(columns, [
            "vehicle condition", "condition", "car condition"
        ])
        fuel_type_col = _find_first_column(columns, [
            "fuel type", "fuel", "gas type", "engine fuel"
        ])
        transmission_col = _find_first_column(columns, [
            "transmission", "gearbox", "trans", "drive transmission"
        ])
        description_col = _find_first_column(columns, [
            "description", "details", "item description", "vehicle description", "car description", "info"
        ])
        location_col = _find_first_column(columns, [
            "location", "city", "area", "town", "district", "zip", "zipcode", "postal code", "address"
        ])

        explicit_image_cols = []
        for i in range(0, 11):
            c = _find_first_column(columns, [f"image {i}", f"image{i}", f"img {i}", f"img{i}", f"photo {i}", f"photo{i}"])
            if c:
                explicit_image_cols.append(c)
        image_cols = list(dict.fromkeys(explicit_image_cols + _find_image_columns(columns)))[:10]

        required_columns = {
            "vehicle type": vehicle_type_col,
            "make": make_col,
            "model": model_col,
            "mileage": mileage_col,
            "price": price_col,
            "body style": body_style_col,
        }
        missing_required = [name for name, col in required_columns.items() if not col]
        if not image_cols:
            missing_required.append("at least one image column (e.g. image1, image 1, photo1)")
        if missing_required:
            raise ValueError("Missing required Excel column(s): " + ", ".join(missing_required))

        for row_index, row in df.iterrows():
            post_data = {
                "vehicle_type": _clean_value(row[vehicle_type_col]) if vehicle_type_col else "",
                "year": _clean_value(row[year_col]) if year_col else "",
                "make": _clean_value(row[make_col]) if make_col else "",
                "model": _clean_value(row[model_col]) if model_col else "",
                "mileage": _clean_value(row[mileage_col]) if mileage_col else "",
                "description": _clean_optional_value(row[description_col]) if description_col else None,
                "price": _clean_value(row[price_col]) if price_col else "",
                "body_style": _clean_value(row[body_style_col]) if body_style_col else "",
                "exterior_color": _clean_optional_value(row[exterior_color_col]) if exterior_color_col else None,
                "interior_color": _clean_optional_value(row[interior_color_col]) if interior_color_col else None,
                "vehicle_condition": _clean_optional_value(row[vehicle_condition_col]) if vehicle_condition_col else None,
                "fuel_type": _clean_optional_value(row[fuel_type_col]) if fuel_type_col else None,
                "transmission": _clean_optional_value(row[transmission_col]) if transmission_col else None,
                "location": _clean_optional_value(row[location_col]) if location_col else None,
                # Keep legacy keys present for current caller compatibility.
                "author": "",
                "genre": "",
            }

            image_count = 0
            seen_values = set()
            for col in image_cols:
                value = _clean_value(row[col])
                if not value:
                    continue
                if value in seen_values:
                    continue
                seen_values.add(value)
                image_count += 1
                post_data[f"image_path{image_count}"] = value
                if image_count >= 10:
                    break

            # Skip truly blank rows.
            non_image_values = [
                post_data.get("vehicle_type", ""),
                post_data.get("year", ""),
                post_data.get("make", ""),
                post_data.get("model", ""),
                post_data.get("mileage", ""),
                post_data.get("price", ""),
                post_data.get("body_style", ""),
                post_data.get("exterior_color", ""),
                post_data.get("interior_color", ""),
                post_data.get("vehicle_condition", ""),
                post_data.get("fuel_type", ""),
                post_data.get("transmission", ""),
                post_data.get("description", ""),
                post_data.get("location", ""),
            ]
            if not any(non_image_values) and image_count == 0:
                continue

            required_values = {
                "vehicle type": post_data.get("vehicle_type"),
                "make": post_data.get("make"),
                "model": post_data.get("model"),
                "mileage": post_data.get("mileage"),
                "price": post_data.get("price"),
                "body style": post_data.get("body_style"),
            }
            missing_values = [name for name, val in required_values.items() if not _norm_text(val)]
            if missing_values:
                row_no = int(row_index) + 2
                raise ValueError(
                    f"Row {row_no}: missing required value(s): " + ", ".join(missing_values)
                )

            if image_count < 1:
                row_no = int(row_index) + 2
                raise ValueError(f"Row {row_no}: at least one image is required.")

            try:
                mileage_num = int(float(str(post_data.get("mileage", "")).replace(",", "")))
            except Exception:
                mileage_num = 0
            if mileage_num <= 300:
                row_no = int(row_index) + 2
                raise ValueError(f"Row {row_no}: mileage must be greater than 300.")

            posts.append(post_data)

    return posts
def decode_base64_image(image_data, save_dir="uploads"):
    """Decode base64 image strings and save as temp file."""
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    match = re.match(r"data:image/(png|jpeg|jpg);base64,(.*)", image_data)
    if not match:
        return None
    ext = match.group(1)
    img_data = base64.b64decode(match.group(2))
    filename = f"decoded_image.{ext}"
    file_path = os.path.join(save_dir, filename)
    with open(file_path, "wb") as f:
        f.write(img_data)
    return file_path

def read_first_record_excel(excel_path):
    """Reads the first row from Excel and returns as dict."""
    df = pd.read_excel(excel_path, dtype=str).fillna("")

    if df.empty:
        return None
    first_row = df.iloc[0]
    return {
        "author": str(first_row.get("Book Author", "")),
        "description": str(first_row.get("Book Description", "")),
        "price": str(first_row.get("Book Price", "")),
        "genre": str(first_row.get("Book Genre", "")),
        "image_path1": str(first_row.get("Book Image", ""))

    }

def handle_exc(func, path, excinfo):
    """
    func  : the function that failed (os.unlink, os.rmdir)
    path  : path to the file
    excinfo: exception info
    """
    try:
        os.chmod(path, 0o777)  # make file writable
        func(path)              # retry the operation
    except Exception as e:
        print(f"Failed to delete {path}: {e}")


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

    # Backward compatibility: old names replaced '@' with '_'.
    normalized_account = account_text.replace("@", "_")
    for stem in stems:
        if stem == normalized_account or stem.replace("@", "_") == normalized_account:
            return os.path.join(session_dir, f"{stem}.pkl")

    raise FileNotFoundError(
        f"Session file not found for account '{account_text}'. Available sessions: {', '.join(stems) or 'none'}"
    )


def _canonical_image_source(value):
    """Normalize image source strings so semantically same URLs dedupe reliably."""
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        return ""

    if text.startswith("http://") or text.startswith("https://"):
        try:
            parsed = urlsplit(text)
            return urlunsplit(
                (
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path,
                    parsed.query,
                    "",
                )
            )
        except Exception:
            return text
    return text


def _file_sha1(path):
    """Return SHA1 of file contents for duplicate-by-content protection."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_group_name(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _simplify_group_name(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch.isspace():
            cleaned.append(ch)
            continue
        # Keep emoji symbols for flexible matching
        if unicodedata.category(ch) == "So":
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    simplified = re.sub(r"\s+", " ", "".join(cleaned)).strip().lower()
    return simplified


def _match_group_name(candidate, target):
    if not candidate or not target:
        return False
    cand_norm = _normalize_group_name(candidate)
    targ_norm = _normalize_group_name(target)
    if cand_norm == targ_norm:
        return True
    cand_simple = _simplify_group_name(candidate)
    targ_simple = _simplify_group_name(target)
    if cand_simple and targ_simple and cand_simple == targ_simple:
        return True
    if cand_norm and targ_norm and (cand_norm in targ_norm or targ_norm in cand_norm):
        return True
    if cand_simple and targ_simple and (cand_simple in targ_simple or targ_simple in cand_simple):
        return True
    return False


def _extract_group_name_from_checkbox(checkbox):
    try:
        spans = checkbox.find_elements(By.XPATH, ".//span[@dir='auto']")
    except Exception:
        spans = []
    texts = []
    for span in spans:
        try:
            t = (span.text or "").strip()
        except Exception:
            t = ""
        if t:
            texts.append(t)
    for t in texts:
        low = t.lower()
        if "member" in low:
            continue
        if low in {"public", "private"}:
            continue
        return t
    return texts[0] if texts else ""


def _find_scrollable_child(driver, root):
    try:
        return driver.execute_script(
            """
            const root = arguments[0];
            if (!root) return null;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            while (walker.nextNode()) {
              const el = walker.currentNode;
              const style = window.getComputedStyle(el);
              if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                  el.scrollHeight > el.clientHeight + 10) {
                return el;
              }
            }
            return null;
            """,
            root,
        )
    except Exception:
        return None


def _select_groups_for_listing(driver, group_names):
    targets = [str(t or "").strip() for t in (group_names or []) if str(t or "").strip()]
    if not targets:
        return []

    try:
        header = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//span[contains(translate(normalize-space(.),"
                    " 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
                    " 'list in your groups')]",
                )
            )
        )
    except Exception as e:
        print(f"⚠️ Group list header not found: {e}")
        return []

    try:
        container = header.find_element(
            By.XPATH,
            "./ancestor::div[.//div[@role='checkbox']][1]",
        )
    except Exception:
        container = driver

    try:
        WebDriverWait(driver, 20).until(
            lambda d: len(container.find_elements(By.XPATH, ".//div[@role='checkbox']")) > 0
        )
    except Exception:
        pass

    scroll_el = _find_scrollable_child(driver, container) or container
    matched = set()
    max_rounds = 8

    for _ in range(max_rounds):
        try:
            checkboxes = container.find_elements(By.XPATH, ".//div[@role='checkbox']")
        except Exception:
            checkboxes = []

        for checkbox in checkboxes:
            name = _extract_group_name_from_checkbox(checkbox)
            if not name:
                continue

            for target in targets:
                if target in matched:
                    continue
                if not _match_group_name(name, target):
                    continue

                try:
                    is_checked = (checkbox.get_attribute("aria-checked") or "").lower() == "true"
                except Exception:
                    is_checked = False
                if not is_checked:
                    try:
                        checkbox.click()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].click();", checkbox)
                        except Exception:
                            pass
                    cancellable_sleep(0.2)
                matched.add(target)
                break

        if len(matched) >= len(targets):
            break

        try:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;",
                scroll_el,
            )
        except Exception:
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
        cancellable_sleep(0.6)

    return list(matched)


 
 
# ======================================================
# ðŸ§  Main Function: Facebook Marketplace Auto Poster
# ======================================================

def _marketplace_publish_confirmed(driver):
    """Require a destination page or explicit successful publication message."""
    path = urlsplit(driver.current_url).path.rstrip('/').lower()
    if path.startswith('/marketplace/item/') or path == '/marketplace/you/selling':
        return True
    return bool(driver.execute_script(r"""
        const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        return [...document.querySelectorAll('[role="alert"],[role="status"],[role="dialog"]')]
          .filter(visible).some(el => /(?:your listing (?:is now live|has been published|was published)|listing published|successfully published)/i.test(el.innerText || ''));
    """))


@cancellable
@traced('POST')
def post_to_fb_marketplace(data, account, post_mode="simple", group_names=None):
    print("post_to_fb_marketplace : Starting Facebook Marketplace posting process...")
    indicator=False
    posting_error = None
    active_field = "initialization"
    entered_fields = {}

    def debug_field_success(field_name, value=None):
        value_text = f" Value: {value}" if value not in (None, "") else ""
        print(f"[AutoListing][FIELD SUCCESS] {field_name} entered/selected.{value_text}", flush=True)
        from automation.reposting import EDIT_FIELDS
        for key, label in EDIT_FIELDS.items():
            if label.casefold() == str(field_name).casefold() and key != 'clean_title':
                entered_fields[key] = value if value not in (None, '') else data.get(key)
        if str(field_name or "").strip().casefold() == "publish":
            print('[AutoListing] Waiting 7 seconds after Publish...', flush=True)
            cancellable_sleep(7)

    def debug_field_failure(field_name, error):
        print(f"[AutoListing][FIELD FAILED] {field_name}: {error}", flush=True)

    if not isinstance(data, dict):
        print("❌ Invalid post data: expected dict.")
        return False
    request_payload = data
    data = _normalize_marketplace_vehicle_settings(data)
    strict_fields = bool(data.get('_require_exact_fields'))

    if not post_mode:
        post_mode = "simple"
    post_mode = str(post_mode).strip().lower()
    if post_mode not in {"simple", "group"}:
        post_mode = str(data.get("post_action") or "simple").strip().lower()
    if post_mode not in {"simple", "group"}:
        post_mode = "simple"

    if group_names is None:
        group_names = data.get("group_names") or []
    if isinstance(group_names, str):
        group_names = [group_names]

    required_fields = {
        "vehicle type": _norm_text(data.get("vehicle_type")),
        "make": _norm_text(data.get("make")),
        "model": _norm_text(data.get("model")),
        "mileage": _norm_text(data.get("mileage")),
        "price": _norm_text(data.get("price")),
    }
    if _norm_text(data.get("vehicle_type")).casefold() == "car":
        required_fields["body style"] = _norm_text(data.get("body_style"))
    missing_required = [name for name, value in required_fields.items() if not value]
    if missing_required:
        for field_name in missing_required:
            debug_field_failure(field_name.title(), "required value is missing from the Excel row")
        print(f"❌ Missing required post value(s): {', '.join(missing_required)}")
        return False

    try:
        mileage_num = int(float(required_fields["mileage"].replace(",", "")))
    except Exception:
        mileage_num = 0
    if mileage_num <= 300:
        debug_field_failure("Mileage", "value must be greater than 300")
        print("❌ Mileage must be greater than 300.")
        return False

    precheck_images = []
    for i in range(1, 21):
        img_value = _norm_text(data.get(f"image_path{i}"))
        if img_value:
            precheck_images.append(img_value)
    if not precheck_images:
        debug_field_failure("Images", "at least one image is required")
        print("❌ At least one image is required.")
        return False

    logs = []
    def log(msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)
        logs.append(msg)

    # Posting uses a dedicated tab in the account's persistent CDP Chrome.
    # The account lock prevents two posting jobs from driving the same profile.
    driver, posting_handle, original_handles, posting_lock = open_posting_tab(account)
    try:
        driver.set_page_load_timeout(180)
        wait = WebDriverWait(driver, 25)
        actions = ActionChains(driver)
    except BaseException:
        # A disconnected browser here used to bypass the later cleanup block
        # and leave the account's posting lock held until process restart.
        close_posting_tab(driver, posting_handle, original_handles, posting_lock)
        raise

    # ------------------------------
    # Go to Marketplace Create Page
    # ------------------------------
    create_vehicle_url = "https://www.facebook.com/marketplace/create/vehicle"
    create_form_ready = False
    try:
        for attempt in range(1, 3):
            driver.get(create_vehicle_url)
            try:
                WebDriverWait(driver, 90).until(
                    lambda d: (
                        "/marketplace/create/vehicle" in str(d.current_url or "").lower()
                        and bool(d.find_elements(By.XPATH, "//input[@type='file' and not(@disabled)]"))
                    )
                )
                create_form_ready = True
                break
            except Exception:
                print(
                    f"[AutoListing] Vehicle form not ready after 90s "
                    f"(attempt {attempt}/2), URL: {driver.current_url}"
                )
    except Exception:
        close_posting_tab(driver, posting_handle, original_handles, posting_lock)
        raise
    if not create_form_ready:
        print("[AutoListing] Vehicle form still unavailable after two extended waits")
    print(f"[AutoListing] Create page URL: {driver.current_url}")

     
    # --- IMAGE HANDLING (mandatory) ---
    successful_uploads = 0
    try:
        image_sources = []
        if isinstance(data, dict):
            for i in range(1, 21):
                src = _norm_text(data.get(f"image_path{i}"))
                if src:
                    image_sources.append(src)

        if not image_sources:
            log("❌ No image paths found in row.")

        save_folder = screenshots_dir()
        resolved_upload_paths = []
        unique_tag = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        def _uploaded_preview_count():
            try:
                return len(driver.find_elements(By.XPATH, "//*[@aria-label='Remove photo']"))
            except Exception:
                return 0

        for idx, source in enumerate(image_sources, start=1):
            source = source.strip().strip('"').strip("'")
            if not source:
                continue

            local_path = ""
            target_path = os.path.join(save_folder, f"upload_{unique_tag}_{idx}.jpg")

            if source.startswith("data:image"):
                try:
                    b64 = source.split(",", 1)[1] if "," in source else source
                    with open(target_path, "wb") as f:
                        f.write(base64.b64decode(b64))
                    local_path = target_path
                except Exception as e:
                    log(f"⚠️ Base64 image {idx} failed: {e}")
                    continue
            elif source.startswith("http://") or source.startswith("https://"):
                try:
                    response = requests.get(source, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                    if response.status_code == 200:
                        with open(target_path, "wb") as f:
                            f.write(response.content)
                        local_path = target_path
                    else:
                        log(f"⚠️ Image URL {idx} returned status {response.status_code}")
                        continue
                except Exception as e:
                    log(f"⚠️ Image URL {idx} failed: {e}")
                    continue
            else:
                p = Path(source)
                if p.exists() and p.is_file():
                    local_path = str(p.resolve())
                else:
                    log(f"⚠️ Local image {idx} not found: {source}")
                    continue

            if local_path and Path(local_path).exists():
                resolved_upload_paths.append(os.path.normpath(local_path))

        # Upload once: send all files in a single send_keys call to avoid re-sending
        # earlier files when the same multi-file input is reused in a loop.
        unique_upload_paths = []
        seen_upload_paths = set()
        for path in resolved_upload_paths:
            key = os.path.normcase(path)
            if key in seen_upload_paths:
                continue
            seen_upload_paths.add(key)
            unique_upload_paths.append(path)

        if unique_upload_paths:
            try:
                file_input = WebDriverWait(driver, 60).until(
                    EC.presence_of_element_located((By.XPATH, "//input[@type='file' and not(@disabled)]"))
                )
                try:
                    driver.execute_script(
                        "arguments[0].style.display='block';"
                        "arguments[0].style.visibility='visible';"
                        "arguments[0].removeAttribute('hidden');",
                        file_input,
                    )
                except Exception:
                    pass

                before_count = _uploaded_preview_count()
                # Multi-file inputs accept newline-delimited absolute paths.
                file_input.send_keys("\n".join(unique_upload_paths))
                try:
                    WebDriverWait(driver, 30).until(
                        lambda d: _uploaded_preview_count() >= before_count + len(unique_upload_paths)
                    )
                except Exception:
                    pass
                successful_uploads = len(unique_upload_paths)
                debug_field_success(
                    "Images", f"{successful_uploads}/{len(unique_upload_paths)} uploaded"
                )
            except Exception as e:
                debug_field_failure("Images", e)

        # Best-effort duplicate cleanup in preview:
        # if same preview image appears multiple times, remove extra copies.
        try:
            if successful_uploads > 0:
                duplicate_removed = driver.execute_script(
                    """
                    const buttons = Array.from(document.querySelectorAll('[aria-label="Remove photo"]'));
                    const seen = new Set();
                    let removed = 0;
                    for (let i = 0; i < buttons.length; i++) {
                        const btn = buttons[i];
                        const card = btn.closest('[role="button"], div, li') || btn.parentElement;
                        if (!card) continue;
                        const img = card.querySelector('img');
                        const src = img && img.src ? img.src : '';
                        if (!src) continue;
                        if (seen.has(src)) {
                            try {
                                btn.click();
                                removed += 1;
                            } catch (e) {}
                        } else {
                            seen.add(src);
                        }
                    }
                    return removed;
                    """
                )
                if duplicate_removed:
                    print(f"Removed duplicate preview image(s): {duplicate_removed}")
                    cancellable_sleep(1)
        except Exception:
            pass

    except Exception as e:
      debug_field_failure("Images", e)
    # Optional videos never block the required listing fields or publishing.
    from automation.video_upload import upload_optional_videos
    video_submissions = upload_optional_videos(driver, data.get("video_paths", []), log=log)
    diagnostic_event('POST_MEDIA', 'SUMMARY', account=account,
                     photos_uploaded=successful_uploads,
                     videos_requested=len(data.get('video_paths') or []), videos_submitted=video_submissions)

    # ======================================================
    # Step 6: Fill Post Details (step 1: vehicle type only)
    # ======================================================
    try:
        active_field = "Images"
        if successful_uploads < 1:
            raise Exception("Mandatory images could not be uploaded.")

        active_field = "Vehicle type"
        try:
            vehicle_type_combo = WebDriverWait(driver, 25).until(
                EC.element_to_be_clickable(
                    (By.XPATH, _combobox_label_xpath("Vehicle type"))
                )
            )
        except Exception as exc:
            print(
                f"[AutoListing][FIELD FAILED] Vehicle type: initial combobox HTML "
                f"was not found or was not clickable: {exc}",
                flush=True,
            )
            raise
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", vehicle_type_combo)

        def _xpath_literal(value):
            if "'" not in value:
                return f"'{value}'"
            if '"' not in value:
                return f'"{value}"'
            return "concat('" + value.replace("'", "',\"'\",'") + "')"

        def _open_vehicle_type_combo():
            _open_vehicle_type_menu(driver)

        def _selected_vehicle_type_text():
            try:
                values = driver.execute_script(
                    r"""
                    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                    const norm = value => (value || '').replace(/\s+/g, ' ').trim();
                    const combos = [...document.querySelectorAll('[role="combobox"]')].filter(visible);
                    for (const combo of combos) {
                      const aria = norm(combo.getAttribute('aria-label')).toLowerCase();
                      const whole = norm(combo.innerText || combo.textContent);
                      if (aria !== 'vehicle type' && !whole.toLowerCase().startsWith('vehicle type')) continue;
                      const input = combo.matches('input') ? combo : combo.querySelector('input');
                      const inputValue = norm(input && input.value);
                      if (inputValue && inputValue.toLowerCase() !== 'vehicle type') return inputValue;
                      const clone = combo.cloneNode(true);
                      clone.querySelectorAll('[role="option"], [role="listbox"]').forEach(node => node.remove());
                      clone.querySelectorAll('span, div').forEach(node => {
                        if (norm(node.textContent).toLowerCase() === 'vehicle type') node.remove();
                      });
                      let value = norm(clone.innerText || clone.textContent);
                      value = value.replace(/^vehicle type\s*/i, '').trim();
                      if (value && value.toLowerCase() !== 'vehicle type') return value;
                    }
                    return '';
                    """
                )
                return _norm_text(values)
            except Exception as exc:
                print(f"[AutoListing] Vehicle type verification could not read combobox value: {exc}", flush=True)
                return ""

        def _is_vehicle_type_selected(option_text):
            option_text = option_text.strip()
            if not option_text:
                return False
            try:
                combo_text = _selected_vehicle_type_text()
                wanted = option_text.casefold()
                actual = combo_text.casefold()
                return bool(
                    actual
                    and actual != "vehicle type"
                    and (
                        actual == wanted
                        or actual.startswith(wanted)
                        or wanted.startswith(actual)
                    )
                )
            except Exception:
                return False

        def _visible_vehicle_type_options():
            return _visible_vehicle_type_choices(driver)

        def _log_vehicle_type_failure(reason, exc=None):
            print(f"[AutoListing][VEHICLE TYPE DEBUG] {reason}", flush=True)
            if exc is not None:
                import traceback
                print(f"[AutoListing][VEHICLE TYPE DEBUG] Exception: {type(exc).__name__}: {exc!r}", flush=True)
                print(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)), flush=True)
            try:
                choices = [text for _, text in _visible_vehicle_type_options()]
                print(
                    f"[AutoListing][VEHICLE TYPE DEBUG] URL={driver.current_url!r}; "
                    f"visible options={choices!r}; selected value={_selected_vehicle_type_text()!r}",
                    flush=True,
                )
            except Exception as diagnostic_error:
                print(f"[AutoListing][VEHICLE TYPE DEBUG] Cannot inspect dropdown: {diagnostic_error!r}", flush=True)

        def _click_and_verify_vehicle_type(option, option_text, timeout=25):
            wanted = _norm_text(option_text).casefold()
            last_error = ""
            for method in ("javascript", "native", "actions", "keyboard"):
                try:
                    # Facebook frequently replaces the option node after the
                    # first pointer event, so locate a fresh matching node for
                    # every attempt.
                    matches = [
                        item for item, text in _visible_vehicle_type_options()
                        if _norm_text(text).casefold() == wanted
                        or _norm_text(text).casefold().startswith(wanted)
                        or wanted.startswith(_norm_text(text).casefold())
                    ]
                    if not matches:
                        _open_vehicle_type_combo()
                        matches = [
                            item for item, text in _visible_vehicle_type_options()
                            if _norm_text(text).casefold() == wanted
                            or _norm_text(text).casefold().startswith(wanted)
                            or wanted.startswith(_norm_text(text).casefold())
                        ]
                    if not matches:
                        raise Exception(f"matching option HTML disappeared for {option_text!r}")
                    current_option = matches[0]
                    driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", current_option)
                    if method == "actions":
                        ActionChains(driver).move_to_element(current_option).pause(0.15).click().perform()
                    elif method == "native":
                        current_option.click()
                    elif method == "javascript":
                        driver.execute_script(
                            "arguments[0].dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                            "arguments[0].dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));"
                            "arguments[0].click();",
                            current_option,
                        )
                    else:
                        current_option.send_keys(Keys.ENTER)
                    WebDriverWait(driver, timeout).until(
                        lambda d: _is_vehicle_type_selected(option_text)
                    )
                    print(f"[AutoListing] Vehicle type retained after {method} selection: {option_text}", flush=True)
                    return True
                except Exception as exc:
                    last_error = str(exc)
                    print(f"[AutoListing] Vehicle type {method} selection failed for {option_text!r}: {exc}", flush=True)
                    _log_vehicle_type_failure(f"Click/verification failed: method={method}, requested option={option_text!r}", exc)
            print(f"[AutoListing][FIELD FAILED] Vehicle type: option {option_text!r} was located but not retained; last_error={last_error}", flush=True)
            return False

        def _select_vehicle_type(option_text, timeout=25):
            option_text = option_text.strip()
            if not option_text:
                return False
            try:
                WebDriverWait(driver, timeout).until(
                    lambda d: bool(_visible_vehicle_type_options())
                )
                wanted = option_text.casefold()
                for option, text in _visible_vehicle_type_options():
                    if text.casefold() == wanted:
                        return _click_and_verify_vehicle_type(option, text)
                _log_vehicle_type_failure(f"No exact dropdown option matches {option_text!r}; checking supported prefix next.")
                return False
            except Exception as exc:
                print(f"[AutoListing][FIELD FAILED] Vehicle type: exact option lookup failed for {option_text!r}: {exc}", flush=True)
                _log_vehicle_type_failure(f"Exact lookup failed for {option_text!r}", exc)
                return False

        def _select_vehicle_type_starts_with(prefix, timeout=25):
            """Select first vehicle type option that starts with the given prefix (case-insensitive)."""
            prefix_lower = prefix.strip().lower()
            try:
                WebDriverWait(driver, timeout).until(
                    lambda d: bool(_visible_vehicle_type_options())
                )
                for option, text in _visible_vehicle_type_options():
                    if text.casefold().startswith(prefix_lower):
                        return text if _click_and_verify_vehicle_type(option, text) else ""
                _log_vehicle_type_failure(f"No visible dropdown option starts with {prefix!r}.")
                return ""
            except Exception as exc:
                print(f"[AutoListing][FIELD FAILED] Vehicle type: prefix option lookup failed for {prefix!r}: {exc}", flush=True)
                _log_vehicle_type_failure(f"Prefix lookup failed for {prefix!r}", exc)
                return ""

        def _select_case_insensitive_combo(label, desired, fallback=None, timeout=25, exact_only=False):
            return _select_marketplace_combo(driver, label, desired, fallback, timeout, exact_only)

        active_field = "Vehicle type"
        excel_vehicle_type = _norm_text(data.get("vehicle_type"))
        selected_vehicle_type = None
        excel_starts_with_car = excel_vehicle_type.lower().startswith("car") if excel_vehicle_type else False

        # Step 1: Try exact match from Excel data
        if excel_vehicle_type:
            if _is_vehicle_type_selected(excel_vehicle_type):
                selected_vehicle_type = excel_vehicle_type
            else:
                _open_vehicle_type_combo()
                if _select_vehicle_type(excel_vehicle_type):
                    selected_vehicle_type = excel_vehicle_type

        # Step 2: If exact match not found and Excel type starts with "car", try to find option starting with "car"
        if not selected_vehicle_type and excel_starts_with_car:
            _open_vehicle_type_combo()
            matched_car_type = _select_vehicle_type_starts_with("car")
            if matched_car_type:
                selected_vehicle_type = matched_car_type
        if not selected_vehicle_type:
            _log_vehicle_type_failure(f"Selection exhausted for requested vehicle type {excel_vehicle_type!r}.")
            raise Exception(
                f"Could not select and retain requested vehicle type {excel_vehicle_type!r}. "
                "Posting stopped; no fallback vehicle type will be selected."
            )

        if not _is_vehicle_type_selected(selected_vehicle_type):
            raise Exception(
                f"Vehicle type selection was not retained by Facebook: {selected_vehicle_type}"
            )
        selected_vehicle_type = _selected_vehicle_type_text() or selected_vehicle_type
        # Later fields must follow the requested type Facebook retained.
        data['vehicle_type'] = 'Car' if selected_vehicle_type.casefold().startswith('car') else 'Other'

        debug_field_success("Vehicle type", selected_vehicle_type)

        _fill_saved_text_field(driver, 'VIN', data.get('vin'))

        active_field = "Year"
        year_value = _norm_text(data.get("year"))
        if not year_value:
            raise Exception("Mandatory year is missing.")
        if year_value:
            year_combo = WebDriverWait(driver, 25).until(
                EC.element_to_be_clickable(
                    (By.XPATH, _combobox_label_xpath("Year"))
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", year_combo)
            try:
                year_combo.click()
            except Exception:
                driver.execute_script("arguments[0].click();", year_combo)
            cancellable_sleep(0.3)
            year_combo.send_keys(year_value)
            year_combo.send_keys(Keys.ENTER)
            debug_field_success("Year", year_value)
        cancellable_sleep(1)

        active_field = "Make"
        make_value = _norm_text(data.get("make"))
        if make_value:
            make_selected = False
            try:
                # Existing approach: direct Make input interaction.
                make_input = WebDriverWait(driver, 25).until(
                    EC.presence_of_element_located(
                        (By.XPATH, _input_for_label_xpath("Make"))
                    )
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", make_input)
                make_input.click()
                make_input.clear()
                make_input.send_keys(make_value)
                debug_field_success("Make", make_value)
                make_selected = True
            except Exception:
                pass

            if not make_selected:
                try:
                    # Fallback for Make-as-combobox layout: target only Make.
                    make_combo = WebDriverWait(driver, 25).until(
                        EC.element_to_be_clickable(
                        (By.XPATH, _combobox_label_xpath("Make"))
                        )
                    )
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", make_combo)
                    try:
                        make_combo.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", make_combo)
                    cancellable_sleep(0.3)
                    try:
                        driver.execute_script("arguments[0].focus();", make_combo)
                    except Exception:
                        pass

                    make_selected_xpath = (
                        f"{_combobox_label_xpath('Make')}"
                        "//span[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                        f"{_xpath_literal(make_value.lower())}]"
                    )

                    typed = False
                    try:
                        has_make_focus = driver.execute_script(
                            "return document.activeElement === arguments[0];", make_combo
                        )
                    except Exception:
                        has_make_focus = False

                    if has_make_focus:
                        try:
                            actions.move_to_element(make_combo).click().send_keys(make_value).send_keys(Keys.ENTER).perform()
                            typed = True
                            cancellable_sleep(0.8)
                        except Exception:
                            typed = False

                    if typed and driver.find_elements(By.XPATH, make_selected_xpath):
                        debug_field_success("Make (combobox typed fallback)", make_value)
                        make_selected = True
                    else:
                        make_option_xpath = (
                            "//span[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                            f"{_xpath_literal(make_value.lower())}]"
                        )
                        make_option = WebDriverWait(driver, 25).until(
                            EC.element_to_be_clickable((By.XPATH, make_option_xpath))
                        )
                        try:
                            make_option.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", make_option)
                        debug_field_success("Make (combobox option fallback)", make_value)
                        make_selected = True
                    cancellable_sleep(1)
                except Exception as exc:
                    debug_field_failure("Make fallback", f"value={make_value!r}; error={exc}")
            if not make_selected:
                raise Exception(f"Mandatory make could not be filled: {make_value}")
        else:
            raise Exception("Mandatory make is missing.")

        active_field = "Model"
        model_value = _norm_text(data.get("model"))
        if model_value:
            model_input = WebDriverWait(driver, 25).until(
                EC.presence_of_element_located(
                    (By.XPATH, _input_for_label_xpath("Model"))
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", model_input)
            model_input.click()
            model_input.clear()
            model_input.send_keys(model_value)
            debug_field_success("Model", model_value)
        else:
            raise Exception("Mandatory model is missing.")

        _fill_optional_trim(driver, model_input, model_value, data.get('trim'))

        active_field = "Mileage"
        mileage_value_raw = _norm_text(data.get("mileage"))
        if mileage_value_raw:
            mileage_to_use = mileage_value_raw
            mileage_num = int(float(mileage_value_raw.replace(",", "")))
            if mileage_num <= 300:
                raise Exception("Mileage must be greater than 300.")

            mileage_input = WebDriverWait(driver, 25).until(
                EC.presence_of_element_located(
                    (By.XPATH, _input_for_any_label_xpath("Mileage", "Odometer"))
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", mileage_input)
            mileage_input.click()
            mileage_input.clear()
            mileage_input.send_keys(mileage_to_use)
            debug_field_success("Mileage", mileage_to_use)
        else:
            raise Exception("Mandatory mileage is missing.")

        active_field = "Price"
        price_value = _norm_text(data.get("price"))
        if not price_value:
            raise Exception("Mandatory price is missing.")
        price_input = WebDriverWait(driver, 25).until(
            EC.presence_of_element_located(
                (By.XPATH, _input_for_label_xpath("Price"))
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", price_input)
        price_input.click()
        price_input.clear()
        price_input.send_keys(price_value)
        debug_field_success("Price", price_value)

        active_field = "Body style"
        body_style_value = _norm_text(data.get("body_style"))
        if _norm_text(data.get("vehicle_type")).casefold() == "car":
            if not body_style_value:
                raise Exception("Mandatory body style is missing.")
            body_style_selected = _select_case_insensitive_combo(
                "Body style", body_style_value, fallback=None if strict_fields else "Other", timeout=25
            )
            debug_field_success("Body style", body_style_selected)
        else:
            print("[AutoListing][FIELD SKIPPED] Body style: vehicle type is Other.", flush=True)

        for color_label, color_key in (("Exterior colour", "exterior_color"),
                                       ("Interior colour", "interior_color")):
            active_field = color_label
            selected_color = _fill_optional_color(driver, color_label, data.get(color_key), timeout=25, strict=strict_fields)
            if selected_color:
                entered_fields[color_key] = selected_color

        # Reposts must retain supplied fields. Fresh manual posts keep their
        # existing optional-field behavior.

        try:
            active_field = "Clean title"
            if not strict_fields or _norm_text(data.get('clean_title')):
                clean_title_checkbox = WebDriverWait(driver, 25).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//input[@type='checkbox' and @aria-label='This vehicle has a clean title.']")
                    )
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", clean_title_checkbox)
                is_checked = (clean_title_checkbox.get_attribute("aria-checked") or "").lower() == "true" or clean_title_checkbox.is_selected()
                clean_title_requested = _norm_text(data.get('clean_title', 'Yes')).casefold() not in {'no', 'false', '0'}
                if is_checked != clean_title_requested:
                    try:
                        clean_title_checkbox.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", clean_title_checkbox)
                debug_field_success("Clean title", "checked" if clean_title_requested else "unchecked")
        except Exception as exc:
            debug_field_failure("Clean title", exc)
            if strict_fields and str(data.get('clean_title') or '').strip():
                raise

        try:
            active_field = "Vehicle condition"
            if _norm_text(data.get('vehicle_condition')):
                condition_selected = _select_case_insensitive_combo(
                    "Vehicle condition", data['vehicle_condition'], fallback=None if strict_fields else "Very good", timeout=25
                )
                debug_field_success("Vehicle condition", condition_selected)
            else:
                print('[AutoListing][FIELD SKIPPED] Vehicle condition: no value supplied.', flush=True)
        except Exception as exc:
            debug_field_failure("Vehicle condition (optional)", exc)
            if strict_fields and _norm_text(data.get('vehicle_condition')):
                raise

        fuel_type_value = _norm_text(data.get("fuel_type")) or "Other"
        try:
            active_field = "Fuel type"
            fuel_selected = _select_case_insensitive_combo(
                "Fuel type", fuel_type_value, fallback=None if strict_fields else "Other", timeout=25
            )
            debug_field_success("Fuel type", fuel_selected)
        except Exception as exc:
            debug_field_failure("Fuel type (optional)", exc)
            if strict_fields:
                raise

        transmission_value = _norm_text(data.get("transmission")) or "Automatic transmission"
        try:
            active_field = "Transmission"
            transmission_selected = _select_case_insensitive_combo(
                "Transmission", transmission_value, fallback=None if strict_fields else "Automatic transmission", timeout=25
            )
            debug_field_success("Transmission", transmission_selected)
        except Exception as exc:
            debug_field_failure("Transmission (optional)", exc)
            if strict_fields:
                raise

        try:
            active_field = "Description"
            driver.execute_script("window.scrollTo(0, 0);")
            cancellable_sleep(1.5)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            cancellable_sleep(1.5)
        except Exception:
            pass

        try:
            # Never invent listing copy. Use only the description supplied by
            # the selected source; an empty source description stays empty.
            description_value = _norm_text(data.get("description"))
            if description_value:
                def _try_set_description():
                    desc_xpath = (
                        # Provided HTML: <span>Description</span> ... <textarea ...>
                        "//span[translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='description']"
                        "/ancestor::div[1]//textarea[1]"
                        " | //textarea[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'description')]"
                        " | //span[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'description')]"
                        "/following::textarea[1]"
                        " | //span[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'description')]"
                        "/following::*[@role='textbox'][1]"
                        " | (//textarea)[last()]"
                    )
                    try:
                        description_input = WebDriverWait(driver, 25).until(
                            EC.presence_of_element_located((By.XPATH, desc_xpath))
                        )
                    except Exception:
                        description_input = None

                    if not description_input:
                        return False

                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", description_input)
                    try:
                        WebDriverWait(driver, 25).until(EC.element_to_be_clickable(description_input))
                    except Exception:
                        pass

                    try:
                        description_input.click()
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].click();", description_input)
                        except Exception:
                            pass

                    # Prefer JS set + input event (more reliable on FB React)
                    try:
                        driver.execute_script(
                            """
                            const el = arguments[0];
                            const val = arguments[1];
                            const tag = (el.tagName || '').toLowerCase();
                            if (tag === 'textarea' || tag === 'input') {
                              el.value = val;
                            } else {
                              el.textContent = val;
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            """,
                            description_input,
                            description_value,
                        )
                        cancellable_sleep(0.2)
                    except Exception:
                        pass

                    # Fallback typing
                    try:
                        current_val = (description_input.get_attribute("value") or "").strip()
                    except Exception:
                        current_val = ""
                    if not current_val or len(current_val) < 10:
                        try:
                            description_input.send_keys(Keys.CONTROL, "a")
                            description_input.send_keys(Keys.BACKSPACE)
                        except Exception:
                            pass
                        description_input.send_keys(description_value)

                    return True

                ok = _try_set_description()
                if not ok:
                    debug_field_failure("Description", "field not found; skipped")
                    if strict_fields:
                        raise RuntimeError('Saved description could not be filled.')
                else:
                    debug_field_success("Description")
            else:
                print("[AutoListing][FIELD SKIPPED] Description: no value supplied.", flush=True)
        except Exception as exc:
            debug_field_failure("Description", exc)
            if strict_fields:
                raise

        from automation.posting_recovery import wait_for_action, restore_empty_fields

        def repair_cleared_fields():
            def fill(key, label, value):
                if key == 'vehicle_type':
                    _open_vehicle_type_combo()
                    if not _select_vehicle_type(str(value), timeout=25):
                        raise RuntimeError('Cleared vehicle type could not be restored.')
                elif key == 'description':
                    if not _try_set_description():
                        raise RuntimeError('Cleared description could not be restored.')
                elif key in {'year', 'body_style', 'vehicle_condition', 'fuel_type', 'transmission'}:
                    _select_case_insensitive_combo(label, str(value), timeout=25)
                elif key in {'exterior_color', 'interior_color'}:
                    _fill_optional_color(driver, label, value, timeout=25, strict=True)
                elif key == 'make' and _marketplace_combo(driver, label):
                    _select_case_insensitive_combo(label, str(value), timeout=25)
                elif key == 'clean_title':
                    return  # A checkbox is not a cleared text/dropdown field.
                else:
                    aliases = ('Mileage', 'Odometer') if key == 'mileage' else (label,)
                    control = WebDriverWait(driver, 25).until(EC.element_to_be_clickable(
                        (By.XPATH, _input_for_any_label_xpath(*aliases))))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", control)
                    control.click()
                    control.clear()
                    control.send_keys(str(value))
                    control.send_keys(Keys.TAB)
            return restore_empty_fields(driver, entered_fields, fill)

        cancellable_sleep(1)
        try:
            # Next step (click if present)
            try:
                next_btn = wait_for_action(driver, 'Next', repair_cleared_fields, alternative='Publish')
                if next_btn is not None:
                    try:
                        next_btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", next_btn)
                    print("Clicked Next")
                    cancellable_sleep(2)
            except Exception:
                print("Next button not found; proceeding to Publish.")

            # Description sometimes appears on the next step for some accounts/layouts.
            try:
                if description_value:
                    try:
                        ok2 = _try_set_description()
                    except Exception:
                        ok2 = False
                    if ok2:
                        debug_field_success("Description (after Next)")
            except Exception:
                pass

            if post_mode == "group":
                try:
                    # Wait up to 10s for group section; if not present, skip.
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located(
                                (
                                    By.XPATH,
                                    "//span[contains(translate(normalize-space(.),"
                                    " 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
                                    " 'list in your groups')]",
                                )
                            )
                        )
                    except Exception:
                        print("Group section not found within 10s; skipping group selection.")
                    else:
                        selected_groups = _select_groups_for_listing(driver, group_names)
                        if selected_groups:
                            print(f"Selected groups: {len(selected_groups)}/{len(group_names) or len(selected_groups)}")
                        else:
                            print("⚠️ No matching groups selected; continuing to publish.")
                except Exception as e:
                    print(f"⚠️ Group selection step failed: {e}")
        except Exception as e:
            print(f"Skipping Next/groups due to error: {e}")

        try:
            active_field = "Publish"
            publish_btn = wait_for_action(driver, 'Publish', repair_cleared_fields)
            try:
                  
                publish_btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", publish_btn)
            print("Clicked Publish")
            diagnostic_event("PUBLISH", "CLICKED", account=account, title=data.get("title"))
            if data.get('_require_publish_confirmation'):
                try:
                    WebDriverWait(driver, 45).until(_marketplace_publish_confirmed)
                    diagnostic_event("PUBLISH", "CONFIRMED_BY_UI", account=account, title=data.get("title"))
                    if data.get('_manual_record_id'):
                        # Reuse the confirmed page; URL capture is optional and
                        # does not add a second publication check or navigation.
                        confirmed_url = str(driver.current_url or '')
                        if re.search(r'/marketplace/item/\d+(?:/|\?|$)', confirmed_url):
                            request_payload['_confirmed_listing_url'] = confirmed_url
                        else:
                            try:
                                links = driver.execute_script('''
                                  return [...document.querySelectorAll('[role="dialog"] a[href*="/marketplace/item/"],[role="alert"] a[href*="/marketplace/item/"]')]
                                    .filter(a => !!a.getClientRects().length && /view (?:your )?listing/i.test(a.innerText || a.getAttribute('aria-label') || ''))
                                    .map(a => a.href);
                                ''') or []
                                if len(set(links)) == 1:
                                    request_payload['_confirmed_listing_url'] = links[0]
                            except Exception:
                                pass
                except Exception as exc:
                    raise RuntimeError(
                        'Publish was clicked, but Facebook did not confirm publication; '
                        'saved listing date, clicks, and details were not changed.'
                    ) from exc
            # Facebook frequently publishes without changing the create-page
            # URL or showing stable confirmation text. A completed Publish
            # click is therefore the success boundary for this flow.
            if not data.get("_require_publish_confirmation"):
                diagnostic_event("PUBLISH", "CLICK_ONLY_REVIEW_ON_FACEBOOK", account=account, title=data.get("title"))
            indicator = True
            debug_field_success("Publish", "listing marked as posted")
        except Exception as exc:
            debug_field_failure("Publish", exc)
            indicator = False

        cancellable_sleep(1)
    except Exception as e:
        debug_field_failure(active_field, e)
        print(f"❌ Vehicle details step failed: {e}", flush=True)
        if active_field == "Vehicle type":
            import traceback
            print(f"[AutoListing][VEHICLE TYPE DEBUG] Final exception: {type(e).__name__}: {e!r}", flush=True)
            print(''.join(traceback.format_exception(type(e), e, e.__traceback__)), flush=True)
        indicator = False
        posting_error = RuntimeError(f'{active_field}: {e}')
    finally:
        close_posting_tab(driver, posting_handle, original_handles, posting_lock)
        cancellable_sleep(1)
          
       
         
        

    if posting_error is not None:
        raise posting_error
    return indicator



