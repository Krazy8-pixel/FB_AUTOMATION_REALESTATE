import json
from automation.task_control import cancellable, cancellable_sleep
from automation.diagnostics import event as diagnostic_event, traced
import os
import pickle
import random
import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from automation.browser_settings import apply_background_mode_option
from automation.runtime_paths import data_dir
from automation.runtime_paths import sessions_dir
from automation.chromedriver import build_chrome_driver
from automation.cdp_login import load_current_cdp_cookies

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


class ImportCancelled(RuntimeError):
    pass


def _safe_key(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    return "".join(ch if ch.isalnum() else "_" for ch in text)


def _groups_store_path(account_email: str) -> Path:
    base = Path(data_dir("groups"))
    return base / f"groups_{_safe_key(account_email)}.json"


def load_groups(account_email: str) -> dict:
    """
    Returns a dict with keys:
      - account_email
      - updated_at (ISO string) or None
      - groups (list)
    """
    store_path = _groups_store_path(account_email)
    if not store_path.exists():
        return {"account_email": account_email, "updated_at": None, "groups": []}

    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
    except Exception:
        return {"account_email": account_email, "updated_at": None, "groups": []}

    if not isinstance(raw, dict):
        return {"account_email": account_email, "updated_at": None, "groups": []}

    groups = raw.get("groups") or []
    if not isinstance(groups, list):
        groups = []

    return {
        "account_email": raw.get("account_email") or account_email,
        "updated_at": raw.get("updated_at"),
        "groups": groups,
    }


def save_groups(account_email: str, groups: list) -> None:
    store_path = _groups_store_path(account_email)
    payload = {
        "account_email": account_email,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": groups if isinstance(groups, list) else [],
    }
    store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_group_record(account_email: str, group_id: str) -> bool:
    data = load_groups(account_email)
    groups = data.get("groups") or []

    before = len(groups)
    groups = [g for g in groups if str((g or {}).get("id", "")).strip() != str(group_id or "").strip()]
    if len(groups) == before:
        return False

    save_groups(account_email, groups)
    return True


def summarize_groups(account_email: str) -> dict:
    data = load_groups(account_email)
    groups = data.get("groups") or []
    updated_at = data.get("updated_at")
    last_imported_at = None
    if updated_at:
        try:
            normalized = str(updated_at).replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            last_imported_at = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            last_imported_at = str(updated_at)
    return {
        "group_count": len([g for g in groups if isinstance(g, dict)]),
        "last_imported_at": last_imported_at,
    }


def _apply_browser_safety_patches(driver) -> None:
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


def _resolve_session_file(session_dir: str, account_email: str) -> str:
    direct = os.path.join(session_dir, f"{account_email}.pkl")
    if os.path.exists(direct):
        return direct

    sanitized = re.sub(r"[\\/:*?\"<>|]", "_", account_email) + ".pkl"
    candidate = os.path.join(session_dir, sanitized)
    if os.path.exists(candidate):
        return candidate

    raise FileNotFoundError(f"Session cookies not found for account: {account_email}")


def _normalize_group_link(href: str) -> str:
    url = str(href or "").strip()
    if not url:
        return ""
    url = url.split("?", 1)[0].split("#", 1)[0]
    url = url.rstrip("/")
    return url


def _group_id_from_link(link: str) -> str:
    url = _normalize_group_link(link)
    m = re.search(r"/groups/([^/]+)", url)
    return m.group(1) if m else url


def _parse_member_count_label(text: str) -> str | None:
    t = str(text or "")
    m = re.search(r"(\d[\d,.\s]*\d|\d+)\s*([KMkm])?\s+(members?|people|person)\b", t, re.IGNORECASE)
    if not m:
        return None
    num = re.sub(r"\s+", "", m.group(1)).replace(",", "")
    suffix = (m.group(2) or "").upper()
    return f"{num}{suffix}" if suffix else num


def _parse_privacy(text: str) -> str | None:
    t = str(text or "").lower()
    if "private group" in t or re.search(r"\bprivate\b", t):
        return "private"
    if "public group" in t or re.search(r"\bpublic\b", t):
        return "public"
    return None


def _extract_group_page_info(driver, fallback_name: str = "", fallback_link: str = "") -> dict:
    """Read the group header, preferring Facebook's visible H1 structure."""
    info = {}
    try:
        info = driver.execute_script(
            r"""
            const clean = value => (value || '').replace(/\s+/g, ' ').trim();
            const main = document.querySelector('div[role="main"]') || document;
            const ignoredHeadings = new Set(['notifications', 'facebook', 'menu', 'chats']);
            const headings = Array.from(main.querySelectorAll('h1')).filter(h => {
              const text = clean(h.innerText || h.textContent);
              if (!text || ignoredHeadings.has(text.toLowerCase()) || h.getAttribute('aria-hidden') === 'true') return false;
              const style = window.getComputedStyle(h);
              return style.display !== 'none' && style.visibility !== 'hidden';
            });

            // The real group heading sits beside metadata such as "Public
            // group" and "66.2K members". Notification/dialog headings do not.
            let heading = null;
            let headerText = '';
            for (const candidate of headings) {
              let parent = candidate.parentElement;
              for (let i = 0; i < 5 && parent; i++, parent = parent.parentElement) {
                const text = clean(parent.innerText || parent.textContent);
                const lower = text.toLowerCase();
                if (lower.includes('member') || lower.includes('public group') || lower.includes('private group')) {
                  heading = candidate;
                  headerText = text;
                  break;
                }
              }
              if (heading) break;
            }

            return {
              name: heading ? clean(heading.innerText || heading.textContent) : '',
              headerText: headerText,
              url: window.location.href || ''
            };
            """
        ) or {}
    except Exception as exc:
        print(f"[Groups][Info] H1 extraction failed: {exc}")
        info = {}

    name = str(info.get("name") or "").strip()
    header_text = str(info.get("headerText") or "").strip()
    page_link = _normalize_group_link(info.get("url"))

    # Selenium fallback for layouts where the script cannot see the heading.
    if not name:
        try:
            heading = WebDriverWait(driver, 5).until(
                EC.visibility_of_element_located(
                    (
                        By.XPATH,
                        "//div[@role='main']//h1[normalize-space() and "
                        "not(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')="
                        "'notifications')]",
                    )
                )
            )
            name = str(heading.text or heading.get_attribute("textContent") or "").strip()
            if not header_text:
                header_text = str(
                    driver.execute_script(
                        "return (arguments[0].parentElement.innerText || arguments[0].parentElement.textContent || '').trim();",
                        heading,
                    )
                    or ""
                )
        except Exception:
            pass

    if not name:
        name = str(fallback_name or "").strip()
    if not page_link or "/groups/" not in page_link:
        page_link = _normalize_group_link(fallback_link)

    result = {
        "name": name,
        "link": page_link,
        "header_text": header_text,
        "privacy": _parse_privacy(header_text),
        "member_count_label": _parse_member_count_label(header_text),
    }
    print(
        f"[Groups][Info] name={result['name']!r} link={result['link']!r} "
        f"privacy={result['privacy']!r} members={result['member_count_label']!r}"
    )
    return result


@traced('GROUP_IMPORT')
@cancellable
def import_groups_from_facebook(
    account_email: str,
    progress_callback=None,
    max_scroll_rounds: int = 200,
    stop_event: threading.Event | None = None,
) -> list:
    """
    Visits https://www.facebook.com/groups/joins and imports only Buy & Sell groups.
    """
    def _progress(event: dict):
        try:
            if callable(progress_callback):
                progress_callback(event)
        except Exception:
            pass

    def _check_stop():
        if stop_event is not None and stop_event.is_set():
            _progress({"event": "status", "message": "Stopping import..."})
            raise ImportCancelled("Import cancelled by user.")

    _check_stop()
    cookies = load_current_cdp_cookies(account_email)
    if cookies is None:
        session_dir = sessions_dir()
        session_file = _resolve_session_file(session_dir, account_email)
        with open(session_file, "rb") as f:
            cookies = pickle.load(f)

    options = Options()
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    apply_background_mode_option(options)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    driver = build_chrome_driver(options)
    _apply_browser_safety_patches(driver)
    wait = WebDriverWait(driver, 5)

    def _cleanup():
        try:
            driver.quit()
        except Exception:
            pass

    def _sleep_with_stop(seconds: float):
        end = time.time() + float(seconds or 0)
        while time.time() < end:
            _check_stop()
            cancellable_sleep(0.2)

    def _is_base_group_link(link: str) -> bool:
        url = _normalize_group_link(link)
        if not url:
            return False
        if url.startswith("/groups/"):
            url = _ensure_facebook_url(url)
        if not url.startswith("https://www.facebook.com/groups/") and not url.startswith("http://www.facebook.com/groups/"):
            return False
        return bool(re.match(r"^https?://www\.facebook\.com/groups/[^/]+$", url))

    try:
        _check_stop()
        _progress({"event": "status", "message": "Opening Facebook..."})
        driver.get("https://www.facebook.com/")
        cancellable_sleep(random.uniform(0.6, 1.0))

        for cookie in cookies:
            _check_stop()
            if isinstance(cookie, dict) and "sameSite" not in cookie:
                cookie["sameSite"] = "Lax"
            try:
                driver.add_cookie(cookie)
            except Exception:
                continue

        _check_stop()
        driver.refresh()
        cancellable_sleep(random.uniform(0.6, 1.0))

        joins_url = "https://www.facebook.com/groups/joins"
        _progress({"event": "status", "message": "Loading joined groups..."})
        driver.get(joins_url)
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//a[@role='link' and contains(@href, '/groups/')]")))
        except Exception:
            pass

        existing = load_groups(account_email).get("groups") or []
        existing_map = {}
        for g in existing:
            if isinstance(g, dict) and g.get("id"):
                existing_map[str(g["id"])] = g

        new_added = 0
        existing_total = len(existing_map)
        seen_links = set(_normalize_group_link((g or {}).get("link")) for g in existing if isinstance(g, dict))

        def _extract_join_groups():
            main = None
            try:
                main = driver.find_element(By.XPATH, "//div[@role='main']")
            except Exception:
                pass
            scope = main if main is not None else driver
            anchors = scope.find_elements(By.XPATH, ".//a[@role='link' and contains(@href, '/groups/')]")
            results = []
            local_seen = set()
            for a in anchors:
                _check_stop()
                try:
                    raw_href = a.get_attribute("href")
                except Exception:
                    raw_href = ""
                href = _ensure_facebook_url(raw_href)
                if not _is_base_group_link(href):
                    continue
                href = _normalize_group_link(href)
                if href in seen_links or href in local_seen:
                    continue
                try:
                    name = (a.text or "").strip()
                except Exception:
                    name = ""
                if not name:
                    try:
                        name = (a.get_attribute("aria-label") or "").strip()
                    except Exception:
                        name = ""
                if not name:
                    continue
                local_seen.add(href)
                results.append((href, name))
            return results

        rounds_without_new = 0
        last_height = 0
        main_handle = driver.current_window_handle
        group_handle = None
        try:
            driver.execute_script("window.open('about:blank', '_blank');")
            handles = driver.window_handles
            group_handle = next((h for h in handles if h != main_handle), None)
        except Exception:
            group_handle = None

        for round_idx in range(int(max_scroll_rounds or 0)):
            _check_stop()
            join_groups = _extract_join_groups()
            added_this_round = 0

            for href, join_name in join_groups:
                _check_stop()
                seen_links.add(href)

                if group_handle:
                    driver.switch_to.window(group_handle)
                    driver.get(href)
                else:
                    driver.switch_to.window(main_handle)
                    driver.get(href)
                _sleep_with_stop(2.0)

                page_info = _extract_group_page_info(driver, fallback_name=join_name, fallback_link=href)
                header_name = page_info.get("name") or join_name
                page_link = page_info.get("link") or href

                # Joined-group cards often include member counts, activity, or
                # other labels in their visible text.  Comparing that text to
                # the clean page heading rejected valid groups.  Validate the
                # stable group identity instead, when Facebook exposes it.
                requested_group_id = _group_id_from_link(href)
                page_group_id = _group_id_from_link(page_link) if page_link else ""
                if page_group_id and requested_group_id and page_group_id != requested_group_id:
                    _progress({"event": "status", "message": f"Skipped (different group): {join_name}"})
                else:
                    privacy = page_info.get("privacy")
                    members_label = page_info.get("member_count_label")

                    has_buy_sell = _has_buy_sell_tab(driver)
                    if has_buy_sell is True:
                        group_id = page_group_id or requested_group_id
                        group_obj = {
                            "id": group_id,
                            "name": header_name,
                            "link": page_link,
                            "member_count_label": members_label,
                            "privacy": privacy,
                        }

                        is_new = str(group_id) not in existing_map
                        existing_map[str(group_id)] = group_obj
                        if is_new:
                            new_added += 1

                        total_count = len(existing_map)
                        added_this_round += 1
                        save_groups(account_email, list(existing_map.values()))
                        diagnostic_event('GROUP_IMPORT_ITEM', 'SAVED', account=account_email,
                                         listing_id=group_id, title=header_name)
                        _progress(
                            {
                                "event": "group",
                                "message": f"Imported: {header_name}",
                                "new_count": new_added,
                                "total_count": total_count,
                                "existing_total": existing_total,
                            }
                        )
                    else:
                        _progress({"event": "status", "message": f"Skipped (no Buy & Sell): {header_name}"})

                _sleep_with_stop(0.6)
                try:
                    driver.switch_to.window(main_handle)
                except Exception:
                    pass

            if added_this_round == 0:
                rounds_without_new += 1
            else:
                rounds_without_new = 0

            if rounds_without_new >= 4:
                break

            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
            _sleep_with_stop(0.6)

            try:
                height = int(driver.execute_script("return document.body.scrollHeight") or 0)
            except Exception:
                height = 0
            if height and height == last_height:
                rounds_without_new += 1
            last_height = height

        final_groups = list(existing_map.values())
        save_groups(account_email, final_groups)
        _progress({"event": "status", "message": f"Collected {len(final_groups)} groups ({new_added} new)."})
        return final_groups
    finally:
        _cleanup()


@traced('GROUP_ADD_BY_LINK')
@cancellable
def add_buy_sell_group_by_link(
    account_email: str,
    group_link: str,
) -> dict:
    """
    Visit a group link, verify Buy & Sell tab, extract details, and save to DB.
    """
    link = _ensure_facebook_url(group_link)
    link = _normalize_group_link(link)
    if not link:
        return {"ok": False, "message": "Missing group link."}

    cookies = load_current_cdp_cookies(account_email)
    if cookies is None:
        session_dir = sessions_dir()
        session_file = _resolve_session_file(session_dir, account_email)
        with open(session_file, "rb") as f:
            cookies = pickle.load(f)

    options = Options()
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    apply_background_mode_option(options)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    driver = build_chrome_driver(options)
    _apply_browser_safety_patches(driver)
    wait = WebDriverWait(driver, 5)

    def _cleanup():
        try:
            driver.quit()
        except Exception:
            pass

    try:
        driver.get("https://www.facebook.com/")
        cancellable_sleep(random.uniform(0.6, 1.0))

        for cookie in cookies:
            if isinstance(cookie, dict) and "sameSite" not in cookie:
                cookie["sameSite"] = "Lax"
            try:
                driver.add_cookie(cookie)
            except Exception:
                continue

        driver.refresh()
        cancellable_sleep(random.uniform(0.6, 1.0))

        driver.get(link)
        cancellable_sleep(random.uniform(0.6, 0.9))

        page_info = _extract_group_page_info(driver, fallback_link=link)
        header_name = page_info.get("name") or ""
        page_link = page_info.get("link") or link

        if not header_name:
            return {"ok": False, "message": "Could not read group name. Try again."}

        has_buy_sell = _has_buy_sell_tab(driver)
        if has_buy_sell is not True:
            return {"ok": False, "message": "Not a Buy & Sell group. Skipped."}

        privacy = page_info.get("privacy")
        members_label = page_info.get("member_count_label")

        group_id = _group_id_from_link(page_link)
        group_obj = {
            "id": group_id,
            "name": header_name,
            "link": page_link,
            "member_count_label": members_label,
            "privacy": privacy,
        }

        data = load_groups(account_email)
        groups = data.get("groups") or []
        groups = [g for g in groups if isinstance(g, dict)]
        existing_map = {}
        for g in groups:
            gid = str((g or {}).get("id") or "").strip()
            if gid:
                existing_map[gid] = g

        existing_groups = list(existing_map.values())
        if str(group_id) in existing_map:
            return {"ok": True, "message": f"Already added: {existing_map[str(group_id)].get('name') or header_name}", "group": existing_map[str(group_id)]}

        existing_groups.append(group_obj)
        save_groups(account_email, existing_groups)

        return {"ok": True, "message": f"Added: {header_name}", "group": group_obj}
    finally:
        _cleanup()

@traced('GROUP_POST')
@cancellable
def post_anonymous_group_posts(
    account_email: str,
    group_links: list[str],
    description: str,
    image_paths: list[str] | None = None,
    delay_seconds: int = 0,
    progress_callback=None,
) -> dict:
    def _log(message: str):
        if callable(progress_callback):
            try:
                progress_callback(message)
                return
            except Exception:
                pass
        try:
            print(message, flush=True)
        except Exception:
            pass

    links = []
    seen = set()
    for raw in group_links or []:
        url = _ensure_facebook_url(raw)
        if not url:
            continue
        key = _normalize_group_link(url)
        if not key or key in seen:
            continue
        seen.add(key)
        links.append(url)

    if not account_email:
        return {"ok": False, "message": "No active account selected."}
    if not links:
        return {"ok": False, "message": "No group links provided."}
    if not str(description or "").strip():
        return {"ok": False, "message": "Description is required."}

    cookies = load_current_cdp_cookies(account_email)
    if cookies is None:
        session_dir = sessions_dir()
        session_file = _resolve_session_file(session_dir, account_email)
        with open(session_file, "rb") as f:
            cookies = pickle.load(f)

    options = Options()
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    apply_background_mode_option(options)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    driver = build_chrome_driver(options)
    _apply_browser_safety_patches(driver)
    wait = WebDriverWait(driver, 20)

    def _cleanup():
        try:
            driver.quit()
        except Exception:
            pass

    def _wait_clickable(xpath: str):
        return wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))

    def _wait_present(xpath: str):
        return wait.until(EC.presence_of_element_located((By.XPATH, xpath)))

    def _upload_images(dialog_root, upload_paths: list[str]):
        if not upload_paths:
            return
        file_input = _wait_present(
            "//div[@role='dialog' and .//span[normalize-space()='Create post']]//input[@type='file' and not(@disabled)]"
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
        file_input.send_keys("\n".join(upload_paths))
        cancellable_sleep(2)

    def _turn_off_anonymous_toggle():
        try:
            toggle = _wait_present(
                "//div[@role='dialog' and .//span[normalize-space()='Create post']]"
                "//input[@role='switch' and (contains(@aria-label, 'Anonymous') or contains(@aria-label, 'anonymous'))]"
            )
        except Exception:
            return

        try:
            aria_checked = str(toggle.get_attribute("aria-checked") or "").strip().lower()
            if aria_checked in {"true", "false"}:
                checked = aria_checked == "true"
            else:
                checked = None
        except Exception:
            checked = None

        try:
            if checked is None:
                checked = toggle.is_selected()
        except Exception:
            pass

        if checked is None:
            try:
                checked = bool(driver.execute_script("return arguments[0].checked === true;", toggle))
            except Exception:
                checked = False

        if checked:
            try:
                driver.execute_script("arguments[0].click();", toggle)
            except Exception:
                try:
                    toggle.click()
                except Exception:
                    return
            cancellable_sleep(0.6)

    results = []
    posted = 0

    try:
        driver.get("https://www.facebook.com/")
        cancellable_sleep(random.uniform(0.6, 1.0))

        for cookie in cookies:
            if isinstance(cookie, dict) and "sameSite" not in cookie:
                cookie["sameSite"] = "Lax"
            try:
                driver.add_cookie(cookie)
            except Exception:
                continue

        driver.refresh()
        cancellable_sleep(random.uniform(0.8, 1.2))

        upload_paths = []
        for p in image_paths or []:
            if not p:
                continue
            try:
                path_obj = Path(p)
                if path_obj.exists():
                    upload_paths.append(os.path.normpath(str(path_obj)))
            except Exception:
                continue

        for index, link in enumerate(links, start=1):
            _log(f"[GroupPosting] Opening group {index}/{len(links)}: {link}")
            try:
                driver.get(link)

                anonymous_btn = _wait_clickable(
                    "//span[normalize-space()='Anonymous post']/ancestor::*[@role='button'][1]"
                )
                anonymous_btn.click()

                create_btn = _wait_clickable(
                    "//span[normalize-space()='Create Anonymous Post']/ancestor::*[@role='button'][1]"
                )
                create_btn.click()

                dialog = _wait_present("//div[@role='dialog' and .//span[normalize-space()='Create post']]")

                _turn_off_anonymous_toggle()

                try:
                    textbox = _wait_clickable(
                        "//div[@role='dialog' and .//span[normalize-space()='Create post']]"
                        "//div[@role='textbox' and @contenteditable='true' and contains(@aria-placeholder, 'anonymous post')]"
                    )
                except Exception:
                    textbox = _wait_clickable(
                        "//div[@role='dialog' and .//span[normalize-space()='Create post']]"
                        "//div[@role='textbox' and @contenteditable='true']"
                    )
                textbox.click()
                textbox.send_keys(Keys.CONTROL, "a")
                textbox.send_keys(Keys.BACKSPACE)
                textbox.send_keys(description)

                if upload_paths:
                    _upload_images(dialog, upload_paths)

                submit_btn = _wait_clickable(
                    "//div[@role='dialog' and .//span[normalize-space()='Create post']]"
                    "//span[normalize-space()='Submit' or normalize-space()='Post']/ancestor::*[@role='button'][1]"
                )
                submit_btn.click()

                cancellable_sleep(10)
                posted += 1
                results.append({"link": link, "ok": True})
                diagnostic_event('GROUP_POST_ITEM', 'POSTED', account=account_email, group_link=link)
                _log(f"[GroupPosting] Posted to group: {link}")
            except Exception as exc:
                results.append({"link": link, "ok": False, "error": str(exc)})
                diagnostic_event('GROUP_POST_ITEM', 'FAILED', account=account_email,
                                 group_link=link, error_type=type(exc).__name__, error=str(exc))
                _log(f"[GroupPosting] Failed for {link}: {exc}")

            if index < len(links) and delay_seconds and delay_seconds > 0:
                cancellable_sleep(float(delay_seconds))

        return {
            "ok": posted > 0,
            "message": f"Posted to {posted} of {len(links)} groups.",
            "posted": posted,
            "total": len(links),
            "results": results,
        }
    finally:
        _cleanup()

def _ensure_facebook_url(link: str) -> str:
    url = _normalize_group_link(link)
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"https://www.facebook.com{url}"
    return f"https://www.facebook.com/{url}"


def _has_buy_sell_tab(driver) -> bool | None:
    def _probe():
        return driver.execute_script(
            r"""
            const normalizeText = value => (value || '')
              .trim()
              .toLowerCase()
              .replace(/&/g, ' and ')
              .replace(/\s+/g, ' ');
            const roleTabs = Array.from(document.querySelectorAll('a[role="tab"]'));
            const buySellTab = roleTabs.find(tab =>
              normalizeText(tab.innerText || tab.textContent) === 'buy and sell'
            );
            if (buySellTab) {
              return {
                result: true,
                reason: 'a[role=tab] text is buy and sell',
                link: buySellTab.href || '',
                tabText: normalizeText(buySellTab.innerText || buySellTab.textContent)
              };
            }
            const candidateSpans = Array.from(document.querySelectorAll('span'))
              .map(s => normalizeText(s.innerText || s.textContent))
              .filter(t => t && (t.includes('buy') || t.includes('sell')))
              .slice(0, 10);
            const tablist = document.querySelector('div[role="tablist"]');
            if (!tablist) {
              return {
                result: null,
                reason: 'no tablist found',
                candidates: candidateSpans
              };
            }
            const text = normalizeText(tablist.innerText || tablist.textContent);
            return {
              result: false,
              reason: 'no a[role=tab] with buy and sell text',
              tabText: text,
              candidates: candidateSpans
            };
            """
        )

    def _print_probe(probe, stage):
        try:
            current_url = driver.current_url
        except Exception:
            current_url = "<unavailable>"
        if not isinstance(probe, dict):
            print(f"[Groups][BuySell] stage={stage} url={current_url} invalid_probe={probe!r}")
            return
        print(
            f"[Groups][BuySell] stage={stage} url={current_url} "
            f"result={probe.get('result')!r} reason={probe.get('reason')!r}"
        )
        if probe.get("link"):
            print(f"[Groups][BuySell] matched_link={probe.get('link')!r}")
        if probe.get("tabText"):
            print(f"[Groups][BuySell] tab_text={probe.get('tabText')!r}")
        print(f"[Groups][BuySell] candidate_spans={probe.get('candidates') or []!r}")

    try:
        probe = _probe()
    except Exception as exc:
        print(f"[Groups][BuySell] initial probe error: {exc}")
        probe = {"result": None, "reason": "probe error"}
    _print_probe(probe, "initial")
    result = probe.get("result") if isinstance(probe, dict) else None
    if result is True:
        return True

    print("[Groups][BuySell] waiting up to 10 seconds for Buy and sell tab...")
    wait_short = WebDriverWait(driver, 10)
    try:
        wait_short.until(
            lambda d: d.execute_script(
                r"""
                const normalizeText = value => (value || '')
                  .trim()
                  .toLowerCase()
                  .replace(/&/g, ' and ')
                  .replace(/\s+/g, ' ');
                return Array.from(document.querySelectorAll('a[role="tab"]')).some(
                  tab => normalizeText(tab.innerText || tab.textContent) === 'buy and sell'
                );
                """
            )
        )
    except Exception as exc:
        print(f"[Groups][BuySell] no Buy and sell tab found within 10 seconds: {exc}")

    try:
        probe = _probe()
    except Exception as exc:
        print(f"[Groups][BuySell] final probe error: {exc}")
        probe = {"result": None, "reason": "probe error"}
    _print_probe(probe, "after_wait")
    result = probe.get("result") if isinstance(probe, dict) else None
    return bool(result)
