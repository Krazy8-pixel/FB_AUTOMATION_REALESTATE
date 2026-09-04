from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_from_directory
import copy
import hashlib
from automation.fb_login import MAX_ACCOUNTS
from automation.cdp_login import cleanup_expired_cdp_logins, cleanup_orphaned_cdp_accounts, close_cdp_browser, delete_cdp_account, get_cdp_account, list_cdp_accounts, start_cdp_login
from automation.remote_login import (
    cleanup_finished_remote_login_sessions,
    create_remote_login_session,
    get_remote_login_session,
)
from automation.auto_listing_excel_poster import read_excel_flexible, post_to_fb_marketplace
from automation.reposting import read_listing_from_manage
from automation.delete_listing_import import (
      ListingImportCancelled,
      delete_selected_listings_from_facebook,
      import_listings_from_facebook,
      refresh_listing_clicks_from_facebook,
      stop_listing_import_driver,
      stop_listing_delete_driver,
      load_listings,
    save_listings,
)
import os
from automation.delete_listings import delete_listing
import traceback
from automation.message_reply import AutoReplyManager, auto_reply_managers, get_new_messages
import re
import tempfile
import pandas as pd
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
import json
import threading
from urllib.parse import urlparse
from uuid import uuid4
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from automation.runtime_paths import activity_log_path, runtime_log_path, sessions_dir, screenshots_dir, data_dir, project_root
import time
from automation.email_settings import (
    get_smtp_receiver_email,
    set_smtp_receiver_email,
    clear_smtp_receiver_email,
)
from automation.auto_reply_state import (
    load_auto_reply_state,
    save_auto_reply_state,
    clear_auto_reply_state,
)
from automation.browser_settings import set_run_in_background_enabled
from automation.session_health import (
    cancel_session_check,
    ensure_default_healthy,
    get_session_check_snapshot,
    load_session_health,
    remove_session_health,
    set_session_health,
    start_session_check,
    HEALTHY,
)
from automation.groups import (
    delete_group_record,
    ImportCancelled,
    add_buy_sell_group_by_link,
    import_groups_from_facebook,
    post_anonymous_group_posts,
    load_groups,
    summarize_groups,
)
from automation.instagram_login import (
    instagram_account,
    login_instagram,
    delete_instagram_account,
    open_instagram_post_page,
)
from web_scraper1 import normalize_http_url, scrape_vehicle_listing
from web_scraper2 import scrape_autorama_listing, normalize_http_url as normalize_http_url2
from web_scraper3 import scrape_carsbyjatin_listing
from web_scraper4 import (
    normalize_http_url as normalize_http_url4, scrape_carsforsale_listing,
    collect_carsforsale_listings, mark_history_status as mark_carsforsale_history_status,
    setup_carsforsale_session, carsforsale_login_status, delete_carsforsale_session,
)
from web_scraper_california import collect_four_listings, delete_history, mark_history_status
from google import genai
 

gemini_client = genai.Client(
    api_key="AIzaSyA_OIDs9GFe9NgnzSwVzu0HUPsQOj7N8ws"
)
 
web_bp = Blueprint("web", __name__)

scheduler = BackgroundScheduler(daemon=True)
if not scheduler.running:
    scheduler.start()

scheduled_posts = {}
california_scan_jobs_lock = threading.Lock()
california_scan_jobs = {}
california_settings_lock = threading.RLock()
CALIFORNIA_SETTINGS_PATH = str(data_dir() / "california_autofinance_settings.json")
CALIFORNIA_DAILY_INTERVAL = timedelta(days=1)
CALIFORNIA_MAX_DAILY_POSTS = 24
CARSFORSALE_SETTINGS_PATH = str(data_dir() / "carsforsale_settings.json")
carsforsale_settings_lock = threading.RLock()
carsforsale_scan_jobs_lock = threading.Lock()
carsforsale_scan_jobs = {}
PORTAL_AUTH_PATH = str(data_dir() / "portal_auth.json")
portal_auth_lock = threading.RLock()
INSTAGRAM_SCHEDULER_ACCOUNT = "__instagram__"
groups_jobs_lock = threading.Lock()
groups_jobs = {}
groups_cancel_events = {}
listing_import_jobs_lock = threading.Lock()
listing_import_jobs = {}
listing_import_cancel_events = {}
LISTING_AUTO_REFRESH_INTERVAL = timedelta(hours=24)
LISTING_AUTO_REFRESH_CHECK_JOB_ID = "delete-listings-auto-refresh-check"
listing_click_jobs_lock = threading.Lock()
listing_click_jobs = {}
facebook_delete_jobs_lock = threading.Lock()
facebook_delete_jobs = {}
facebook_delete_cancel_events = {}

# =========================
# Paths & setup
# =========================
SESSION_DIR = sessions_dir()
LOG_PATH = activity_log_path()
RUNTIME_LOG_PATH = runtime_log_path()

# =========================
# Helpers
# =========================
def email_to_filename(email):
    return re.sub(r"[\\/:*?\"<>|]", "_", email) + ".pkl"

def filename_to_email(filename):
    return filename[:-4]

def get_all_accounts():
    return [
        filename_to_email(f)
        for f in os.listdir(SESSION_DIR)
        if f.endswith(".pkl")
    ]

def get_active_account():
    active = session.get("active_account")

    if active:
        return active

    accounts = get_all_accounts()
    if accounts:
        session["active_account"] = accounts[0]
        return accounts[0]

    return None


def _load_portal_auth():
    try:
        with open(PORTAL_AUTH_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            return payload
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return {}


def _portal_auth_is_configured():
    with portal_auth_lock:
        auth = _load_portal_auth()
    return bool(auth.get("username") and auth.get("password_hash"))


def _save_portal_auth(username, password):
    payload = {
        "username": str(username or "").strip(),
        "password_hash": generate_password_hash(str(password or "")),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    temporary = f"{PORTAL_AUTH_PATH}.tmp"
    with portal_auth_lock:
        os.makedirs(os.path.dirname(PORTAL_AUTH_PATH), exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, PORTAL_AUTH_PATH)


def _safe_portal_next_url(value):
    target = str(value or "").strip()
    parsed = urlparse(target)
    if target.startswith("/") and not target.startswith("//") and not parsed.netloc:
        return target
    return url_for("web.dashboard")


@web_bp.before_request
def require_portal_login():
    endpoint = request.endpoint or ""
    if endpoint in {"web.portal_login", "web.portal_setup", "static"}:
        return None
    if not _portal_auth_is_configured():
        return redirect(url_for("web.portal_setup"))
    if not session.get("portal_logged_in"):
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("web.portal_login", next=next_url))
    return None


@web_bp.route("/portal_setup", methods=["GET", "POST"])
def portal_setup():
    if _portal_auth_is_configured():
        return redirect(url_for("web.portal_login"))
    error = None
    if request.method == "POST":
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")
        confirm_password = str(request.form.get("confirm_password") or "")
        if len(username) < 3:
            error = "Username must contain at least 3 characters."
        elif len(password) < 8:
            error = "Password must contain at least 8 characters."
        elif password != confirm_password:
            error = "Passwords do not match."
        else:
            _save_portal_auth(username, password)
            session.clear()
            session["portal_logged_in"] = True
            session["portal_username"] = username
            return redirect(url_for("web.dashboard"))
    return render_template("portal_login.html", setup_mode=True, error=error)


@web_bp.route("/login", methods=["GET", "POST"])
def portal_login():
    if not _portal_auth_is_configured():
        return redirect(url_for("web.portal_setup"))
    if session.get("portal_logged_in"):
        return redirect(url_for("web.dashboard"))
    error = None
    if request.method == "POST":
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")
        with portal_auth_lock:
            auth = _load_portal_auth()
        if username == auth.get("username") and check_password_hash(auth.get("password_hash", ""), password):
            session.clear()
            session["portal_logged_in"] = True
            session["portal_username"] = username
            return redirect(_safe_portal_next_url(request.form.get("next")))
        error = "Incorrect username or password."
    return render_template(
        "portal_login.html",
        setup_mode=False,
        error=error,
        next_url=_safe_portal_next_url(request.args.get("next")),
    )


@web_bp.route("/logout", methods=["GET"])
def portal_logout():
    session.clear()
    return redirect(url_for("web.portal_login"))


def _load_california_settings():
    try:
        with open(CALIFORNIA_SETTINGS_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _save_california_settings(settings):
    os.makedirs(os.path.dirname(CALIFORNIA_SETTINGS_PATH), exist_ok=True)
    temporary = f"{CALIFORNIA_SETTINGS_PATH}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, CALIFORNIA_SETTINGS_PATH)


def _california_setting(account):
    if not account:
        return {"enabled": False, "post_count": 4, "delay_value": 1, "delay_unit": "hours", "next_scan_at": None}
    with california_settings_lock:
        item = dict(_load_california_settings().get(account) or {})
    try:
        post_count = int(item.get("post_count") or 4)
    except (TypeError, ValueError):
        post_count = 4
    item["post_count"] = max(1, min(CALIFORNIA_MAX_DAILY_POSTS, post_count))
    delay_unit = str(item.get("delay_unit") or "hours").strip().lower()
    if delay_unit not in {"minutes", "hours"}:
        delay_unit = "hours"
    try:
        delay_value = int(item.get("delay_value") or 1)
    except (TypeError, ValueError):
        delay_value = 1
    item["delay_unit"] = delay_unit
    item["delay_value"] = max(1, min(59 if delay_unit == "minutes" else 24, delay_value))
    item["enabled"] = bool(item.get("enabled"))
    return item


def _california_daily_job_id(account):
    digest = hashlib.sha256(str(account or "").encode("utf-8")).hexdigest()[:16]
    return f"california_daily_{digest}"


def _california_public_status(account):
    setting = _california_setting(account)
    with california_scan_jobs_lock:
        scan_running = bool((california_scan_jobs.get(account) or {}).get("running"))
    next_run = None
    try:
        recurring_job = scheduler.get_job(_california_daily_job_id(account)) if account else None
        next_run = recurring_job.next_run_time if recurring_job else None
    except Exception:
        next_run = None
    return {
        "enabled": bool(setting.get("enabled")),
        "post_count": setting.get("post_count", 4),
        "delay_value": setting.get("delay_value", 1),
        "delay_unit": setting.get("delay_unit", "hours"),
        "scan_running": scan_running,
        "next_scan_at": next_run,
        "post_interval_minutes": setting.get("delay_value", 1) * (60 if setting.get("delay_unit") == "hours" else 1),
    }


def _load_carsforsale_settings():
    try:
        with open(CARSFORSALE_SETTINGS_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_carsforsale_settings(settings):
    temporary = f"{CARSFORSALE_SETTINGS_PATH}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, CARSFORSALE_SETTINGS_PATH)


def _carsforsale_setting(account):
    with carsforsale_settings_lock:
        item = dict(_load_carsforsale_settings().get(account) or {}) if account else {}
    try: count = int(item.get("post_count") or 4)
    except Exception: count = 4
    unit = str(item.get("delay_unit") or "hours").lower()
    if unit not in {"minutes", "hours"}: unit = "hours"
    try: value = int(item.get("delay_value") or 1)
    except Exception: value = 1
    item.update({"enabled": bool(item.get("enabled")), "post_count": max(1, min(24, count)),
                 "delay_unit": unit, "delay_value": max(1, min(59 if unit == "minutes" else 24, value))})
    return item


def _carsforsale_daily_job_id(account):
    return "carsforsale_daily_" + hashlib.sha256(str(account or "").encode()).hexdigest()[:16]


def _carsforsale_public_status(account):
    setting = _carsforsale_setting(account)
    with carsforsale_scan_jobs_lock:
        scanning = bool((carsforsale_scan_jobs.get(account) or {}).get("running"))
    job = scheduler.get_job(_carsforsale_daily_job_id(account)) if account else None
    return {**setting, "scan_running": scanning, "next_scan_at": job.next_run_time if job else None,
            "post_interval_minutes": setting.get("delay_value", 1) * (60 if setting.get("delay_unit") == "hours" else 1)}


def _account_page_context(accounts=None):
    accounts = accounts if accounts is not None else get_all_accounts()
    health = ensure_default_healthy(accounts)
    return {
        "accounts": accounts,
        "active_account": get_active_account(),
        "active_page": "account",
        "max_accounts": MAX_ACCOUNTS,
        "account_count": len(accounts),
        "account_slots_remaining": max(0, MAX_ACCOUNTS - len(accounts)),
        "account_health": health,
        "session_check": get_session_check_snapshot(),
        "cdp_accounts": list_cdp_accounts(),
        "instagram_account": instagram_account(),
    }

 

def _is_probable_phone_request(req):
    agent = (req.user_agent.string or "").lower()
    mobile_tokens = ("iphone", "android", "mobile", "ipad")
    return any(token in agent for token in mobile_tokens)

def _load_activity_log():
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def _save_activity_log(log_items):
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log_items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _runtime_log(message):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(RUNTIME_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


@web_bp.route("/screenshots/<path:filename>", methods=["GET"])
def screenshot_file(filename):
    folder = screenshots_dir()
    return send_from_directory(folder, filename, as_attachment=False)


def _normalize_header(value):
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_urls_from_excel(excel_path):
    df = pd.read_excel(excel_path)
    if df is None or df.empty:
        return []

    aliases = [
        "url",
        "urls",
        "post url",
        "post urls",
        "post link",
        "post links",
        "post_link",
        "post_links",
        "post_url",
        "post_urls",
        "listing url",
        "listing urls",
        "liting url",
        "liting urls",
    ]
    normalized_aliases = {_normalize_header(a) for a in aliases}

    target_column = None
    columns = list(df.columns)

    for col in columns:
        if _normalize_header(col) in normalized_aliases:
            target_column = col
            break

    if target_column is None:
        for col in columns:
            normalized = _normalize_header(col)
            if "url" in normalized or "link" in normalized:
                target_column = col
                break

    if target_column is None and columns:
        target_column = columns[0]

    if target_column is None:
        return []

    urls = []
    for value in df[target_column].tolist():
        if pd.isna(value):
            continue
        text = str(value).strip()
        if text:
            urls.append(text)
    return urls


def _parse_manual_urls(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return []
    parts = re.split(r"[\r\n,]+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _is_valid_http_url(value):
    try:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _split_valid_invalid_urls(urls):
    valid = []
    invalid = []
    for item in urls:
        text = str(item or "").strip()
        if not text:
            continue
        if _is_valid_http_url(text):
            valid.append(text)
        else:
            invalid.append(text)
    return valid, invalid


def _build_repost_payloads(read_result):
    """
    Convert Manage Listings edit-page output into Marketplace post payload(s).
    Reuses image URL handling inside poster by mapping urls -> image_path1..N.
    """
    payloads = []
    if not isinstance(read_result, dict):
        return payloads

    posts = read_result.get("posts") or []
    if not isinstance(posts, list):
        return payloads

    for post in posts:
        if not isinstance(post, dict):
            continue

        title = str(post.get("title", "")).strip()
        if not title:
            title = " ".join(
                str(post.get(field, "")).strip()
                for field in ("year", "make", "model")
                if str(post.get(field, "")).strip()
            ).strip()

        payload = {
            "title": title,
            "price": str(post.get("price", "")).strip(),
            "brand": str(post.get("brand") or post.get("make", "")).strip(),
            "description": str(post.get("description", "")).strip(),
        }

        # Preserve the complete extracted vehicle record for the existing
        # Marketplace poster; it requires make/model/mileage/body style in
        # addition to the repost title, price, description, and images.
        for field in (
            "vehicle_type", "year", "make", "model", "mileage", "body_style",
            "exterior_color", "interior_color", "vehicle_condition", "fuel_type",
            "transmission", "location",
        ):
            payload[field] = str(post.get(field, "") or "").strip()

        payload["listing_name_for_delete"] = payload.get("title", "")

        image_urls = post.get("image_urls") or []
        if isinstance(image_urls, list):
            for idx, image_url in enumerate(image_urls[:10], start=1):
                payload[f"image_path{idx}"] = str(image_url or "").strip()

        payloads.append(payload)

    return payloads


def _register_repost_job(job_id, account, listing, run_at, delay_value, delay_unit):
    listing = dict(listing or {})
    with repost_jobs_lock:
        repost_jobs[job_id] = {
            "job_id": job_id,
            "account": account,
            "listing_id": str(listing.get("id") or ""),
            "listing": listing,
            "title": " ".join(
                str(listing.get(field) or "").strip()
                for field in ("year", "make", "model")
                if str(listing.get(field) or "").strip()
            ).strip(),
            "run_at": run_at,
            "status": "pending",
            "delay_value": delay_value,
            "delay_unit": delay_unit,
            "delay_seconds": int(delay_value) * ({"minutes": 60, "hours": 3600, "days": 86400}.get(delay_unit, 60)),
            "created_at": datetime.now(),
            "started_at": None,
            "finished_at": None,
        }

def _collect_repost_schedule(active_account):
    if not active_account:
        return {"items": [], "pending_count": 0, "running_count": 0}

    now = datetime.now()
    items = []
    pending_count = 0
    running_count = 0

    with repost_jobs_lock:
        for _, meta in repost_jobs.items():
            if meta.get("account") != active_account:
                continue

            status = meta.get("status", "pending")
            if status not in {"pending", "reading", "posting", "deleting"}:
                continue
            run_at = meta.get("run_at")
            if not isinstance(run_at, datetime):
                continue

            remaining = max(0, int((run_at - now).total_seconds()))
            if status == "pending":
                pending_count += 1
            if status in {"reading", "deleting", "posting"}:
                running_count += 1

            items.append({
                "job_id": meta.get("job_id"),
                "listing_id": meta.get("listing_id", ""),
                "title": meta.get("title", "") or "Listing",
                "run_at": run_at,
                "status": status,
                "remaining_seconds": remaining,
                "delay_value": meta.get("delay_value", ""),
                "delay_unit": meta.get("delay_unit", ""),
            })

    items.sort(key=lambda x: x.get("run_at"))
    return {"items": items, "pending_count": pending_count, "running_count": running_count}


def _is_reposting_running_for_account(account):
    if not account:
        return False
    with repost_jobs_lock:
        for meta in repost_jobs.values():
            if meta.get("account") != account:
                continue
            if meta.get("status") in {"pending", "reading", "deleting", "posting"}:
                return True
    return False


def _active_repost_listing_ids(account):
    active = set()
    if not account:
        return active
    with repost_jobs_lock:
        for meta in repost_jobs.values():
            if meta.get("account") != account:
                continue
            if meta.get("status") in {"pending", "reading", "posting", "deleting"}:
                listing_id = str(meta.get("listing_id") or "").strip()
                if listing_id:
                    active.add(listing_id)
    return active


def _repost_history_path():
    return data_dir() / "reposting_history.json"


def _load_repost_history():
    path = _repost_history_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception as exc:
        _runtime_log(f"[Reposting] Could not load history: {exc}")
    return []


def _save_repost_history(items):
    path = _repost_history_path()
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def _record_repost_history(account, listing, status, error=""):
    listing = listing if isinstance(listing, dict) else {}
    title = " ".join(
        str(listing.get(field) or "").strip()
        for field in ("year", "make", "model")
        if str(listing.get(field) or "").strip()
    ).strip() or str(listing.get("title") or "Listing").strip()
    item = {
        "id": uuid4().hex,
        "account": str(account or ""),
        "listing_id": str(listing.get("id") or ""),
        "title": title,
        "status": "reposted" if status == "reposted" else "failed",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": str(error or "").strip(),
    }
    with repost_history_lock:
        items = _load_repost_history()
        items.insert(0, item)
        _save_repost_history(items[:1000])


def _collect_repost_history(account):
    if not account:
        return []
    with repost_history_lock:
        return [
            item for item in _load_repost_history()
            if item.get("account") == account
        ]


def _run_reposting_read_job(listing, account, job_id=None):
    run_lock = _get_repost_run_lock(account)
    with run_lock:
        account_post_lock = _get_post_lock(account)
        if job_id:
            with repost_jobs_lock:
                if job_id in repost_jobs:
                    repost_jobs[job_id]["status"] = "reading"
                    repost_jobs[job_id]["started_at"] = datetime.now()

        try:
            with account_post_lock:
                read_result = read_listing_from_manage(account, listing)
                payloads = _build_repost_payloads(read_result)
                if not payloads:
                    raise Exception("No readable post data found from edit page")

                post_results = []
                for payload in payloads:
                    title = str(payload.get("title") or "").strip()
                    price = str(payload.get("price") or "").strip()
                    has_image = any(
                        str(payload.get(f"image_path{index}") or "").strip()
                        for index in range(1, 11)
                    )
                    if not title or not price or not has_image:
                        raise Exception(
                            "Read listing data is incomplete; source was not deleted "
                            "(title, price, and at least one image are required)"
                        )

                    if job_id:
                        with repost_jobs_lock:
                            if job_id in repost_jobs:
                                repost_jobs[job_id]["status"] = "posting"
                    result = post_to_fb_marketplace(payload, account)
                    post_results.append(bool(result))
                    if not result:
                        raise Exception("Posting did not return a success signal; source listing was kept")

                    # Delete only after the posting function confirms success.
                    if job_id:
                        with repost_jobs_lock:
                            if job_id in repost_jobs:
                                repost_jobs[job_id]["status"] = "deleting"
                    delete_name = str(payload.get("listing_name_for_delete") or "").strip()
                    _runtime_log(
                        f"[Reposting] Using Delete Listings flow for original source: title='{delete_name}'"
                    )
                    repost_delete_listing = dict(listing)
                    repost_delete_listing["_match_occurrence"] = 2
                    delete_result = delete_selected_listings_from_facebook(
                        account,
                        [repost_delete_listing],
                    )
                    deleted_count = int((delete_result or {}).get("deleted") or 0)
                    second_match_not_found = int((delete_result or {}).get("not_found") or 0) >= 1
                    if deleted_count != 1 and not second_match_not_found:
                        raise Exception(
                            "Repost succeeded, but Delete Listings flow could not delete "
                            f"the original source (result={delete_result})"
                        )
                    listing_id = str((listing or {}).get("id") or "") if isinstance(listing, dict) else ""
                    if deleted_count == 1 and listing_id:
                        stored = load_listings(account)
                        remaining = [
                            item for item in (stored.get("listings") or [])
                            if str(item.get("id") or "") != listing_id
                        ]
                        save_listings(account, remaining)
                    if deleted_count == 1:
                        _runtime_log(
                            f"[Reposting] Repost succeeded and second/old source was deleted: title='{delete_name}'"
                        )
                    else:
                        _runtime_log(
                            f"[Reposting] Repost succeeded; only one matching listing was found, "
                            f"so nothing was deleted: title='{delete_name}'"
                        )

                if not all(post_results):
                    raise Exception("Posting failed for one or more repost payloads")

            if job_id:
                with repost_jobs_lock:
                    if job_id in repost_jobs:
                        repost_jobs[job_id]["status"] = "done"
                        repost_jobs[job_id]["finished_at"] = datetime.now()
            _runtime_log(
                f"[Reposting] Completed job={job_id or '-'} account={account} listing={listing.get('id') if isinstance(listing, dict) else '-'}"
            )
            _record_repost_history(account, listing, "reposted")
        except Exception as e:
            if job_id:
                with repost_jobs_lock:
                    if job_id in repost_jobs:
                        repost_jobs[job_id]["status"] = "failed"
                        repost_jobs[job_id]["finished_at"] = datetime.now()
            _runtime_log(
                f"[Reposting] Failed job={job_id or '-'} account={account} listing={listing.get('id') if isinstance(listing, dict) else '-'} | {e}"
            )
            _record_repost_history(account, listing, "failed", error=e)


activity_log = _load_activity_log()
activity_log_lock = threading.Lock()
delete_jobs_lock = threading.Lock()
delete_jobs_by_account = {}
post_jobs_lock = threading.Lock()
post_locks_by_account = {}
scheduled_posts_lock = threading.Lock()
repost_jobs_lock = threading.Lock()
repost_jobs = {}
repost_history_lock = threading.Lock()
repost_run_locks_lock = threading.Lock()
repost_run_locks_by_account = {}


def _get_post_lock(account):
    with post_jobs_lock:
        lock = post_locks_by_account.get(account)
        if lock is None:
            lock = threading.Lock()
            post_locks_by_account[account] = lock
        return lock


def _get_repost_run_lock(account):
    with repost_run_locks_lock:
        lock = repost_run_locks_by_account.get(account)
        if lock is None:
            lock = threading.Lock()
            repost_run_locks_by_account[account] = lock
        return lock

def _log_event(event_type, account, status, reference, details=None, tb_text=None):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": event_type,
        "status": status,
        "account": account or "-",
        "reference": reference or "-",
        "details": details or "",
        "traceback": tb_text or ""
    }
    with activity_log_lock:
        activity_log.insert(0, entry)
        _save_activity_log(activity_log)


def _auto_reply_crash_logger(account, details):
    _log_event(
        event_type="auto_reply",
        account=account,
        status="failed",
        reference="Failed",
        details=details or "Auto-reply worker crashed",
    )


def _restore_auto_reply_if_enabled():
    state = load_auto_reply_state()
    if not state or not state.get("enabled"):
        return

    account = str(state.get("account") or "").strip()
    if not account:
        return

    session_file = os.path.join(SESSION_DIR, email_to_filename(account))
    if not os.path.exists(session_file):
        return

    if account not in auto_reply_managers:
        auto_reply_managers[account] = AutoReplyManager(account)

    manager = auto_reply_managers[account]
    manager.set_crash_callback(_auto_reply_crash_logger)

    if manager.active:
        return

    try:
        manager.start(
            reply_message=str(state.get("reply_message") or ""),
            delay=int(state.get("delay") or 5),
            pin=str(state.get("pin") or ""),
            get_messages_func=get_new_messages,
        )
        _runtime_log(f"[AutoReply] Restored auto-reply for account={account}")
    except Exception as e:
        _runtime_log(f"[AutoReply] Failed to restore auto-reply for account={account}: {e}")


def _is_delete_running_for_account(account):
    with delete_jobs_lock:
        job = delete_jobs_by_account.get(account)
        return bool(job and job.get("running"))


def _mark_delete_job_done(account):
    with delete_jobs_lock:
        delete_jobs_by_account.pop(account, None)


def _get_delete_job_snapshot(account):
    with delete_jobs_lock:
        job = delete_jobs_by_account.get(account) or {}
        if not job.get("running"):
            return False, None
        return True, job.get("started_at")


def _run_delete_job(account, limit, status_filter):
    try:
        # Reuse per-account automation lock to avoid parallel browser actions
        # for the same account across posting/reposting/delete flows.
        with _get_post_lock(account):
            success = delete_listing(
                account=account,
                limit=limit,
                status=status_filter
            )
        if success:
            _log_event(
                event_type="delete_listings",
                account=account,
                status="success",
                reference=f"Status {status_filter}",
                details=f"Limit {limit}"
            )
        else:
            _log_event(
                event_type="delete_listings",
                account=account,
                status="failed",
                reference=f"Status {status_filter}",
                details=f"Limit {limit}"
            )
    except Exception as e:
        tb_text = traceback.format_exc()
        _log_event(
            event_type="delete_listings",
            account=account,
            status="failed",
            reference=f"Status {status_filter}",
            details=f"Limit {limit}; Error {e}",
            tb_text=tb_text
        )
    finally:
        _mark_delete_job_done(account)


def _post_single_listing(post_data, account, job_id=None):
    account_post_lock = _get_post_lock(account)
    try:
        with account_post_lock:
            _runtime_log(f"[AutoListing] Start job={job_id or '-'} account={account}")
            if job_id:
                with scheduled_posts_lock:
                    if job_id in scheduled_posts:
                        scheduled_posts[job_id]["status"] = "posting"
                        scheduled_posts[job_id]["started_at"] = datetime.now()
            post_mode = str((post_data or {}).get("post_action") or "simple").strip().lower()
            group_names = (post_data or {}).get("group_names") or []
            if isinstance(group_names, str):
                group_names = [group_names]
            if post_mode in {"instagram", "instagram_post"}:
                _runtime_log(f"[Instagram] Dispatching Instagram job={job_id or '-'} to the Instagram composer")
                open_instagram_post_page(post_data)
                result = True
            else:
                result = post_to_fb_marketplace(post_data, account, post_mode=post_mode, group_names=group_names)
        if job_id:
            with scheduled_posts_lock:
                if job_id in scheduled_posts:
                    scheduled_posts[job_id]["status"] = "posted" if result else "failed"
                    scheduled_posts[job_id]["posted_at"] = datetime.now()
        status = "success" if result else "failed"
        title = (post_data or {}).get("title") or "Untitled"
        _log_event(
            event_type=("instagram_post" if post_mode in {"instagram", "instagram_post"} else "post_listing"),
            account=account,
            status=status,
            reference=title,
            details=f"Job {job_id}" if job_id else ""
        )
        _runtime_log(f"[AutoListing] End job={job_id or '-'} status={status}")
        return bool(result)
    except Exception:
        tb_text = traceback.format_exc()
        traceback.print_exc()
        _runtime_log(f"[AutoListing] Exception job={job_id or '-'} account={account}: {tb_text}")
        if job_id:
            with scheduled_posts_lock:
                if job_id in scheduled_posts:
                    scheduled_posts[job_id]["status"] = "failed"
                    scheduled_posts[job_id]["posted_at"] = datetime.now()
        title = (post_data or {}).get("title") or "Untitled"
        _log_event(
            event_type="post_listing",
            account=account,
            status="failed",
            reference=title,
            details=f"Job {job_id}" if job_id else "",
            tb_text=tb_text
        )
        return False


def _open_instagram_scheduled_post(job_id):
    """Open the Instagram composer when an Instagram scheduler job is due."""
    with scheduled_posts_lock:
        job_meta = dict(scheduled_posts.get(job_id, {}))
        if job_id in scheduled_posts:
            scheduled_posts[job_id]["status"] = "posting"
            scheduled_posts[job_id]["started_at"] = datetime.now()
    try:
        open_instagram_post_page(job_meta.get("post_data") or {})
        with scheduled_posts_lock:
            if job_id in scheduled_posts:
                scheduled_posts[job_id]["status"] = "opened"
                scheduled_posts[job_id]["posted_at"] = datetime.now()
        _runtime_log(f"[Instagram] Opened scheduled post composer job={job_id}")
    except Exception:
        tb_text = traceback.format_exc()
        with scheduled_posts_lock:
            if job_id in scheduled_posts:
                scheduled_posts[job_id]["status"] = "failed"
                scheduled_posts[job_id]["posted_at"] = datetime.now()
        _runtime_log(f"[Instagram] Scheduled post failed job={job_id}: {tb_text}")
        _log_event(
            event_type="instagram_post",
            account=INSTAGRAM_SCHEDULER_ACCOUNT,
            status="failed",
            reference="Instagram post",
            details=f"Job {job_id}",
            tb_text=tb_text,
        )


def _register_scheduled_post(job_id, account, post_data, run_at, platform="facebook"):
    post_payload = post_data if isinstance(post_data, dict) else {}
    title = post_payload.get("title") or "Untitled"
    make = post_payload.get("make") or ""
    with scheduled_posts_lock:
        scheduled_posts[job_id] = {
            "job_id": job_id,
            "account": account,
            "platform": platform,
            "post_data": (dict(post_payload) if platform == "instagram" else None),
            "title": title,
            "make": make,
            "run_at": run_at,
            "status": "pending",
            "created_at": datetime.now(),
            "started_at": None,
            "posted_at": None
        }


def _collect_account_schedule(active_account):
    now = datetime.now()
    items = []
    pending_count = 0

    for job in scheduler.get_jobs():
        with scheduled_posts_lock:
            known_meta = dict(scheduled_posts.get(job.id, {}))
        is_instagram = (
            known_meta.get("platform") == "instagram"
            or str(job.id).startswith("instagram_")
        )
        job_account = None
        post_data = None
        if is_instagram:
            job_account = INSTAGRAM_SCHEDULER_ACCOUNT
            post_data = {}
        elif job.args and len(job.args) >= 2:
            post_data = job.args[0]
            job_account = job.args[1]

        # This scheduler view is only for normal post jobs (dict payload),
        # not repost-read jobs where first arg is a URL string.
        if not is_instagram and not isinstance(post_data, dict):
            continue
        if not is_instagram and str(post_data.get("post_action") or "").strip().lower() in {"instagram", "instagram_post"}:
            continue

        if not is_instagram and job_account != active_account:
            continue

        with scheduled_posts_lock:
            known_job = job.id in scheduled_posts
        if not known_job:
            _register_scheduled_post(
                job.id,
                job_account,
                post_data,
                job.next_run_time or now,
                platform=("instagram" if is_instagram else "facebook"),
            )

        with scheduled_posts_lock:
            job_meta = dict(scheduled_posts.get(job.id, {}))
        next_run = job.next_run_time
        if not next_run:
            continue

        if next_run.tzinfo is not None and now.tzinfo is None:
            now = datetime.now(next_run.tzinfo)
        elif next_run.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        remaining_seconds = max(0, int((next_run - now).total_seconds()))
        status = "pending" if remaining_seconds > 0 else "posting"

        items.append({
            "job_id": job.id,
            "title": job_meta.get("title", "Untitled"),
            "make": "Instagram" if is_instagram else job_meta.get("make", ""),
            "platform": "instagram" if is_instagram else "facebook",
            "source": ("California Auto Finance" if str(job.id).startswith("california_") else
                       "CarsForSale" if str(job.id).startswith("carsforsale_") else "Manual / Excel"),
            "status": status,
            "run_at": next_run,
            "remaining_seconds": remaining_seconds
        })

        if status == "pending":
            pending_count += 1

    # Include jobs already handed off by APScheduler. California jobs can wait
    # here for their account's one-at-a-time posting lock.
    visible_job_ids = {item["job_id"] for item in items}
    with scheduled_posts_lock:
        scheduled_snapshot = list(scheduled_posts.items())

    for job_id, meta in scheduled_snapshot:
        is_instagram = (
            meta.get("platform") == "instagram"
            or str(job_id).startswith("instagram_")
        )
        if not is_instagram and meta.get("account") != active_account:
            continue
        meta_status = str(meta.get("status") or "").strip().lower()
        if meta_status not in {"waiting", "posting"} or job_id in visible_job_ids:
            continue
        run_at = meta.get("started_at") or meta.get("run_at") or now
        items.append({
            "job_id": job_id,
            "title": meta.get("title", "Untitled"),
            "make": "Instagram" if is_instagram else meta.get("make", ""),
            "platform": "instagram" if is_instagram else "facebook",
            "source": ("California Auto Finance" if str(job_id).startswith("california_") else
                       "CarsForSale" if str(job_id).startswith("carsforsale_") else "Manual / Excel"),
            "status": meta_status,
            "run_at": run_at,
            "remaining_seconds": 0
        })
        if meta_status == "waiting":
            pending_count += 1

    with scheduled_posts_lock:
        posted_count = sum(
            1 for p in scheduled_posts.values()
            if p.get("platform") != "instagram"
            and p.get("account") == active_account
            and p.get("status") == "posted"
        )

    local_tz = datetime.now().astimezone().tzinfo

    def _run_at_sort_key(item):
        run_at = item.get("run_at")
        if not isinstance(run_at, datetime):
            return float("inf")
        if run_at.tzinfo is None:
            return run_at.replace(tzinfo=local_tz).astimezone(timezone.utc).timestamp()
        return run_at.astimezone(timezone.utc).timestamp()

    items.sort(key=_run_at_sort_key)
    return {
        "items": items,
        "pending_count": pending_count,
        "posted_count": posted_count
    }


def _is_auto_listing_running_for_account(account):
    if not account:
        return False

    with california_scan_jobs_lock:
        if (california_scan_jobs.get(account) or {}).get("running"):
            return True

    # Fast path from in-memory job registry.
    with scheduled_posts_lock:
        for meta in scheduled_posts.values():
            if meta.get("account") != account:
                continue
            if str(meta.get("status") or "").strip().lower() in {"pending", "waiting", "posting"}:
                return True

    schedule_data = _collect_account_schedule(account)
    for item in schedule_data.get("items", []):
        if item.get("platform") == "instagram":
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in {"pending", "posting"}:
            return True
    return False


def _get_latest_scheduled_run_at(account):
    if not account:
        return None

    latest = None
    with scheduled_posts_lock:
        for meta in scheduled_posts.values():
            if meta.get("account") != account:
                continue
            status = str(meta.get("status") or "").strip().lower()
            if status not in {"pending", "posting"}:
                continue
            run_at = meta.get("run_at")
            if isinstance(run_at, datetime) and (latest is None or run_at > latest):
                latest = run_at

    for job in scheduler.get_jobs():
        if not job.args or len(job.args) < 2:
            continue
        post_data = job.args[0]
        job_account = job.args[1]
        if not isinstance(post_data, dict):
            continue
        if job_account != account:
            continue
        run_at = job.next_run_time
        if isinstance(run_at, datetime):
            if run_at.tzinfo is not None:
                run_at = run_at.astimezone().replace(tzinfo=None)
            if latest is None or run_at > latest:
                latest = run_at

    return latest


def _manual_images_directory():
    return str(data_dir("manual_listing_images"))

def _group_post_images_directory():
    return str(data_dir("group_post_images"))


def _build_manual_post_data(form_data):
    def _clean(field_name):
        return str(form_data.get(field_name) or "").strip()

    # This payload is exclusively for the manual *vehicle* form.  Facebook's
    # vehicle listing flow has no title input; keep a derived title only for
    # our scheduler/activity-log display.
    vehicle_fields = (
        "vehicle_type",
        "year",
        "make",
        "model",
        "mileage",
        "price",
        "body_style",
        "exterior_color",
        "interior_color",
        "vehicle_condition",
        "fuel_type",
        "transmission",
        "description",
        "location",
    )
    post_data = {field: _clean(field) for field in vehicle_fields}
    post_data["description_optional"] = True
    display_title = " ".join(
        value for value in (post_data["year"], post_data["make"], post_data["model"]) if value
    )
    post_data["title"] = display_title or "Manual Vehicle Listing"
    return post_data
def _save_manual_images(files):
    saved_paths = []
    target_dir = _manual_images_directory()
    os.makedirs(target_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for index, file in enumerate(files[:10], start=1):
        if not file:
            continue
        original_name = str(file.filename or "").strip()
        if not original_name:
            continue
        safe_name = secure_filename(original_name)
        if not safe_name:
            safe_name = f"image_{index}.jpg"
        _, ext = os.path.splitext(safe_name)
        ext = ext.lower() or ".jpg"
        file_name = f"manual_{timestamp}_{uuid4().hex[:8]}_{index}{ext}"
        full_path = os.path.join(target_dir, file_name)
        file.save(full_path)
        saved_paths.append(full_path)

    return saved_paths


def _get_prefilled_manual_image_paths(form_data):
    paths = []
    folder = screenshots_dir()
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".jfif"}
    for raw_name in form_data.getlist("prefilled_images"):
        name = str(raw_name or "").strip()
        safe_name = os.path.basename(name)
        if not name or safe_name != name:
            continue
        if os.path.splitext(safe_name)[1].lower() not in allowed_exts:
            continue
        candidate = os.path.join(folder, safe_name)
        if os.path.isfile(candidate):
            paths.append(candidate)
    return paths


def _save_group_post_images(files):
    saved_paths = []
    target_dir = _group_post_images_directory()
    os.makedirs(target_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for index, file in enumerate(files[:10], start=1):
        if not file:
            continue
        original_name = str(file.filename or "").strip()
        if not original_name:
            continue
        safe_name = secure_filename(original_name)
        if not safe_name:
            safe_name = f"image_{index}.jpg"
        _, ext = os.path.splitext(safe_name)
        ext = ext.lower() or ".jpg"
        file_name = f"group_post_{timestamp}_{uuid4().hex[:8]}_{index}{ext}"
        full_path = os.path.join(target_dir, file_name)
        file.save(full_path)
        saved_paths.append(full_path)

    return saved_paths

# =========================
# Routes
# =========================




@web_bp.route("/")
def dashboard():
    accounts = get_all_accounts()
    active_account = get_active_account()

    return render_template(
        "dashboard.html",
        accounts=accounts,
        active_account=active_account,
        active_page="dashboard"
    )


def _render_groups_page(message=None, message_type=None):
    accounts = get_all_accounts()
    active_account = get_active_account()

    groups_payload = load_groups(active_account) if active_account else {"groups": []}
    groups = groups_payload.get("groups") or []
    groups = [g for g in groups if isinstance(g, dict)]

    if message is None:
        message = session.pop("groups_message", None)
        message_type = session.pop("groups_message_type", None)

    summaries = {}
    for acc in accounts:
        summaries[acc] = summarize_groups(acc)

    with groups_jobs_lock:
        import_status = dict(groups_jobs.get(active_account) or {}) if active_account else {"running": False}

    return render_template(
        "groups.html",
        message=message,
        message_type=message_type,
        accounts=accounts,
        active_account=active_account,
        active_page="groups",
        groups=groups,
        account_summaries=summaries,
        import_status=import_status,
    )


@web_bp.route("/groups", methods=["GET"])
def groups_page():
    return _render_groups_page()


def _start_groups_import_job(account_email: str) -> tuple[bool, str]:
    if not account_email:
        return False, "No active account selected."

    with groups_jobs_lock:
        current = groups_jobs.get(account_email) or {}
        if current.get("running"):
            return False, "Import is already running for this account."
        cancel_event = threading.Event()
        groups_cancel_events[account_email] = cancel_event
        groups_jobs[account_email] = {
            "running": True,
            "started_at": time.time(),
            "finished_at": None,
            "imported_new_count": 0,
            "imported_total_count": 0,
            "stop_requested": False,
            "message": "Starting import...",
            "error": None,
        }

    def _runner():
        imported_new = 0
        imported_total = 0

        def _progress(event: dict):
            nonlocal imported_new, imported_total
            if not isinstance(event, dict):
                return
            if event.get("event") == "group":
                if event.get("new_count") is not None:
                    try:
                        imported_new = int(event.get("new_count") or 0)
                    except Exception:
                        pass
                if event.get("total_count") is not None:
                    try:
                        imported_total = int(event.get("total_count") or 0)
                    except Exception:
                        pass
                msg = event.get("message") or "Imported a group..."
            else:
                msg = event.get("message") or ""
            with groups_jobs_lock:
                job = groups_jobs.get(account_email) or {}
                job["imported_new_count"] = imported_new
                job["imported_total_count"] = imported_total
                if msg:
                    job["message"] = msg
                groups_jobs[account_email] = job

        try:
            with groups_jobs_lock:
                cancel_event = groups_cancel_events.get(account_email)
            import_groups_from_facebook(account_email, progress_callback=_progress, stop_event=cancel_event)
            with groups_jobs_lock:
                job = groups_jobs.get(account_email) or {}
                job["running"] = False
                job["finished_at"] = time.time()
                total_now = len((load_groups(account_email).get("groups") or []))
                job["imported_total_count"] = max(int(job.get("imported_total_count") or 0), total_now)
                job["message"] = f"Import complete. {job['imported_total_count']} groups scraped."
                groups_jobs[account_email] = job
        except ImportCancelled:
            with groups_jobs_lock:
                job = groups_jobs.get(account_email) or {}
                job["running"] = False
                job["finished_at"] = time.time()
                total_now = len((load_groups(account_email).get("groups") or []))
                job["imported_total_count"] = max(int(job.get("imported_total_count") or 0), total_now)
                job["message"] = f"Importing cancelled. {job['imported_total_count']} groups scraped."
                groups_jobs[account_email] = job
        except Exception as exc:
            with groups_jobs_lock:
                job = groups_jobs.get(account_email) or {}
                job["running"] = False
                job["finished_at"] = time.time()
                job["error"] = str(exc)
                job["message"] = "Import failed."
                groups_jobs[account_email] = job
        finally:
            with groups_jobs_lock:
                groups_cancel_events.pop(account_email, None)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return True, "Import started. Groups will appear live as they are found."


@web_bp.route("/groups/import", methods=["POST"])
def groups_import():
    active_account = get_active_account()
    if not active_account:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": "No active account selected."}), 400
        return _render_groups_page(message="No active account selected.", message_type="failed")

    ok, msg = _start_groups_import_job(active_account)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    return _render_groups_page(message=msg, message_type=("success" if ok else "failed"))


@web_bp.route("/groups/import/stop", methods=["POST"])
def groups_import_stop():
    active_account = get_active_account()
    if not active_account:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": "No active account selected."}), 400
        return _render_groups_page(message="No active account selected.", message_type="failed")

    with groups_jobs_lock:
        job = groups_jobs.get(active_account) or {}
        if not job.get("running"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"ok": False, "message": "No import is running."}), 409
            return _render_groups_page(message="No import is running.", message_type="failed")

        job["stop_requested"] = True
        job["message"] = "Stop requested. Finishing current step..."
        groups_jobs[active_account] = job

        ev = groups_cancel_events.get(active_account)
        if ev is not None:
            ev.set()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "message": "Stop requested."})
    return _render_groups_page(message="Stop requested.", message_type="success")


@web_bp.route("/groups/add_by_link", methods=["POST"])
def groups_add_by_link():
    active_account = get_active_account()
    if not active_account:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": "No active account selected."}), 400
        session["groups_message"] = "No active account selected."
        session["groups_message_type"] = "failed"
        return redirect(url_for("web.groups_page"))

    link = (request.form.get("group_link") or "").strip()
    if not link:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": "Please enter a group link."}), 400
        session["groups_message"] = "Please enter a group link."
        session["groups_message_type"] = "failed"
        return redirect(url_for("web.groups_page"))

    try:
        result = add_buy_sell_group_by_link(active_account, link)
    except Exception as exc:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": f"Failed to add group: {exc}"}), 500
        session["groups_message"] = f"Failed to add group: {exc}"
        session["groups_message_type"] = "failed"
        return redirect(url_for("web.groups_page"))

    ok = bool(result.get("ok"))
    message = result.get("message", "")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    session["groups_message"] = message
    session["groups_message_type"] = "success" if ok else "failed"
    return redirect(url_for("web.groups_page"))


@web_bp.route("/groups/data", methods=["GET"])
def groups_data():
    active_account = get_active_account()
    if not active_account:
        return jsonify({"ok": False, "message": "No active account selected.", "groups": [], "status": {"running": False}})

    payload = load_groups(active_account)
    groups = payload.get("groups") or []
    groups = [g for g in groups if isinstance(g, dict)]

    with groups_jobs_lock:
        import_status = dict(groups_jobs.get(active_account) or {"running": False, "finished_at": None})

    status = {"running": False, "message": "", "stop_requested": False, "mode": "idle"}
    if import_status.get("running"):
        status = {
            "running": True,
            "message": import_status.get("message") or "",
            "stop_requested": import_status.get("stop_requested", False),
            "mode": "import",
        }
    else:
        import_finished = import_status.get("finished_at") or 0
        if import_finished:
            status["message"] = import_status.get("message") or ""
            status["mode"] = "import"

    return jsonify({"ok": True, "groups": groups, "status": status})


@web_bp.route("/groups/delete", methods=["POST"])
def groups_delete():
    active_account = get_active_account()
    if not active_account:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": "No active account selected."}), 400
        return _render_groups_page(message="No active account selected.", message_type="failed")

    group_ids = request.form.getlist("group_id")
    if not group_ids:
        raw = (request.form.get("group_ids") or "").strip()
        if raw:
            group_ids = [g.strip() for g in raw.split(",") if g.strip()]

    deduped = []
    seen = set()
    for gid in group_ids:
        if gid not in seen:
            seen.add(gid)
            deduped.append(gid)
    group_ids = deduped

    if not group_ids:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": "No groups selected."}), 400
        return _render_groups_page(message="No group selected.", message_type="failed")

    deleted = 0
    for gid in group_ids:
        if delete_group_record(active_account, gid):
            deleted += 1

    if deleted == 0:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "message": "No groups were deleted."}), 404
        return _render_groups_page(message="Group not found.", message_type="failed")

    msg = f"Deleted {deleted} group{'s' if deleted != 1 else ''}."
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "message": msg})
    return _render_groups_page(message=msg, message_type="success")


@web_bp.route("/posting_in_groups", methods=["GET", "POST"])
def posting_in_groups_page():
    accounts = get_all_accounts()
    active_account = get_active_account()

    if request.method == "POST":
        if not active_account:
            return render_template(
                "posting_in_groups.html",
                message="No active account selected.",
                message_type="failed",
                accounts=accounts,
                active_account=None,
                active_page="posting_in_groups",
            )

        description = (request.form.get("description") or "").strip()
        group_links_raw = request.form.get("group_links") or ""
        delay_unit = (request.form.get("delay_unit") or "").strip().lower()
        delay_value_raw = (request.form.get("delay_value") or "").strip()

        if not description:
            return render_template(
                "posting_in_groups.html",
                message="Description is required.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="posting_in_groups",
            )

        group_links = _parse_manual_urls(group_links_raw)
        if not group_links:
            return render_template(
                "posting_in_groups.html",
                message="Please provide at least one group link.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="posting_in_groups",
            )

        if delay_unit not in {"seconds", "minutes", "hours"}:
            return render_template(
                "posting_in_groups.html",
                message="Please select a valid delay unit (seconds, minutes, or hours).",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="posting_in_groups",
            )

        try:
            delay_value = int(delay_value_raw)
        except ValueError:
            return render_template(
                "posting_in_groups.html",
                message="Delay value must be a whole number.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="posting_in_groups",
            )

        if delay_value < 1:
            return render_template(
                "posting_in_groups.html",
                message=f"{delay_unit.capitalize()} delay must be at least 1.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="posting_in_groups",
            )

        multiplier = {"seconds": 1, "minutes": 60, "hours": 3600}
        delay_seconds = delay_value * multiplier[delay_unit]

        image_files = [f for f in request.files.getlist("images") if f and f.filename]
        if not image_files:
            return render_template(
                "posting_in_groups.html",
                message="Please upload at least one image.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="posting_in_groups",
            )

        saved_image_paths = _save_group_post_images(image_files)
        if not saved_image_paths:
            return render_template(
                "posting_in_groups.html",
                message="Failed to save uploaded images.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="posting_in_groups",
            )

        def _run_group_posting():
            try:
                result = post_anonymous_group_posts(
                    active_account,
                    group_links,
                    description,
                    saved_image_paths,
                    delay_seconds=delay_seconds,
                    progress_callback=lambda msg: _runtime_log(msg),
                )
                _runtime_log(f"[GroupPosting] {result.get('message')}")
            except Exception as exc:
                _runtime_log(f"[GroupPosting] Failed: {exc}")

        threading.Thread(target=_run_group_posting, daemon=True).start()

        return render_template(
            "posting_in_groups.html",
            message="Group posting started in background.",
            message_type="loading",
            accounts=accounts,
            active_account=active_account,
            active_page="posting_in_groups",
        )

    return render_template(
        "posting_in_groups.html",
        accounts=accounts,
        active_account=active_account,
        active_page="posting_in_groups",
    )


@web_bp.route("/reposting", methods=["GET"])
def reposting_page():
    accounts = get_all_accounts()
    active_account = get_active_account()
    schedule_data = _collect_repost_schedule(active_account)
    repost_is_running = _is_reposting_running_for_account(active_account)

    # Reposting is scheduled only from per-listing controls on Manage Listings.
    # This page is now a read-only queue/status view.
    return render_template(
        "reposting.html",
        accounts=accounts,
        active_account=active_account,
        active_page="reposting",
        repost_is_running=repost_is_running,
        repost_items=schedule_data["items"],
        repost_history=_collect_repost_history(active_account),
    )

    if request.method == "POST":
        if not active_account:
            return render_template(
                "reposting.html",
                message="No active account selected.",
                message_type="failed",
                accounts=accounts,
                active_account=None,
                active_page="reposting",
                repost_is_running=False,
                repost_items=[],
            )

        source_mode = (request.form.get("source_mode") or "excel").strip().lower()
        delay_unit = (request.form.get("delay_unit") or "").strip().lower()
        delay_value_raw = (request.form.get("delay_value") or "").strip()

        if delay_unit not in {"minutes", "hours"}:
            return render_template(
                "reposting.html",
                message="Please select a valid delay unit (minutes or hours).",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="reposting",
                repost_is_running=repost_is_running,
                repost_items=schedule_data["items"],
            )

        try:
            delay_value = int(delay_value_raw)
        except ValueError:
            return render_template(
                "reposting.html",
                message="Delay value must be a whole number.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="reposting",
                repost_is_running=repost_is_running,
                repost_items=schedule_data["items"],
            )

        if delay_unit == "hours" and not (1 <= delay_value <= 24):
            return render_template(
                "reposting.html",
                message="Hours delay must be between 1 and 24.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="reposting",
                repost_is_running=repost_is_running,
                repost_items=schedule_data["items"],
            )

        if delay_unit == "minutes" and not (1 <= delay_value <= 59):
            return render_template(
                "reposting.html",
                message="Minutes delay must be between 1 and 59.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="reposting",
                repost_is_running=repost_is_running,
                repost_items=schedule_data["items"],
            )

        delay_seconds = delay_value * (3600 if delay_unit == "hours" else 60)
        urls = []

        if source_mode == "excel":
            if "excel_file" not in request.files:
                return render_template(
                    "reposting.html",
                    message="No Excel file uploaded.",
                    message_type="failed",
                    accounts=accounts,
                    active_account=active_account,
                    active_page="reposting",
                    repost_is_running=repost_is_running,
                    repost_items=schedule_data["items"],
                )

            file = request.files["excel_file"]
            if not file or file.filename == "":
                return render_template(
                    "reposting.html",
                    message="No Excel file selected.",
                    message_type="failed",
                    accounts=accounts,
                    active_account=active_account,
                    active_page="reposting",
                    repost_is_running=repost_is_running,
                    repost_items=schedule_data["items"],
                )

            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
            try:
                file.save(temp_file.name)
                temp_file.close()
                urls = _extract_urls_from_excel(temp_file.name)
            except Exception as e:
                return render_template(
                    "reposting.html",
                    message=f"Failed to read Excel URLs: {e}",
                    message_type="failed",
                    accounts=accounts,
                    active_account=active_account,
                    active_page="reposting",
                    repost_is_running=repost_is_running,
                    repost_items=schedule_data["items"],
                )
            finally:
                try:
                    if os.path.exists(temp_file.name):
                        os.remove(temp_file.name)
                except Exception:
                    pass
        else:
            urls = _parse_manual_urls(request.form.get("manual_urls"))

        if not urls:
            return render_template(
                "reposting.html",
                message="No valid post URLs found.",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="reposting",
                repost_is_running=repost_is_running,
                repost_items=schedule_data["items"],
            )

        valid_urls, invalid_urls = _split_valid_invalid_urls(urls)
        if invalid_urls:
            invalid_preview = ", ".join(invalid_urls[:5])
            extra = "" if len(invalid_urls) <= 5 else f" (+{len(invalid_urls) - 5} more)"
            return render_template(
                "reposting.html",
                message=f"Invalid URL(s) found: {invalid_preview}{extra}. URLs must start with http:// or https://",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="reposting",
                repost_is_running=repost_is_running,
                repost_items=schedule_data["items"],
            )

        batch_time = datetime.now()
        safe_account = re.sub(r"[^a-zA-Z0-9_]+", "_", active_account or "account")
        for index, post_url in enumerate(valid_urls, start=1):
            run_at = batch_time + timedelta(seconds=delay_seconds * index)
            job_id = f"repost_read_{safe_account}_{int(batch_time.timestamp())}_{index}_{uuid4().hex[:8]}"
            scheduler.add_job(
                _run_reposting_read_job,
                trigger="date",
                run_date=run_at,
                args=[post_url, active_account, job_id],
                id=job_id,
                replace_existing=False
            )
            _register_repost_job(
                job_id=job_id,
                account=active_account,
                post_url=post_url,
                run_at=run_at,
                delay_value=delay_value,
                delay_unit=delay_unit
            )
            _runtime_log(
                f"[Reposting] Scheduled read job={job_id} account={active_account} run_at={run_at.strftime('%Y-%m-%d %H:%M:%S')} url={post_url}"
            )

        return render_template(
            "reposting.html",
            message=(
                f"Scheduled read for {len(valid_urls)} URL(s). "
                f"Each URL will run every {delay_value} {delay_unit}. "
                f"First URL will run after the selected delay."
            ),
            message_type="success",
            accounts=accounts,
            active_account=active_account,
            active_page="reposting",
            repost_is_running=True,
            repost_items=_collect_repost_schedule(active_account)["items"],
        )

    return render_template(
        "reposting.html",
        accounts=accounts,
        active_account=active_account,
        active_page="reposting",
        repost_is_running=repost_is_running,
        repost_items=schedule_data["items"],
    )


@web_bp.route("/reposting_data", methods=["GET"])
def reposting_data():
    active_account = get_active_account()
    data = _collect_repost_schedule(active_account)
    items_payload = []
    for item in data["items"]:
        items_payload.append({
            "job_id": item["job_id"],
            "listing_id": item["listing_id"],
            "title": item["title"],
            "status": item["status"],
            "run_at": item["run_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_seconds": item["remaining_seconds"],
            "delay_label": f"{item['delay_value']} {item['delay_unit']}".strip(),
        })

    return jsonify({
        "ok": True,
        "account": active_account,
        "pending_count": data["pending_count"],
        "running_count": data["running_count"],
        "items": items_payload,
        "history": _collect_repost_history(active_account),
    })


@web_bp.route("/reposting_history/clear", methods=["POST"])
def reposting_history_clear():
    active_account = get_active_account()
    if not active_account:
        return jsonify({"ok": False, "message": "No active account selected."}), 400
    with repost_history_lock:
        items = _load_repost_history()
        kept = [item for item in items if item.get("account") != active_account]
        removed = len(items) - len(kept)
        _save_repost_history(kept)
    return jsonify({
        "ok": True,
        "message": f"Cleared {removed} repost history record(s).",
    })


@web_bp.route("/info")
def info_page():
    accounts = get_all_accounts()
    active_account = get_active_account()
    return render_template(
        "info.html",
        accounts=accounts,
        active_account=active_account,
        active_page="info"
    )

@web_bp.route("/email_setter", methods=["GET", "POST"])
def email_setter():
    accounts = get_all_accounts()
    active_account = get_active_account()
    current_email = get_smtp_receiver_email()

    if request.method == "GET":
        return render_template(
            "email_setter.html",
            accounts=accounts,
            active_account=active_account,
            active_page="email_setter",
            current_email=current_email,
        )

    action = (request.form.get("action") or "set").strip().lower()
    if action == "delete":
        cleared = clear_smtp_receiver_email()
        return render_template(
            "email_setter.html",
            message="Receiver email deleted." if cleared else "Failed to delete receiver email.",
            message_type="success" if cleared else "failed",
            accounts=accounts,
            active_account=active_account,
            active_page="email_setter",
            current_email=get_smtp_receiver_email(),
        )

    submitted_email = str(request.form.get("receiver_email") or "").strip().lower()
    if not submitted_email:
        return render_template(
            "email_setter.html",
            message="Please enter an email address.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="email_setter",
            current_email=current_email,
        )

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", submitted_email):
        return render_template(
            "email_setter.html",
            message="Please enter a valid email address.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="email_setter",
            current_email=current_email,
        )

    set_smtp_receiver_email(submitted_email)
    return render_template(
        "email_setter.html",
        message="Receiver email saved successfully.",
        message_type="success",
        accounts=accounts,
        active_account=active_account,
        active_page="email_setter",
        current_email=get_smtp_receiver_email(),
    )

# -------------------------
# Account management
# -------------------------

@web_bp.route("/account", methods=["GET", "POST"])
def account_page():
    cleanup_finished_remote_login_sessions()
    cleanup_expired_cdp_logins()
    cleanup_orphaned_cdp_accounts()
    accounts = get_all_accounts()
    is_phone_client = _is_probable_phone_request(request)

    if request.method == "GET":
        return render_template(
            "account.html",
            message=session.pop("account_message", None),
            message_type=session.pop("account_message_type", None),
            is_phone_client=is_phone_client,
            **_account_page_context(accounts),
        )

    account_type = (request.form.get("account_type") or "facebook").strip().lower()

    if account_type == "instagram":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        success, message = login_instagram(email, password)
        accounts = get_all_accounts()
        return render_template(
            "account.html",
            message=message,
            message_type=("success" if success else "failed"),
            is_phone_client=is_phone_client,
            **_account_page_context(accounts),
        )

    account_title = (request.form.get("account_title") or "").strip()
    try:
        meta = start_cdp_login(account_title)
        message = (
            f"Chrome opened for {account_title} on port {meta.get('port')}. "
            "Log into Facebook there; this page will recognize the account after login."
        )
        message_type = "success"
    except Exception as exc:
        message = f"Could not open Facebook login: {exc}"
        message_type = "failed"

    accounts = get_all_accounts()
    return render_template(
        "account.html",
        message=message,
        message_type=message_type,
        is_phone_client=is_phone_client,
        **_account_page_context(accounts),
    )


@web_bp.route("/account/cdp_status", methods=["GET"])
def account_cdp_status():
    account_title = (request.args.get("account") or "").strip()
    cleanup_expired_cdp_logins()
    meta = get_cdp_account(account_title)
    if not meta:
        return jsonify({"ok": False, "status": "missing", "message": "Account login was not found."}), 404
    ready = meta.get("status") == "ready" and account_title in set(get_all_accounts())
    if ready:
        session["active_account"] = account_title
        set_session_health(account_title, HEALTHY)
        close_cdp_browser(account_title)
        session["account_message"] = f"Login successful: {account_title}. Facebook session saved."
        session["account_message_type"] = "success"
    return jsonify({
        "ok": True,
        "ready": ready,
        "status": meta.get("status", "waiting"),
        "message": meta.get("message", "Waiting for Facebook login."),
        "port": meta.get("port"),
    })


@web_bp.route("/instagram_account/delete", methods=["POST"])
def instagram_account_delete():
    removed = delete_instagram_account()
    accounts = get_all_accounts()
    return render_template(
        "account.html",
        message=("Instagram cookies deleted." if removed else "No saved Instagram cookies found."),
        message_type=("success" if removed else "failed"),
        is_phone_client=_is_probable_phone_request(request),
        **_account_page_context(accounts),
    )


@web_bp.route("/account/phone_verify/<session_id>", methods=["GET"])
def account_phone_verify(session_id):
    cleanup_finished_remote_login_sessions()
    remote_session = get_remote_login_session(session_id)
    if not remote_session:
        return render_template(
            "account_phone_verify.html",
            session_id=session_id,
            session_state={"status": "failed", "message": "Phone verification session was not found or expired."},
            accounts=get_all_accounts(),
            active_account=get_active_account(),
            active_page="account",
        )

    return render_template(
        "account_phone_verify.html",
        session_id=session_id,
        session_state={
            "status": remote_session.status,
            "message": remote_session.message,
            "email": remote_session.email,
            "account_name": remote_session.account_name,
        },
        accounts=get_all_accounts(),
        active_account=get_active_account(),
        active_page="account",
    )


@web_bp.route("/account/phone_verify/<session_id>/state", methods=["GET"])
def account_phone_verify_state(session_id):
    cleanup_finished_remote_login_sessions()
    remote_session = get_remote_login_session(session_id)
    if not remote_session:
        return jsonify({"ok": False, "status": "failed", "message": "Session expired"}), 404

    return jsonify(
        {
            "ok": True,
            "status": remote_session.status,
            "message": remote_session.message,
            "email": remote_session.email,
            "account_name": remote_session.account_name,
        }
    )


@web_bp.route("/account/phone_verify/<session_id>/frame", methods=["GET"])
def account_phone_verify_frame(session_id):
    remote_session = get_remote_login_session(session_id)
    if not remote_session:
        return ("Session expired", 404)

    try:
        frame = remote_session.capture_frame()
    except Exception as exc:
        return (str(exc), 500)

    if not frame:
        return ("Frame not available", 409)
    frame_bytes, content_type = frame
    return frame_bytes, 200, {"Content-Type": content_type, "Cache-Control": "no-store"}


@web_bp.route("/account/phone_verify/<session_id>/action", methods=["POST"])
def account_phone_verify_action(session_id):
    remote_session = get_remote_login_session(session_id)
    if not remote_session:
        return jsonify({"ok": False, "message": "Session expired"}), 404

    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip().lower()

    try:
        if action == "tap":
            remote_session.click(data.get("x"), data.get("y"))
        elif action == "scroll":
            remote_session.scroll(int(data.get("amount") or 0))
        elif action == "text":
            remote_session.send_text(data.get("text") or "")
        elif action == "key":
            remote_session.send_key(data.get("key") or "")
        elif action == "cancel":
            remote_session.cancel()
        else:
            return jsonify({"ok": False, "message": "Unsupported action"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    return jsonify({"ok": True, "status": remote_session.status, "message": remote_session.message})

@web_bp.route("/delete_account", methods=["POST"])
def delete_account():
    is_phone_client = _is_probable_phone_request(request)
    account_email = request.form.get("account")
    if not account_email:
        return "No account specified", 400

    session_file = os.path.join(SESSION_DIR, email_to_filename(account_email))

    removed_session = os.path.exists(session_file)
    removed_cdp = delete_cdp_account(account_email)

    if removed_session:
        os.remove(session_file)
    if removed_session or removed_cdp:
        remove_session_health(account_email)
        delete_history(account_email)
        message = f"Deleted account {account_email} and its dedicated Chrome profile"
        message_type = "success"
    else:
        message = f"Account {account_email} not found"
        message_type = "failed"

    if session.get("active_account") == account_email:
        session.pop("active_account", None)

    accounts = get_all_accounts()
    return render_template(
        "account.html",
        message=message,
        message_type=message_type,
        is_phone_client=is_phone_client,
        **_account_page_context(accounts),
    )


@web_bp.route("/account/session_check/start", methods=["POST"])
def account_session_check_start():
    saved_accounts = set(get_all_accounts())
    payload = request.get_json(silent=True) or {}
    requested_accounts = payload.get("accounts") or request.form.getlist("accounts")
    accounts = [
        str(account or "").strip()
        for account in requested_accounts
        if str(account or "").strip() in saved_accounts
    ]
    if not accounts:
        return jsonify({"ok": False, "message": "No saved accounts selected."}), 400

    started, job = start_session_check(accounts)
    return jsonify(
        {
            "ok": started,
            "message": "Session check started." if started else "Session check is already running.",
            "session_check": job.snapshot(),
            "account_health": load_session_health(),
        }
    )


@web_bp.route("/account/session_check/status", methods=["GET"])
def account_session_check_status():
    return jsonify(
        {
            "ok": True,
            "session_check": get_session_check_snapshot(),
            "account_health": load_session_health(),
        }
    )


@web_bp.route("/account/session_check/cancel", methods=["POST"])
def account_session_check_cancel():
    cancelled = cancel_session_check()
    return jsonify(
        {
            "ok": True,
            "cancelled": cancelled,
            "session_check": get_session_check_snapshot(),
            "account_health": load_session_health(),
        }
    )

@web_bp.route("/set_active_account", methods=["POST"])
def set_active_account():
    selected = request.form.get("account")

    if selected:
        session["active_account"] = selected

    referrer = request.referrer or ""
    # Background action forms can leave the browser referrer on a POST-only
    # endpoint (for example /delete_listings/import). Never redirect account
    # switching back to an action URL; return to the page that owns the data.
    if referrer and urlparse(referrer).path.startswith("/delete_listings/"):
        referrer = url_for("web.delete_listings_page")
    return redirect(referrer or url_for("web.dashboard"))


@web_bp.route("/set_background_mode", methods=["POST"])
def set_background_mode():
    enabled = request.form.get("run_in_background") == "1"
    set_run_in_background_enabled(enabled)
    return redirect(request.referrer or url_for("web.dashboard"))

# -------------------------
# Auto listing
# -------------------------

@web_bp.route("/auto_listing")
def auto_listing_page():
    accounts = get_all_accounts()
    active_account = get_active_account()
    auto_listing_running = _is_auto_listing_running_for_account(active_account)

    return render_template(
        "auto_listing.html",
        accounts=accounts,
        active_account=active_account,
        active_page="auto_listing",
        auto_listing_running=auto_listing_running,
    )


@web_bp.route("/auto_listing_clear_junk", methods=["POST"])
def auto_listing_clear_junk():
    active_account = get_active_account()
    auto_listing_running = _is_auto_listing_running_for_account(active_account)
    redirect_target = request.referrer or url_for("web.auto_listing_page")

    if auto_listing_running:
        return redirect(redirect_target)

    folders = [screenshots_dir(), _manual_images_directory()]
    # Also clear legacy/project-root data folders (useful when running from source).
    try:
        project_data = os.path.join(str(project_root()), "data")
    except Exception:
        project_data = ""
    if project_data:
        folders.extend(
            [
                os.path.join(project_data, "screenshots"),
                os.path.join(project_data, "manual_listing_images"),
            ]
        )
    # De-dupe paths (runtime may already point at project root).
    unique_folders = []
    seen = set()
    for folder in folders:
        if not folder:
            continue
        try:
            key = os.path.normcase(os.path.normpath(folder))
        except Exception:
            key = folder
        if key in seen:
            continue
        seen.add(key)
        unique_folders.append(folder)

    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".jfif"}
    try:
        for folder in unique_folders:
            if not folder or not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                ext = os.path.splitext(name)[1].strip().lower()
                if ext in image_exts:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
    except Exception as e:
        _runtime_log(f"[AutoListing] Clear junk failed: {e}")

    return redirect(redirect_target)

@web_bp.route("/auto_listing_excel", methods=["GET"])
def auto_listing_excel():
    accounts = get_all_accounts()
    active_account = get_active_account()
    auto_listing_running = _is_auto_listing_running_for_account(active_account)

    return render_template(
        "auto_listing_excel.html",
        accounts=accounts,
        active_account=active_account,
        active_page="auto_listing",
        auto_listing_running=auto_listing_running,
    )

@web_bp.route("/auto_listing_excel", methods=["POST"])
def auto_listing_excel_post():
    accounts = get_all_accounts()
    active_account = get_active_account()
    auto_listing_running = _is_auto_listing_running_for_account(active_account)

    if not active_account:
        return render_template(
            "auto_listing_excel.html",
            message="No active account selected",
            message_type="failed",
            accounts=accounts,
            active_account=None,
            active_page="auto_listing",
            auto_listing_running=False,
        )

    if auto_listing_running:
        return render_template(
            "auto_listing_excel.html",
            message="Auto Listing is already running for this account. Wait until scheduled/ongoing posts finish.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_listing",
            auto_listing_running=True,
        )

    if "excel_file" not in request.files:
        return render_template(
            "auto_listing_excel.html",
            message="No file uploaded",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_listing",
            auto_listing_running=False,
        )

    file = request.files["excel_file"]
    delay_unit = (request.form.get("delay_unit") or "").strip().lower()
    delay_value_raw = (request.form.get("delay_value") or "").strip()

    valid_units = {"hours", "minutes"}
    if delay_unit not in valid_units:
        return render_template(
            "auto_listing_excel.html",
            message="Please select a valid delay unit.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_listing",
            auto_listing_running=False,
        )

    try:
        delay_value = int(delay_value_raw)
    except ValueError:
        return render_template(
            "auto_listing_excel.html",
            message="Delay value must be a whole number.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_listing",
            auto_listing_running=False,
        )

    if delay_value < 1:
        return render_template(
            "auto_listing_excel.html",
            message=f"{delay_unit.capitalize()} delay must be at least 1.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_listing",
            auto_listing_running=False,
        )

    delay_multiplier = {"hours": 3600, "minutes": 60}
    delay_seconds = delay_value * delay_multiplier[delay_unit]

    if file.filename == "":
        return render_template(
            "auto_listing_excel.html",
            message="No file selected",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_listing",
            auto_listing_running=False,
        )

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    file.save(temp_file.name)
    temp_file.close()

    try:
        post_data_list = read_excel_flexible(temp_file.name)

        if not post_data_list:
            raise Exception("Excel file is empty or invalid")

        total_posts = len(post_data_list)
        batch_time = datetime.now()

        for index, post_data in enumerate(post_data_list, start=1):
            run_at = batch_time + timedelta(seconds=delay_seconds * index)
            safe_account = re.sub(r"[^a-zA-Z0-9_]+", "_", active_account or "account")
            job_id = f"excel_{safe_account}_{int(batch_time.timestamp())}_{index}_{uuid4().hex[:8]}"
            scheduler.add_job(
                _post_single_listing,
                trigger="date",
                run_date=run_at,
                args=[post_data, active_account, job_id],
                id=job_id,
                replace_existing=False
            )
            _register_scheduled_post(job_id, active_account, post_data, run_at)
            _runtime_log(
                f"[AutoListing] Scheduled job={job_id} account={active_account} run_at={run_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        message = (
            f"Scheduled {total_posts} listing(s). "
            f"Each post will run every {delay_value} {delay_unit}. "
            f"First post will also run after the selected delay."
        )
        message_type = "success"

    except Exception as e:
        return render_template(
            "auto_listing_excel.html",
            message=f"Error processing Excel: {str(e)}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_listing",
            auto_listing_running=False,
        )

    finally:
        if os.path.exists(temp_file.name):
            os.remove(temp_file.name)

    return render_template(
        "auto_listing_excel.html",
        message=message,
        message_type=message_type,
        accounts=accounts,
        active_account=active_account,
        active_page="auto_listing",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
    )


@web_bp.route("/auto_listing_manual", methods=["GET"])
def auto_listing_manual():
    accounts = get_all_accounts()
    active_account = get_active_account()
    prefill = session.pop("manual_prefill", None)
    groups_payload = load_groups(active_account) if active_account else {"groups": []}
    groups = groups_payload.get("groups") or []
    groups = [g for g in groups if isinstance(g, dict)]
    return render_template(
        "auto_listing_manual.html",
        accounts=accounts,
        active_account=active_account,
        active_page="auto_listing",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
        prefill=prefill,
        groups=groups,
    )


@web_bp.route("/auto_listing_manual_instagram", methods=["POST"])
def auto_listing_manual_instagram():
    # Dedicated endpoint prevents the Instagram button from ever entering
    # Facebook's normal posting path, even if another submit control is added.
    return auto_listing_manual_post()


@web_bp.route("/auto_listing_manual", methods=["POST"])
def auto_listing_manual_post():
    accounts = get_all_accounts()
    active_account = get_active_account()
    groups_payload = load_groups(active_account) if active_account else {"groups": []}
    groups = groups_payload.get("groups") or []
    groups = [g for g in groups if isinstance(g, dict)]

    def _render_manual(message, message_type, active_account_override=None):
        account_value = active_account_override if active_account_override is not None else active_account
        return render_template(
            "auto_listing_manual.html",
            message=message,
            message_type=message_type,
            accounts=accounts,
            active_account=account_value,
            active_page="auto_listing",
            auto_listing_running=_is_auto_listing_running_for_account(account_value),
            groups=groups,
        )

    post_action = (request.form.get("post_action") or "simple").strip().lower()
    if request.path.rstrip("/") == "/auto_listing_manual_instagram":
        post_action = "instagram"
    _runtime_log(f"[ManualListing] endpoint={request.path} post_action={post_action}")

    if post_action in {"instagram", "instagram_post"}:
        try:
            if not instagram_account():
                return _render_manual("Save an Instagram account before scheduling an Instagram post.", "failed")
            image_files = [
                uploaded for uploaded in request.files.getlist("images")
                if uploaded and str(uploaded.filename or "").strip()
            ]
            prefilled_paths = _get_prefilled_manual_image_paths(request.form)
            if not image_files and not prefilled_paths:
                return _render_manual("Select at least one image for the Instagram post.", "failed")
            saved_image_paths = _save_manual_images(image_files) if image_files else []
            for image_path in prefilled_paths:
                if len(saved_image_paths) >= 10:
                    break
                if image_path not in saved_image_paths:
                    saved_image_paths.append(image_path)
            if not saved_image_paths:
                return _render_manual("No valid images were found for the Instagram post.", "failed")

            instagram_post_data = _build_manual_post_data(request.form)
            instagram_post_data["post_action"] = "instagram"
            instagram_post_data["instagram_image_paths"] = saved_image_paths[:10]
            delay_unit = (request.form.get("delay_unit") or "").strip().lower()
            delay_value_raw = (request.form.get("delay_value") or "").strip()
            if delay_unit not in {"hours", "minutes"}:
                return _render_manual("Please select a valid delay unit for Instagram.", "failed")
            try:
                delay_value = int(delay_value_raw)
            except ValueError:
                return _render_manual("Instagram delay value must be a whole number.", "failed")
            if delay_value < 1:
                return _render_manual("Instagram delay must be at least 1.", "failed")

            delay_seconds = delay_value * {"hours": 3600, "minutes": 60}[delay_unit]
            run_at = datetime.now() + timedelta(seconds=delay_seconds)
            job_id = f"instagram_{int(datetime.now().timestamp())}_{uuid4().hex[:8]}"
            scheduler.add_job(
                _open_instagram_scheduled_post,
                trigger="date",
                run_date=run_at,
                args=[job_id],
                id=job_id,
                replace_existing=False,
            )
            _register_scheduled_post(
                job_id,
                INSTAGRAM_SCHEDULER_ACCOUNT,
                instagram_post_data,
                run_at,
                platform="instagram",
            )
            _log_event(
                event_type="instagram_post",
                account=INSTAGRAM_SCHEDULER_ACCOUNT,
                status="success",
                reference="Instagram post",
                details=f"Scheduled job {job_id} for {run_at.strftime('%Y-%m-%d %H:%M:%S')}",
            )
            return _render_manual(
                "Instagram post composer scheduled successfully. It will open in Chrome "
                f"at {run_at.strftime('%Y-%m-%d %H:%M:%S')} and upload the selected images, "
                "write the vehicle caption, and share the post automatically.",
                "success",
            )
        except Exception as exc:
            _runtime_log(f"[Instagram] Could not open post page: {exc}")
            return _render_manual(f"Could not open Instagram post page: {exc}", "failed")

    multi_account = request.form.get("multi_account") == "1"
    saved_account_set = {str(account).strip() for account in accounts if str(account).strip()}
    requested_accounts = [
        str(account or "").strip()
        for account in request.form.getlist("target_accounts")
        if str(account or "").strip() in saved_account_set
    ]
    target_accounts = list(dict.fromkeys(requested_accounts)) if multi_account else ([active_account] if active_account else [])
    if multi_account and not target_accounts:
        return _render_manual("Select at least one account for multi-account posting.", "failed")
    if not target_accounts:
        return _render_manual("No active account selected", "failed", active_account_override=None)
    if post_action == "group" and not active_account:
        return _render_manual("Select an active account before using Group Post.", "failed", active_account_override=None)

    group_ids = []
    group_names = []
    if post_action == "group":
        raw_group_ids = [str(g or "").strip() for g in request.form.getlist("group_ids")]
        seen = set()
        for gid in raw_group_ids:
            if not gid or gid in seen:
                continue
            seen.add(gid)
            group_ids.append(gid)
        if len(group_ids) > 20:
            return _render_manual("You can select up to 20 groups.", "failed")
        try:
            allowed = {
                str((g or {}).get("id", "")).strip()
                for g in (load_groups(active_account).get("groups") or [])
                if isinstance(g, dict)
            }
            group_ids = [gid for gid in group_ids if gid in allowed]
        except Exception:
            pass
        try:
            group_name_map = {
                str((g or {}).get("id", "")).strip(): str((g or {}).get("name", "")).strip()
                for g in groups
                if isinstance(g, dict)
            }
            for gid in group_ids:
                name = group_name_map.get(gid)
                if name:
                    group_names.append(name)
        except Exception:
            group_names = []

    delay_unit = (request.form.get("delay_unit") or "").strip().lower()
    delay_value_raw = (request.form.get("delay_value") or "").strip()
    if delay_unit not in {"hours", "minutes"}:
        return _render_manual("Please select a valid delay unit.", "failed")

    try:
        delay_value = int(delay_value_raw)
    except ValueError:
        return _render_manual("Delay value must be a whole number.", "failed")

    if delay_value < 1:
        return _render_manual(f"{delay_unit.capitalize()} delay must be at least 1.", "failed")

    delay_seconds = delay_value * {"hours": 3600, "minutes": 60}[delay_unit]
    run_at = datetime.now() + timedelta(seconds=delay_seconds)

    price = str(request.form.get("price") or "").strip()
    if not price:
        return _render_manual("Price is required.", "failed")

    post_data = _build_manual_post_data(request.form)
    missing_color_fields = [
        label
        for field, label in (
            ("exterior_color", "Exterior color"),
            ("interior_color", "Interior color"),
        )
        if not post_data.get(field)
    ]
    if missing_color_fields:
        return _render_manual(
            f"{', '.join(missing_color_fields)} must be selected.",
            "failed",
        )
    if post_action == "group":
        post_data["group_ids"] = list(group_ids)
        post_data["group_names"] = list(group_names)
        post_data["post_action"] = "group"

    image_files = [f for f in request.files.getlist("images") if f and str(f.filename or "").strip()]

    prefilled_filenames = [
        str(v or "").strip() for v in request.form.getlist("prefilled_images") if str(v or "").strip()
    ]
    prefilled_paths = []
    if prefilled_filenames:
        folder = screenshots_dir()
        allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".jfif"}
        for name in prefilled_filenames:
            safe_name = os.path.basename(name)
            if safe_name != name:
                continue
            ext = os.path.splitext(safe_name)[1].lower()
            if ext and ext not in allowed_exts:
                continue
            candidate = os.path.join(folder, safe_name)
            if os.path.isfile(candidate):
                prefilled_paths.append(candidate)

    if not image_files and not prefilled_paths:
        return _render_manual("At least one image is required.", "failed")
    try:
        saved_image_paths = _save_manual_images(image_files) if image_files else []
    except Exception as e:
        return _render_manual(f"Failed to save uploaded images: {e}", "failed")
    for p in prefilled_paths:
        if len(saved_image_paths) >= 10:
            break
        if p not in saved_image_paths:
            saved_image_paths.append(p)

    if not saved_image_paths:
        return _render_manual("At least one valid image is required.", "failed")

    for index, image_path in enumerate(saved_image_paths[:10], start=1):
        post_data[f"image_path{index}"] = image_path

    scheduled_jobs = []
    for account_index, target_account in enumerate(target_accounts):
        account_post_data = copy.deepcopy(post_data)
        account_run_at = run_at + timedelta(seconds=delay_seconds * account_index)
        safe_account = re.sub(r"[^a-zA-Z0-9_]+", "_", target_account or "account")
        job_id = f"manual_{safe_account}_{int(datetime.now().timestamp())}_{uuid4().hex[:8]}"
        scheduler.add_job(
            _post_single_listing,
            trigger="date",
            run_date=account_run_at,
            args=[account_post_data, target_account, job_id],
            id=job_id,
            replace_existing=False,
        )
        _register_scheduled_post(job_id, target_account, account_post_data, account_run_at)
        scheduled_jobs.append((job_id, target_account))
        _runtime_log(
            f"[AutoListing] Scheduled manual job={job_id} account={target_account} run_at={account_run_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        _log_event(
            event_type="scheduler",
            account=target_account,
            status="success",
            reference=post_data.get("title", "Manual Listing"),
            details=f"Added manual scheduled job {job_id} for {account_run_at.strftime('%Y-%m-%d %H:%M:%S')}",
        )

    if post_action == "group":
        group_count = len(group_names) if group_names else 0
        group_label = f"{group_count} group(s)" if group_count else "selected groups"
        if len(target_accounts) > 1:
            message = (
                f"Group posts scheduled for {len(target_accounts)} accounts ({group_label}). "
                f"The first runs at {run_at.strftime('%Y-%m-%d %H:%M:%S')}; "
                f"each next account starts {delay_value} {delay_unit} later."
            )
        else:
            message = (
                "Group post scheduled successfully. "
                f"It will run for {group_label} at {run_at.strftime('%Y-%m-%d %H:%M:%S')}."
            )
    else:
        account_label = f" for {len(target_accounts)} accounts" if len(target_accounts) > 1 else ""
        if len(target_accounts) > 1:
            message = (
                f"Manual posts scheduled for {len(target_accounts)} accounts. "
                f"The first runs at {run_at.strftime('%Y-%m-%d %H:%M:%S')}; "
                f"each next account starts {delay_value} {delay_unit} later."
            )
        else:
            message = (
                "Manual post scheduled successfully. "
                f"It will run at {run_at.strftime('%Y-%m-%d %H:%M:%S')}."
            )
    return _render_manual(message, "success")

@web_bp.route("/website_posting", methods=["GET"])
def website_posting():
    accounts = get_all_accounts()
    active_account = get_active_account()
    return render_template(
        "website_posting.html",
        accounts=accounts,
        active_account=active_account,
        active_page="website_posting",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
    )


@web_bp.route("/website_posting", methods=["POST"])
def website_posting_post():
    accounts = get_all_accounts()
    active_account = get_active_account()
    car_url = (request.form.get("url") or "").strip()

    if not car_url:
        return render_template(
            "website_posting.html",
            message="Please enter the car URL.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        normalized = normalize_http_url(car_url).normalized
    except Exception as e:
        return render_template(
            "website_posting.html",
            message=f"Invalid URL: {e}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        host = (urlparse(normalized).hostname or "").lower()
    except Exception:
        host = ""
    if "cardealsdirect" not in host:
        return render_template(
            "website_posting.html",
            message="Please enter a URL from car deals direct only (cardealsdirect website).",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        _runtime_log(f"[WebsitePosting] Scraping URL: {normalized}")
        scraped_info, scraped_images, opened_url = scrape_vehicle_listing(normalized)
    except Exception as e:
        _runtime_log(f"[WebsitePosting] Scrape failed: {e}")
        return render_template(
            "website_posting.html",
            message=f"Failed to open/scrape this URL: {e}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    return render_template(
        "website_posting.html",
        message=f"Scraped successfully: {opened_url}",
        message_type="success",
        accounts=accounts,
        active_account=active_account,
        active_page="website_posting",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
        scraped_info=scraped_info,
        scraped_images=scraped_images,
    )


@web_bp.route("/website_posting2", methods=["GET"])
def website_posting2():
    accounts = get_all_accounts()
    active_account = get_active_account()
    return render_template(
        "website_posting2.html",
        accounts=accounts,
        active_account=active_account,
        active_page="website_posting2",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
    )


@web_bp.route("/website_posting2", methods=["POST"])
def website_posting2_post():
    accounts = get_all_accounts()
    active_account = get_active_account()
    car_url = (request.form.get("url") or "").strip()

    if not car_url:
        return render_template(
            "website_posting2.html",
            message="Please enter the car URL.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting2",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        normalized = normalize_http_url2(car_url).normalized
    except Exception as e:
        return render_template(
            "website_posting2.html",
            message=f"Invalid URL: {e}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting2",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        host = (urlparse(normalized).hostname or "").lower()
    except Exception:
        host = ""
    if "autorama.ca" not in host:
        return render_template(
            "website_posting2.html",
            message="Please enter a URL from autorama.ca only (autorama website).",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting2",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        _runtime_log(f"[WebsitePosting2] Scraping URL (includes 20s wait): {normalized}")
        scraped_info, scraped_images, opened_url = scrape_autorama_listing(normalized, wait_seconds=0)
    except Exception as e:
        _runtime_log(f"[WebsitePosting2] Scrape failed: {e}")
        return render_template(
            "website_posting2.html",
            message=f"Failed to open/scrape this URL: {e}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting2",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    return render_template(
        "website_posting2.html",
        message=f"Scraped successfully: {opened_url}",
        message_type="success",
        accounts=accounts,
        active_account=active_account,
        active_page="website_posting2",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
        scraped_info=scraped_info,
        scraped_images=scraped_images,
    )


@web_bp.route("/website_posting3", methods=["GET"])
def website_posting3():
    accounts = get_all_accounts()
    active_account = get_active_account()
    return render_template(
        "website_posting3.html",
        accounts=accounts,
        active_account=active_account,
        active_page="website_posting3",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
    )


@web_bp.route("/website_posting3", methods=["POST"])
def website_posting3_post():
    accounts = get_all_accounts()
    active_account = get_active_account()
    car_url = (request.form.get("url") or "").strip()

    if not car_url:
        return render_template(
            "website_posting3.html",
            message="Please enter the car URL.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting3",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        normalized = normalize_http_url(car_url).normalized
    except Exception as e:
        return render_template(
            "website_posting3.html",
            message=f"Invalid URL: {e}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting3",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        host = (urlparse(normalized).hostname or "").lower()
    except Exception:
        host = ""

    if "carsbyjatin.com" not in host:
        return render_template(
            "website_posting3.html",
            message="Please enter a URL from carsbyjatin.com only (carsbyjatin website).",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting3",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    try:
        _runtime_log(f"[WebsitePosting3] Scraping URL: {normalized}")
        scraped_info, scraped_images, opened_url = scrape_carsbyjatin_listing(normalized)
    except Exception as e:
        _runtime_log(f"[WebsitePosting3] Scrape failed: {e}")
        return render_template(
            "website_posting3.html",
            message=f"Failed to open/scrape this URL: {e}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting3",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
        )

    return render_template(
        "website_posting3.html",
        message=f"Scraped successfully: {opened_url}",
        message_type="success",
        accounts=accounts,
        active_account=active_account,
        active_page="website_posting3",
        auto_listing_running=_is_auto_listing_running_for_account(active_account),
        scraped_info=scraped_info,
        scraped_images=scraped_images,
    )


@web_bp.route("/website_posting3_to_manual", methods=["POST"])
def website_posting3_to_manual():
    accounts = get_all_accounts()
    active_account = get_active_account()

    selected_images = [s.strip() for s in request.form.getlist("selected_images") if str(s or "").strip()]
    scraped_images = [s.strip() for s in request.form.getlist("scraped_images") if str(s or "").strip()]

    scraped_info = {
        "make": (request.form.get("make") or "").strip(),
        "model": (request.form.get("model") or "").strip(),
        "year": (request.form.get("year") or "").strip(),
        "body_style": (request.form.get("body_style") or "").strip(),
        "odometer": (request.form.get("odometer") or "").strip(),
        "transmission": (request.form.get("transmission") or "").strip(),
        "exterior": (request.form.get("exterior") or "").strip(),
        "interior": (request.form.get("interior") or "").strip(),
        "fuel_type": (request.form.get("fuel_type") or "").strip(),
    }

    if not selected_images:
        return render_template(
            "website_posting3.html",
            message="Please select at least one image before posting.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting3",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
            scraped_info=scraped_info,
            scraped_images=scraped_images,
        )

    prefill = {
        "make": scraped_info.get("make", ""),
        "model": scraped_info.get("model", ""),
        "year": scraped_info.get("year", ""),
        "body_style": scraped_info.get("body_style", ""),
        "mileage": _digits_only(scraped_info.get("odometer", "")),
        "transmission": scraped_info.get("transmission", ""),
        "exterior_color": scraped_info.get("exterior", ""),
        "interior_color": scraped_info.get("interior", ""),
        "fuel_type": _normalize_fuel_type(scraped_info.get("fuel_type", "")),
        "prefilled_images": selected_images[:10],
    }

    session["manual_prefill"] = prefill
    return redirect(url_for("web.auto_listing_manual"))


@web_bp.route("/website_posting4", methods=["GET"])
def website_posting4():
    accounts = get_all_accounts()
    active_account = get_active_account()
    california_running = _is_california_autofinance_running(active_account)
    california_automation = _california_public_status(active_account)
    return render_template("website_posting4.html", accounts=accounts, active_account=active_account,
                           active_page="website_posting4",
                           auto_listing_running=_is_auto_listing_running_for_account(active_account),
                           california_running=california_running,
                           california_automation=california_automation,
                           carsforsale_automation=_carsforsale_public_status(active_account),
                           carsforsale_login=carsforsale_login_status(),
                           message=session.pop("website_posting4_message", None),
                           message_type=session.pop("website_posting4_message_type", None))


def _is_california_autofinance_running(account):
    if not account:
        return False
    with california_scan_jobs_lock:
        if (california_scan_jobs.get(account) or {}).get("running"):
            return True
    with scheduled_posts_lock:
        for job_id, meta in scheduled_posts.items():
            if not str(job_id).startswith("california_") or meta.get("account") != account:
                continue
            if str(meta.get("status") or "").lower() in {"pending", "waiting", "posting"}:
                return True
    return False


def _schedule_california_daily_run(account, next_run=None):
    if not account:
        return
    run_at = next_run or (datetime.now() + CALIFORNIA_DAILY_INTERVAL)
    scheduler.add_job(
        _run_california_autofinance_scan,
        trigger="interval",
        days=1,
        start_date=run_at,
        args=[account],
        id=_california_daily_job_id(account),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def _set_california_automation(account, post_count, delay_value, delay_unit):
    now = datetime.now()
    next_run = now + CALIFORNIA_DAILY_INTERVAL
    with california_settings_lock:
        settings = _load_california_settings()
        settings[account] = {
            "enabled": True,
            "post_count": post_count,
            "delay_value": delay_value,
            "delay_unit": delay_unit,
            "started_at": now.isoformat(timespec="seconds"),
            "next_scan_at": next_run.isoformat(timespec="seconds"),
        }
        _save_california_settings(settings)
    _schedule_california_daily_run(account, next_run)


def _stop_california_automation(account):
    with california_settings_lock:
        settings = _load_california_settings()
        current = dict(settings.get(account) or {})
        current["enabled"] = False
        current["stopped_at"] = datetime.now().isoformat(timespec="seconds")
        settings[account] = current
        _save_california_settings(settings)
    try:
        scheduler.remove_job(_california_daily_job_id(account))
    except Exception:
        pass

    pending_ids = []
    with scheduled_posts_lock:
        for job_id, meta in scheduled_posts.items():
            if (
                str(job_id).startswith("california_")
                and meta.get("account") == account
                and str(meta.get("status") or "").lower() == "pending"
            ):
                pending_ids.append(job_id)
    for job_id in pending_ids:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        with scheduled_posts_lock:
            if job_id in scheduled_posts:
                scheduled_posts[job_id]["status"] = "deleted"
                scheduled_posts[job_id]["deleted_at"] = datetime.now()
    return len(pending_ids)


def _restore_california_automations():
    with california_settings_lock:
        settings = _load_california_settings()
    for account, item in settings.items():
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        try:
            next_run = datetime.fromisoformat(str(item.get("next_scan_at") or ""))
        except (TypeError, ValueError):
            next_run = datetime.now() + timedelta(seconds=10)
        if next_run <= datetime.now():
            next_run = datetime.now() + timedelta(seconds=10)
        try:
            _schedule_california_daily_run(account, next_run)
            _runtime_log(
                f"[CaliforniaAutoFinance] Restored daily automation account={account} "
                f"posts={item.get('post_count', 4)} delay={item.get('delay_value', 1)} "
                f"{item.get('delay_unit', 'hours')} next={next_run}"
            )
        except Exception as exc:
            _runtime_log(f"[CaliforniaAutoFinance] Restore failed account={account}: {exc}")


def _post_california_listing(post_data, account, job_id=None):
    if job_id:
        with scheduled_posts_lock:
            if job_id in scheduled_posts:
                scheduled_posts[job_id]["status"] = "waiting"
    _runtime_log(f"[CaliforniaAutoFinance] Waiting for account upload lock job={job_id} account={account}")
    # _post_single_listing uses the existing per-account lock. Listings for
    # this account remain sequential, while different accounts may post in
    # parallel using their separate Chrome sessions.
    result = _post_single_listing(post_data, account, job_id)
    mark_history_status(account, (post_data or {}).get("source_url"), "posted" if result else "failed")
    return result


def _run_california_autofinance_scan(account):
    owns_scan = False
    try:
        setting = _california_setting(account)
        if not setting.get("enabled"):
            _runtime_log(f"[CaliforniaAutoFinance] Skipping disabled daily scan account={account}")
            return
        with california_scan_jobs_lock:
            if (california_scan_jobs.get(account) or {}).get("running"):
                _runtime_log(f"[CaliforniaAutoFinance] Scan already running account={account}")
                return
            california_scan_jobs[account] = {
                "running": True,
                "started_at": datetime.now(),
            }
            owns_scan = True
        with california_settings_lock:
            settings = _load_california_settings()
            if account in settings and settings[account].get("enabled"):
                settings[account]["next_scan_at"] = (
                    datetime.now() + CALIFORNIA_DAILY_INTERVAL
                ).isoformat(timespec="seconds")
                _save_california_settings(settings)
        _runtime_log(f"[CaliforniaAutoFinance] Starting inventory scan account={account}")
        post_count = setting.get("post_count", 4)
        delay_value = setting.get("delay_value", 1)
        delay_unit = setting.get("delay_unit", "hours")
        delay_seconds = delay_value * (3600 if delay_unit == "hours" else 60)
        listings = collect_four_listings(account, limit=post_count)
        if not listings:
            _runtime_log(f"[CaliforniaAutoFinance] No new listings found account={account}")
            return
        base_run_at = datetime.now() + timedelta(seconds=2)
        safe_account = re.sub(r"[^a-zA-Z0-9_]+", "_", account or "account")
        for index, post_data in enumerate(listings):
            if not _california_setting(account).get("enabled"):
                _runtime_log(f"[CaliforniaAutoFinance] Automation stopped before queue completed account={account}")
                break
            run_at = base_run_at + timedelta(seconds=delay_seconds * index)
            job_id = f"california_{safe_account}_{int(base_run_at.timestamp())}_{index + 1}_{uuid4().hex[:8]}"
            scheduler.add_job(
                _post_california_listing,
                trigger="date",
                run_date=run_at,
                args=[post_data, account, job_id],
                id=job_id,
                replace_existing=False,
            )
            _register_scheduled_post(job_id, account, post_data, run_at)
            _runtime_log(
                f"[CaliforniaAutoFinance] Scheduled job={job_id} account={account} "
                f"title={post_data.get('title')} source={post_data.get('source_url')}"
            )
    except Exception:
        _runtime_log(f"[CaliforniaAutoFinance] Scan failed account={account}: {traceback.format_exc()}")
    finally:
        if owns_scan:
            with california_scan_jobs_lock:
                job = california_scan_jobs.get(account) or {}
                job["running"] = False
                job["finished_at"] = datetime.now()
                california_scan_jobs[account] = job


@web_bp.route("/website_posting4/california", methods=["POST"])
def website_posting4_california():
    active_account = get_active_account()
    if not active_account:
        session["website_posting4_message"] = "Select an active Facebook account first."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    if _california_setting(active_account).get("enabled"):
        session["website_posting4_message"] = "California Auto Finance daily posting is already enabled for this account."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    try:
        post_count = int(request.form.get("post_count") or 0)
    except (TypeError, ValueError):
        post_count = 0
    if post_count < 1 or post_count > CALIFORNIA_MAX_DAILY_POSTS:
        session["website_posting4_message"] = f"Enter a daily post count from 1 to {CALIFORNIA_MAX_DAILY_POSTS}."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    delay_unit = (request.form.get("delay_unit") or "").strip().lower()
    try:
        delay_value = int(request.form.get("delay_value") or 0)
    except (TypeError, ValueError):
        delay_value = 0
    if delay_unit not in {"minutes", "hours"}:
        session["website_posting4_message"] = "Select a valid delay unit (minutes or hours)."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    delay_max = 59 if delay_unit == "minutes" else 24
    if delay_value < 1 or delay_value > delay_max:
        session["website_posting4_message"] = f"Enter a delay from 1 to {delay_max} {delay_unit}."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    _set_california_automation(active_account, post_count, delay_value, delay_unit)
    threading.Thread(
        target=_run_california_autofinance_scan,
        args=(active_account,),
        daemon=True,
        name=f"california-scan-{re.sub(r'[^a-zA-Z0-9]+', '-', active_account)}",
    ).start()
    session["website_posting4_message"] = (
        f"Daily California Auto Finance posting started with {post_count} post(s), "
        f"spaced {delay_value} {delay_unit} apart."
    )
    session["website_posting4_message_type"] = "success"
    return redirect(url_for("web.website_posting4"))


@web_bp.route("/website_posting4/california_stop", methods=["POST"])
def website_posting4_california_stop():
    active_account = get_active_account()
    if not active_account:
        session["website_posting4_message"] = "Select an active Facebook account first."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    removed = _stop_california_automation(active_account)
    session["website_posting4_message"] = (
        f"California Auto Finance daily posting stopped. Removed {removed} pending post(s)."
    )
    session["website_posting4_message_type"] = "success"
    return redirect(url_for("web.website_posting4"))


@web_bp.route("/website_posting4/california_status", methods=["GET"])
def website_posting4_california_status():
    active_account = get_active_account()
    automation = _california_public_status(active_account)
    return jsonify({
        "ok": True,
        "account": active_account,
        "running": _is_california_autofinance_running(active_account),
        "enabled": automation["enabled"],
        "post_count": automation["post_count"],
        "delay_value": automation["delay_value"],
        "delay_unit": automation["delay_unit"],
        "scan_running": automation["scan_running"],
        "next_scan_at": automation["next_scan_at"].strftime("%Y-%m-%d %H:%M:%S") if automation["next_scan_at"] else None,
        "post_interval_minutes": automation["post_interval_minutes"],
    })


def _schedule_carsforsale_daily(account, next_run):
    scheduler.add_job(_run_carsforsale_scan, trigger="interval", days=1, start_date=next_run,
                      args=[account], id=_carsforsale_daily_job_id(account), replace_existing=True,
                      max_instances=1, coalesce=True)


def _set_carsforsale_automation(account, count, delay, unit):
    now = datetime.now()
    with carsforsale_settings_lock:
        settings = _load_carsforsale_settings()
        settings[account] = {"enabled": True, "post_count": count, "delay_value": delay,
                             "delay_unit": unit, "started_at": now.isoformat(timespec="seconds"),
                             "next_scan_at": (now + CALIFORNIA_DAILY_INTERVAL).isoformat(timespec="seconds")}
        _save_carsforsale_settings(settings)
    _schedule_carsforsale_daily(account, now + CALIFORNIA_DAILY_INTERVAL)


def _post_carsforsale_listing(post_data, account, job_id):
    with scheduled_posts_lock:
        if job_id in scheduled_posts: scheduled_posts[job_id]["status"] = "waiting"
    result = _post_single_listing(post_data, account, job_id)
    mark_carsforsale_history_status(account, post_data.get("source_url"), "posted" if result else "failed")
    return result


def _run_carsforsale_scan(account):
    owns = False
    try:
        setting = _carsforsale_setting(account)
        if not setting.get("enabled"): return
        with carsforsale_scan_jobs_lock:
            if (carsforsale_scan_jobs.get(account) or {}).get("running"): return
            carsforsale_scan_jobs[account] = {"running": True, "started_at": datetime.now()}
            owns = True
        with carsforsale_settings_lock:
            settings = _load_carsforsale_settings()
            if account in settings and settings[account].get("enabled"):
                settings[account]["next_scan_at"] = (datetime.now() + CALIFORNIA_DAILY_INTERVAL).isoformat(timespec="seconds")
                _save_carsforsale_settings(settings)
        _runtime_log(f"[CarsForSaleDaily] Starting inventory scan account={account}")
        listings = collect_carsforsale_listings(account, setting.get("post_count", 4))
        delay = setting.get("delay_value", 1) * (3600 if setting.get("delay_unit") == "hours" else 60)
        base = datetime.now() + timedelta(seconds=2)
        safe = re.sub(r"[^a-zA-Z0-9_]+", "_", account or "account")
        for index, payload in enumerate(listings):
            if not _carsforsale_setting(account).get("enabled"): break
            run_at = base + timedelta(seconds=index * delay)
            job_id = f"carsforsale_{safe}_{int(base.timestamp())}_{index + 1}_{uuid4().hex[:8]}"
            scheduler.add_job(_post_carsforsale_listing, trigger="date", run_date=run_at,
                              args=[payload, account, job_id], id=job_id)
            _register_scheduled_post(job_id, account, payload, run_at)
            _runtime_log(f"[CarsForSaleDaily] Scheduled job={job_id} title={payload.get('title')}")
    except Exception:
        _runtime_log(f"[CarsForSaleDaily] Scan failed account={account}: {traceback.format_exc()}")
    finally:
        if owns:
            with carsforsale_scan_jobs_lock:
                carsforsale_scan_jobs[account] = {"running": False, "finished_at": datetime.now()}


@web_bp.route("/website_posting4/carsforsale_login", methods=["POST"])
def website_posting4_carsforsale_login():
    status = carsforsale_login_status()
    if not status.get("running"):
        threading.Thread(target=setup_carsforsale_session, kwargs={"timeout_seconds": 3600},
                         daemon=True, name="carsforsale-login-setup").start()
    session["website_posting4_message"] = "CarsForSale login window opened. Complete login within 60 minutes."
    session["website_posting4_message_type"] = "success"
    return redirect(url_for("web.website_posting4"))


@web_bp.route("/website_posting4/carsforsale_login_delete", methods=["POST"])
def website_posting4_carsforsale_login_delete():
    try:
        delete_carsforsale_session()
        session["website_posting4_message"] = "CarsForSale saved session and cookies deleted."
        session["website_posting4_message_type"] = "success"
    except Exception as exc:
        session["website_posting4_message"] = str(exc)
        session["website_posting4_message_type"] = "failed"
    return redirect(url_for("web.website_posting4"))


@web_bp.route("/website_posting4/carsforsale", methods=["POST"])
def website_posting4_carsforsale():
    account = get_active_account()
    if not account or not carsforsale_login_status().get("ready"):
        session["website_posting4_message"] = "Select a Facebook account and set up CarsForSale Login first."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    try:
        count, delay = int(request.form.get("post_count") or 0), int(request.form.get("delay_value") or 0)
    except Exception:
        count = delay = 0
    unit = str(request.form.get("delay_unit") or "").lower()
    maximum = 59 if unit == "minutes" else 24
    if not (1 <= count <= 24 and unit in {"minutes", "hours"} and 1 <= delay <= maximum):
        session["website_posting4_message"] = "Enter 1-24 posts and a valid delay."
        session["website_posting4_message_type"] = "failed"
        return redirect(url_for("web.website_posting4"))
    _set_carsforsale_automation(account, count, delay, unit)
    threading.Thread(target=_run_carsforsale_scan, args=(account,), daemon=True,
                     name=f"carsforsale-scan-{account}").start()
    return redirect(url_for("web.website_posting4"))


@web_bp.route("/website_posting4/carsforsale_stop", methods=["POST"])
def website_posting4_carsforsale_stop():
    account = get_active_account()
    with carsforsale_settings_lock:
        settings = _load_carsforsale_settings()
        current = dict(settings.get(account) or {}); current["enabled"] = False
        settings[account] = current; _save_carsforsale_settings(settings)
    try: scheduler.remove_job(_carsforsale_daily_job_id(account))
    except Exception: pass
    with scheduled_posts_lock:
        pending = [jid for jid, meta in scheduled_posts.items() if jid.startswith("carsforsale_") and meta.get("account") == account and meta.get("status") == "pending"]
    for jid in pending:
        try: scheduler.remove_job(jid)
        except Exception: pass
        with scheduled_posts_lock: scheduled_posts[jid]["status"] = "deleted"
    return redirect(url_for("web.website_posting4"))


@web_bp.route("/website_posting4/carsforsale_status", methods=["GET"])
def website_posting4_carsforsale_status():
    account = get_active_account()
    automation = _carsforsale_public_status(account)
    automation["next_scan_at"] = automation["next_scan_at"].strftime("%Y-%m-%d %H:%M:%S") if automation.get("next_scan_at") else None
    return jsonify({"ok": True, "login": carsforsale_login_status(), "automation": automation})


def _restore_carsforsale_automations():
    with carsforsale_settings_lock:
        settings = _load_carsforsale_settings()
    for account, item in settings.items():
        if not isinstance(item, dict) or not item.get("enabled"): continue
        try: next_run = datetime.fromisoformat(str(item.get("next_scan_at") or ""))
        except Exception: next_run = datetime.now() + timedelta(seconds=10)
        if next_run <= datetime.now(): next_run = datetime.now() + timedelta(seconds=10)
        _schedule_carsforsale_daily(account, next_run)


@web_bp.route("/website_posting4", methods=["POST"])
def website_posting4_post():
    return redirect(url_for("web.website_posting4"))


@web_bp.route("/website_posting4_to_manual", methods=["POST"])
def website_posting4_to_manual():
    return redirect(url_for("web.website_posting4"))


def _digits_only(text):
    s = str(text or "")
    digits = re.sub(r"[^0-9]", "", s)
    return digits


def _normalize_fuel_type(text):
    value = str(text or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    if value in {"gasoline", "gas", "petrol"}:
        return "Petrol"
    if value in {"diesel"}:
        return "Diesel"
    if not value:
        return ""
    return value.title()


@web_bp.route("/website_posting2_to_manual", methods=["POST"])
def website_posting2_to_manual():
    accounts = get_all_accounts()
    active_account = get_active_account()

    selected_images = [s.strip() for s in request.form.getlist("selected_images") if str(s or "").strip()]
    scraped_images = [s.strip() for s in request.form.getlist("scraped_images") if str(s or "").strip()]

    scraped_info = {
        "make": (request.form.get("make") or "").strip(),
        "model": (request.form.get("model") or "").strip(),
        "year": (request.form.get("year") or "").strip(),
        "body_style": (request.form.get("body_style") or "").strip(),
        "mileage": (request.form.get("mileage") or "").strip(),
        "transmission": (request.form.get("transmission") or "").strip(),
        "exterior_color": (request.form.get("exterior_color") or "").strip(),
        "interior_color": (request.form.get("interior_color") or "").strip(),
        "fuel_type": (request.form.get("fuel_type") or "").strip(),
    }

    if not selected_images:
        return render_template(
            "website_posting2.html",
            message="Please select at least one image before posting.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting2",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
            scraped_info=scraped_info,
            scraped_images=scraped_images,
        )

    prefill = {
        "make": scraped_info.get("make", ""),
        "model": scraped_info.get("model", ""),
        "year": scraped_info.get("year", ""),
        "body_style": scraped_info.get("body_style", ""),
        "mileage": _digits_only(scraped_info.get("mileage", "")),
        "transmission": scraped_info.get("transmission", ""),
        "exterior_color": scraped_info.get("exterior_color", ""),
        "interior_color": scraped_info.get("interior_color", ""),
        "fuel_type": _normalize_fuel_type(scraped_info.get("fuel_type", "")),
        "prefilled_images": selected_images[:10],
    }

    session["manual_prefill"] = prefill
    return redirect(url_for("web.auto_listing_manual"))


@web_bp.route("/website_posting_to_manual", methods=["POST"])
def website_posting_to_manual():
    accounts = get_all_accounts()
    active_account = get_active_account()

    selected_images = [s.strip() for s in request.form.getlist("selected_images") if str(s or "").strip()]
    scraped_images = [s.strip() for s in request.form.getlist("scraped_images") if str(s or "").strip()]

    scraped_info = {
        "make": (request.form.get("make") or "").strip(),
        "model": (request.form.get("model") or "").strip(),
        "year": (request.form.get("year") or "").strip(),
        "body_style": (request.form.get("body_style") or "").strip(),
        "odometer": (request.form.get("odometer") or "").strip(),
        "transmission": (request.form.get("transmission") or "").strip(),
        "exterior": (request.form.get("exterior") or "").strip(),
        "interior": (request.form.get("interior") or "").strip(),
        "fuel_type": (request.form.get("fuel_type") or "").strip(),
    }

    if not selected_images:
        return render_template(
            "website_posting.html",
            message="Please select at least one image before posting.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="website_posting",
            auto_listing_running=_is_auto_listing_running_for_account(active_account),
            scraped_info=scraped_info,
            scraped_images=scraped_images,
        )

    prefill = {
        "make": scraped_info.get("make", ""),
        "model": scraped_info.get("model", ""),
        "year": scraped_info.get("year", ""),
        "body_style": scraped_info.get("body_style", ""),
        "mileage": _digits_only(scraped_info.get("odometer", "")),
        "transmission": scraped_info.get("transmission", ""),
        "exterior_color": scraped_info.get("exterior", ""),
        "interior_color": scraped_info.get("interior", ""),
        "fuel_type": _normalize_fuel_type(scraped_info.get("fuel_type", "")),
        "prefilled_images": selected_images[:10],
    }

    session["manual_prefill"] = prefill
    return redirect(url_for("web.auto_listing_manual"))

# -------------------------
# Post Scheduler
# -------------------------

@web_bp.route("/post_schedular", methods=["GET"])
def post_schedular():
    accounts = get_all_accounts()
    active_account = get_active_account()
    schedule_data = _collect_account_schedule(active_account)

    return render_template(
        "post_schedular.html",
        accounts=accounts,
        active_account=active_account,
        active_page="schedule",
        scheduled_items=schedule_data["items"],
        pending_count=schedule_data["pending_count"],
        posted_count=schedule_data["posted_count"],
        california_automation=_california_public_status(active_account),
        carsforsale_automation=_carsforsale_public_status(active_account),
    )

@web_bp.route("/post_schedular_data", methods=["GET"])
def post_schedular_data():
    active_account = get_active_account()
    schedule_data = _collect_account_schedule(active_account)
    california_status = _california_public_status(active_account)

    items_payload = []
    for item in schedule_data["items"]:
        items_payload.append({
            "job_id": item["job_id"],
            "title": item["title"],
            "make": item.get("make", ""),
            "platform": item.get("platform", "facebook"),
            "source": item.get("source", ""),
            "status": item["status"],
            "run_at": item["run_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_seconds": item["remaining_seconds"]
        })

    return jsonify({
        "ok": True,
        "account": active_account,
        "pending_count": schedule_data["pending_count"],
        "posted_count": schedule_data["posted_count"],
        "california_automation": {
            "enabled": california_status["enabled"],
            "post_count": california_status["post_count"],
            "delay_value": california_status["delay_value"],
            "delay_unit": california_status["delay_unit"],
            "scan_running": california_status["scan_running"],
            "next_scan_at": california_status["next_scan_at"].strftime("%Y-%m-%d %H:%M:%S") if california_status["next_scan_at"] else None,
            "post_interval_minutes": california_status["post_interval_minutes"],
        },
        "carsforsale_automation": {
            "enabled": _carsforsale_public_status(active_account).get("enabled", False),
            "scan_running": _carsforsale_public_status(active_account).get("scan_running", False),
        },
        "items": items_payload,
    })

@web_bp.route("/post_schedular_delete", methods=["POST"])
def post_schedular_delete():
    active_account = get_active_account()

    data = request.get_json(silent=True) or {}
    job_id = (data.get("job_id") or request.form.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "message": "Missing job id"}), 400

    with scheduled_posts_lock:
        job_meta = scheduled_posts.get(job_id)
    is_instagram_job = bool(job_meta and job_meta.get("platform") == "instagram")
    if not is_instagram_job and not active_account:
        return jsonify({"ok": False, "message": "No active account selected"}), 400
    if not job_meta or (not is_instagram_job and job_meta.get("account") != active_account):
        return jsonify({"ok": False, "message": "Job not found for this account"}), 404

    if job_meta.get("status") != "pending":
        return jsonify({"ok": False, "message": "Only pending jobs can be deleted"}), 400

    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    with scheduled_posts_lock:
        if job_id in scheduled_posts:
            scheduled_posts[job_id]["status"] = "deleted"
            scheduled_posts[job_id]["deleted_at"] = datetime.now()
    _log_event(
        event_type="scheduler",
        account=(INSTAGRAM_SCHEDULER_ACCOUNT if is_instagram_job else active_account),
        status="success",
        reference=job_meta.get("title", "Untitled"),
        details=f"Deleted pending job {job_id}"
    )

    return jsonify({"ok": True})

# -------------------------
# Delete listings
# -------------------------

def _delete_import_snapshot(account):
    with listing_import_jobs_lock:
        return dict(listing_import_jobs.get(account) or {"running": False}) if account else {"running": False}


def _listing_click_snapshot(account):
    with listing_click_jobs_lock:
        return dict(listing_click_jobs.get(account) or {"running": False}) if account else {"running": False}


def _facebook_delete_snapshot(account):
    with facebook_delete_jobs_lock:
        return dict(facebook_delete_jobs.get(account) or {"running": False}) if account else {"running": False}


def _render_delete_listings_page(message=None, message_type=None):
    accounts = get_all_accounts()
    active_account = get_active_account()
    stored = (load_listings(active_account) if active_account else None) or {"listings": [], "updated_at": None}
    listings = [item for item in (stored.get("listings") or []) if isinstance(item, dict)]
    import_status = _delete_import_snapshot(active_account)
    return render_template(
        "delete_listings.html",
        message=message,
        message_type=message_type,
        accounts=accounts,
        active_account=active_account,
        active_page="delete",
        listings=listings,
        listings_updated_at=stored.get("updated_at"),
        import_status=import_status,
        click_refresh_status=_listing_click_snapshot(active_account),
        facebook_delete_status=_facebook_delete_snapshot(active_account),
        scheduled_repost_ids=_active_repost_listing_ids(active_account),
    )


def _start_listing_import_job(account, automatic=False):
    if not account:
        return False, "No active account selected."
    if _listing_click_snapshot(account).get("running"):
        return False, "Wait for the click refresh to finish."
    if _facebook_delete_snapshot(account).get("running"):
        return False, "Wait for Facebook deletion to finish."
    if _active_repost_listing_ids(account):
        return False, "Wait for scheduled reposting to finish."
    with listing_import_jobs_lock:
        current = listing_import_jobs.get(account) or {}
        if current.get("running"):
            return True, "Import listings started."
        stop_event = threading.Event()
        listing_import_cancel_events[account] = stop_event
        listing_import_jobs[account] = {
            "running": True,
            "started_at": time.time(),
            "finished_at": None,
            "new_count": 0,
            "total_count": len(load_listings(account).get("listings") or []),
            "message": "Starting automatic listing refresh..." if automatic else "Starting listing import...",
            "error": None,
            "stop_requested": False,
        }

    def _progress(event):
        with listing_import_jobs_lock:
            job = listing_import_jobs.get(account) or {}
            job["new_count"] = int(event.get("new_count") or 0)
            job["total_count"] = int(event.get("total_count") or 0)
            job["message"] = event.get("message") or job.get("message")
            listing_import_jobs[account] = job

    def _runner():
        try:
            # A re-import is a fresh snapshot. This drops listings that no
            # longer appear on Facebook instead of merging them forever.
            import_listings_from_facebook(
                account,
                stop_event=stop_event,
                progress_callback=_progress,
                replace_existing=True,
            )
            if stop_event.is_set():
                raise ListingImportCancelled()
            with listing_import_jobs_lock:
                job = listing_import_jobs.get(account) or {}
                job.update({
                    "running": False,
                    "finished_at": time.time(),
                    "total_count": len(load_listings(account).get("listings") or []),
                    "message": "Automatic listing refresh complete." if automatic else "Listing import complete.",
                })
                listing_import_jobs[account] = job
        except ListingImportCancelled:
            with listing_import_jobs_lock:
                job = listing_import_jobs.get(account) or {}
                job.update({
                    "running": False,
                    "finished_at": time.time(),
                    "total_count": len(load_listings(account).get("listings") or []),
                    "message": "Listing import stopped.",
                })
                listing_import_jobs[account] = job
        except Exception as exc:
            with listing_import_jobs_lock:
                job = listing_import_jobs.get(account) or {}
                if stop_event.is_set():
                    job.update({"running": False, "finished_at": time.time(), "error": None, "message": "Listing import stopped."})
                else:
                    job.update({"running": False, "finished_at": time.time(), "error": str(exc), "message": "Listing import failed."})
                listing_import_jobs[account] = job
            if not stop_event.is_set():
                _runtime_log(f"[DeleteListings] Import failed account={account}: {exc}")
        finally:
            with listing_import_jobs_lock:
                listing_import_cancel_events.pop(account, None)

    threading.Thread(target=_runner, daemon=True, name=f"listing-import-{account}").start()
    return True, "Automatic listing refresh started." if automatic else "Listing import started."


def _listing_snapshot_is_due(account, now=None):
    """Return True once an existing imported snapshot is at least 24 hours old."""
    updated_at = load_listings(account).get("updated_at")
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (now or datetime.now(timezone.utc)) - updated >= LISTING_AUTO_REFRESH_INTERVAL


def _refresh_due_listing_snapshots():
    """Start non-overlapping refreshes for imported account snapshots that are due."""
    with listing_import_jobs_lock:
        if any((job or {}).get("running") for job in listing_import_jobs.values()):
            return
    now = datetime.now(timezone.utc)
    for account in get_all_accounts():
        if not _listing_snapshot_is_due(account, now=now):
            continue
        ok, message = _start_listing_import_job(account, automatic=True)
        if ok:
            _runtime_log(f"[DeleteListings] 24-hour refresh started account={account}")
            # Avoid launching several Selenium browsers at once. Any other due
            # account will be picked up by the next scheduler check.
            return
        elif "already" not in message.lower():
            _runtime_log(f"[DeleteListings] 24-hour refresh deferred account={account}: {message}")


def _start_listing_click_refresh_job(account):
    _runtime_log(f"[RefreshClicks] request received account={account or '-'}")
    if not account:
        return False, "No active account selected."
    total = len(load_listings(account).get("listings") or [])
    if not total:
        return False, "Import listings before refreshing clicks."
    if _delete_import_snapshot(account).get("running"):
        return False, "Wait for the listing import to finish."
    if _facebook_delete_snapshot(account).get("running"):
        return False, "Wait for Facebook deletion to finish."
    with listing_click_jobs_lock:
        current = listing_click_jobs.get(account) or {}
        if current.get("running"):
            return True, "Click refresh is already running."
        listing_click_jobs[account] = {
            "running": True,
            "started_at": time.time(),
            "finished_at": None,
            "confirmed_count": 0,
            "total_count": total,
            "message": f"Refreshing clicks... 0/{total}",
            "error": None,
        }

    def _progress(event):
        with listing_click_jobs_lock:
            job = listing_click_jobs.get(account) or {}
            job["confirmed_count"] = int(event.get("confirmed_count") or 0)
            job["total_count"] = int(event.get("total_count") or total)
            job["message"] = event.get("message") or job.get("message")
            listing_click_jobs[account] = job

    def _runner():
        try:
            refresh_listing_clicks_from_facebook(account, progress_callback=_progress)
            with listing_click_jobs_lock:
                job = listing_click_jobs.get(account) or {}
                confirmed = int(job.get("confirmed_count") or 0)
                job.update({
                    "running": False,
                    "finished_at": time.time(),
                    "message": f"Clicks refreshed for {confirmed} listing(s).",
                })
                listing_click_jobs[account] = job
        except Exception as exc:
            with listing_click_jobs_lock:
                job = listing_click_jobs.get(account) or {}
                job.update({
                    "running": False,
                    "finished_at": time.time(),
                    "error": str(exc),
                    "message": "Click refresh failed.",
                })
                listing_click_jobs[account] = job
            _runtime_log(f"[DeleteListings] Click refresh failed account={account}: {exc}")

    threading.Thread(target=_runner, daemon=True, name=f"listing-click-refresh-{account}").start()
    _runtime_log(f"[RefreshClicks] background job started account={account} total={total}")
    return True, "Click refresh started."


@web_bp.route("/delete_listings", methods=["GET", "POST"])
def delete_listings_page():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        if action == "import":
            ok, msg = _start_listing_import_job(get_active_account())
            return _render_delete_listings_page(msg, "success" if ok else "failed")
        if action == "delete":
            return _render_delete_listings_page(
                "Delete action is reserved for the next step; listings were not removed.",
                "success",
            )
    return _render_delete_listings_page(
        request.args.get("message"),
        request.args.get("message_type"),
    )


@web_bp.route("/delete_listings/import", methods=["GET", "POST"])
def delete_listings_import():
    # A normal form POST can leave the browser address bar on this endpoint;
    # account switching then follows that referrer with GET. Keep navigation
    # safe while the background import/delete workers continue running.
    if request.method == "GET":
        return redirect(url_for("web.delete_listings_page"))
    ok, msg = _start_listing_import_job(get_active_account())
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)
    return _render_delete_listings_page(msg, "success" if ok else "failed")


@web_bp.route("/delete_listings/import/stop", methods=["POST"])
def delete_listings_import_stop():
    account = get_active_account()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    with listing_import_jobs_lock:
        job = listing_import_jobs.get(account) or {}
        if not job.get("running"):
            message = "No listing import is running."
            if is_ajax:
                return jsonify({"ok": False, "message": message}), 409
            return _render_delete_listings_page(message, "failed")
        job["stop_requested"] = True
        job["message"] = "Stop requested..."
        listing_import_jobs[account] = job
        event = listing_import_cancel_events.get(account)
        if event:
            event.set()
    # Stop the Selenium session immediately; the worker's finally block also
    # remains responsible for cleanup if it is between operations.
    stop_listing_import_driver(account)
    message = "Stop requested."
    if is_ajax:
        return jsonify({"ok": True, "message": message})
    return _render_delete_listings_page(message, "success")


@web_bp.route("/delete_listings/clicks/refresh", methods=["POST"])
def delete_listings_clicks_refresh():
    ok, message = _start_listing_click_refresh_job(get_active_account())
    return jsonify({
        "ok": ok,
        "message": message,
        "click_refresh_status": _listing_click_snapshot(get_active_account()),
    }), (200 if ok else 409)


@web_bp.route("/delete_listings/data", methods=["GET"])
def delete_listings_data():
    account = get_active_account()
    stored = load_listings(account) if account else {"listings": [], "updated_at": None}
    return jsonify({
        "ok": True,
        "account": account,
        "listings": stored.get("listings") or [],
        "updated_at": stored.get("updated_at"),
        "import_status": _delete_import_snapshot(account),
        "click_refresh_status": _listing_click_snapshot(account),
        "facebook_delete_status": _facebook_delete_snapshot(account),
        "scheduled_repost_ids": sorted(_active_repost_listing_ids(account)),
    })


@web_bp.route("/delete_listings/repost", methods=["POST"])
def delete_listings_repost():
    account = get_active_account()
    if not account:
        return jsonify({"ok": False, "message": "No active account selected."}), 400
    data = request.get_json(silent=True) or {}
    listing_id = str(data.get("listing_id") or "").strip()
    delay_unit = str(data.get("delay_unit") or "").strip().lower()
    try:
        delay_value = int(data.get("delay_value"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Enter a whole-number delay."}), 400
    if delay_unit not in {"minutes", "hours", "days"}:
        return jsonify({"ok": False, "message": "Choose minutes, hours, or days."}), 400
    if delay_unit == "minutes" and not (1 <= delay_value <= 59):
        return jsonify({"ok": False, "message": "Minutes must be between 1 and 59."}), 400
    if delay_unit == "hours" and not (1 <= delay_value <= 24):
        return jsonify({"ok": False, "message": "Hours must be between 1 and 24."}), 400
    if delay_unit == "days" and not (1 <= delay_value <= 365):
        return jsonify({"ok": False, "message": "Days must be between 1 and 365."}), 400

    listings = load_listings(account).get("listings") or []
    listing = next(
        (dict(item) for item in listings if str(item.get("id") or "").strip() == listing_id),
        None,
    )
    if not listing:
        return jsonify({"ok": False, "message": "Listing was not found in stored listings."}), 404

    delay_seconds = delay_value * {"minutes": 60, "hours": 3600, "days": 86400}[delay_unit]
    run_at = datetime.now() + timedelta(seconds=delay_seconds)
    safe_account = re.sub(r"[^a-zA-Z0-9_]+", "_", account or "account")
    job_id = f"repost_manage_{safe_account}_{int(time.time())}_{uuid4().hex[:8]}"
    title = " ".join(
        str(listing.get(field) or "").strip()
        for field in ("year", "make", "model")
        if str(listing.get(field) or "").strip()
    ).strip() or "Listing"

    with repost_jobs_lock:
        active_jobs = [
            meta for meta in repost_jobs.values()
            if meta.get("account") == account
            and meta.get("status") in {"pending", "reading", "posting", "deleting"}
        ]
        if any(str(meta.get("listing_id") or "") == listing_id for meta in active_jobs):
            return jsonify({"ok": False, "message": "This listing is already scheduled for reposting."}), 409
        previous_delay = max((int(meta.get("delay_seconds") or 0) for meta in active_jobs), default=0)
        if delay_seconds <= previous_delay:
            previous_label = (
                f"{previous_delay // 86400} day(s)" if previous_delay % 86400 == 0
                else f"{previous_delay // 3600} hour(s)" if previous_delay % 3600 == 0
                else f"{previous_delay // 60} minute(s)"
            )
            return jsonify({
                "ok": False,
                "message": f"Choose a delay greater than the previous scheduled delay ({previous_label}).",
            }), 409
        repost_jobs[job_id] = {
            "job_id": job_id,
            "account": account,
            "listing_id": listing_id,
            "listing": listing,
            "title": title,
            "run_at": run_at,
            "status": "pending",
            "delay_value": delay_value,
            "delay_unit": delay_unit,
            "delay_seconds": delay_seconds,
            "created_at": datetime.now(),
            "started_at": None,
            "finished_at": None,
        }
    try:
        scheduler.add_job(
            _run_reposting_read_job,
            trigger="date",
            run_date=run_at,
            args=[listing, account, job_id],
            id=job_id,
            replace_existing=False,
        )
    except Exception as exc:
        with repost_jobs_lock:
            repost_jobs.pop(job_id, None)
        return jsonify({"ok": False, "message": f"Could not schedule repost: {exc}"}), 500

    _runtime_log(
        f"[Reposting] Scheduled Manage Listings job={job_id} account={account} "
        f"listing={listing_id} run_at={run_at.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return jsonify({
        "ok": True,
        "message": f"{title} scheduled for reposting.",
        "job_id": job_id,
        "run_at": run_at.strftime("%Y-%m-%d %H:%M:%S"),
    })


@web_bp.route("/delete_listings/delete", methods=["POST"])
def delete_listings_delete():
    account = get_active_account()
    if not account:
        return jsonify({"ok": False, "message": "No active account selected."}), 400
    if _listing_click_snapshot(account).get("running"):
        return jsonify({"ok": False, "message": "Wait for the click refresh to finish."}), 409
    data = request.get_json(silent=True) or {}
    selected_ids = {str(value or "").strip() for value in (data.get("ids") or []) if str(value or "").strip()}
    if not selected_ids:
        return jsonify({"ok": False, "message": "Select at least one listing."}), 400
    protected_ids = _active_repost_listing_ids(account)
    blocked_ids = selected_ids & protected_ids
    if blocked_ids:
        return jsonify({
            "ok": False,
            "message": "A listing scheduled for reposting cannot be deleted.",
        }), 409
    with facebook_delete_jobs_lock:
        current = facebook_delete_jobs.get(account) or {}
        if current.get("running"):
            return jsonify({"ok": False, "message": "Facebook listing operation is already running."}), 409
        stored = load_listings(account).get("listings") or []
        selected = [item for item in stored if str(item.get("id") or "").strip() in selected_ids]
        if not selected:
            return jsonify({"ok": False, "message": "Selected listings were not found in storage."}), 404
        stop_event = threading.Event()
        facebook_delete_cancel_events[account] = stop_event
        facebook_delete_jobs[account] = {
            "running": True,
            "started_at": time.time(),
            "finished_at": None,
            "message": f"Opening {len(selected)} selected listing(s)...",
            "clicked": 0,
            "deleted": 0,
            "total": len(selected),
            "error": None,
            "stop_requested": False,
        }

    def _progress(event):
        with facebook_delete_jobs_lock:
            job = facebook_delete_jobs.get(account) or {}
            job.update({
                "clicked": int(event.get("clicked") or 0),
                "deleted": int(event.get("deleted") or 0),
                "message": event.get("message") or job.get("message"),
            })
            facebook_delete_jobs[account] = job

    def _runner():
        try:
            result = delete_selected_listings_from_facebook(
                account,
                selected,
                stop_event=stop_event,
                progress_callback=_progress,
            )
            deleted_ids = {str(value).strip() for value in (result.get("deleted_ids") or []) if str(value).strip()}
            if deleted_ids:
                stored_after = load_listings(account)
                listings_after = stored_after.get("listings") or []
                remaining_after = [
                    item for item in listings_after
                    if str(item.get("id") or "").strip() not in deleted_ids
                ]
                save_listings(account, remaining_after)
            with facebook_delete_jobs_lock:
                job = facebook_delete_jobs.get(account) or {}
                job.update({
                    "running": False,
                    "finished_at": time.time(),
                    "clicked": int(result.get("clicked") or 0),
                    "deleted": int(result.get("deleted") or 0),
                    "not_found": int(result.get("not_found") or 0),
                    "message": (
                        f"Facebook deletion stopped: {int(result.get('deleted') or 0)} listing(s) deleted."
                        if result.get("stopped")
                        else f"Facebook deletion complete: {int(result.get('deleted') or 0)} listing(s) deleted."
                    ),
                })
                facebook_delete_jobs[account] = job
        except Exception as exc:
            with facebook_delete_jobs_lock:
                job = facebook_delete_jobs.get(account) or {}
                if stop_event.is_set():
                    job.update({"running": False, "finished_at": time.time(), "error": None, "message": "Facebook deletion stopped."})
                else:
                    job.update({"running": False, "finished_at": time.time(), "error": str(exc), "message": "Facebook listing operation failed."})
                facebook_delete_jobs[account] = job
            if not stop_event.is_set():
                _runtime_log(f"[DeleteListings] Facebook operation failed account={account}: {exc}")
        finally:
            with facebook_delete_jobs_lock:
                facebook_delete_cancel_events.pop(account, None)

    threading.Thread(target=_runner, daemon=True, name=f"facebook-listing-delete-{account}").start()
    return jsonify({"ok": True, "message": f"Facebook listing operation started for {len(selected)} listing(s)."})


@web_bp.route("/delete_listings/delete/stop", methods=["POST"])
def delete_listings_delete_stop():
    account = get_active_account()
    if not account:
        return jsonify({"ok": False, "message": "No active account selected."}), 400
    with facebook_delete_jobs_lock:
        job = facebook_delete_jobs.get(account) or {}
        if not job.get("running"):
            return jsonify({"ok": False, "message": "No Facebook deletion is running."}), 409
        job["stop_requested"] = True
        job["message"] = "Stopping Facebook deletion..."
        facebook_delete_jobs[account] = job
        stop_event = facebook_delete_cancel_events.get(account)
        if stop_event:
            stop_event.set()
    stop_listing_delete_driver(account)
    return jsonify({"ok": True, "message": "Stop requested. Facebook deletion is stopping."})


@web_bp.route("/delete_listings/storage/clear", methods=["POST"])
def delete_listings_storage_clear():
    """Clear every locally imported listing for the active account."""
    account = get_active_account()
    if not account:
        return jsonify({"ok": False, "message": "No active account selected."}), 400
    with listing_import_jobs_lock:
        if (listing_import_jobs.get(account) or {}).get("running"):
            return jsonify({"ok": False, "message": "Stop the listing import before clearing stored data."}), 409
    with facebook_delete_jobs_lock:
        if (facebook_delete_jobs.get(account) or {}).get("running"):
            return jsonify({"ok": False, "message": "Wait for Facebook deletion to finish before clearing stored data."}), 409
    if _active_repost_listing_ids(account):
        return jsonify({"ok": False, "message": "Wait for scheduled reposting to finish before clearing stored data."}), 409
    removed = len(load_listings(account).get("listings") or [])
    save_listings(account, [])
    return jsonify({
        "ok": True,
        "message": f"Cleared all stored data for {removed} listing(s). Facebook listings were not changed.",
    })


# -------------------------
# Auto Reply
# -------------------------
  

@web_bp.route("/auto_reply", methods=["GET", "POST"])
def auto_reply():
    accounts = get_all_accounts()
    active_account = get_active_account()
    manager = auto_reply_managers.get(active_account) if active_account else None
    auto_reply_running = bool(manager and manager.active)

    # GET request -> show form
    if request.method == "GET":
        return render_template(
            "auto_reply.html",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_reply",
            auto_reply_running=auto_reply_running,
        )

    # POST request -> start auto-reply for active account
    if not active_account:
        return render_template(
            "auto_reply.html",
            message="No active account selected",
            message_type="failed",
            accounts=accounts,
            active_account=None,
            active_page="auto_reply",
            auto_reply_running=False,
        )

    # Get form values (reply_message and delay are optional)
    submitted_reply_message = (request.form.get("reply_message") or "").strip()
    submitted_delay = (request.form.get("limit") or "").strip()
    messenger_pin = (request.form.get("messenger_pin") or "").strip()

    # Create AutoReplyManager for this active account if not exists
    if active_account not in auto_reply_managers:
        auto_reply_managers[active_account] = AutoReplyManager(active_account)

    manager = auto_reply_managers[active_account]
    manager.set_crash_callback(_auto_reply_crash_logger)
    auto_reply_running = bool(manager.active)

    if auto_reply_running:
        return render_template(
            "auto_reply.html",
            message="Auto-reply is already running for this account.",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_reply",
            auto_reply_running=True,
        )

    # Keep previous values when optional fields are not submitted.
    reply_message = submitted_reply_message if submitted_reply_message else (manager.reply_message or "")
    try:
        delay = int(submitted_delay) if submitted_delay else int(manager.delay or 5)
    except (TypeError, ValueError):
        delay = 5

    # Start auto-reply for this account.
    try:
        started = manager.start(
            reply_message=reply_message,
            delay=delay,
            pin=messenger_pin,
            get_messages_func=get_new_messages
        )
        if not started:
            _log_event(
                event_type="auto_reply",
                account=active_account,
                status="failed",
                reference="Auto-reply already running",
                details=f"Delay {delay}s"
            )
            return render_template(
                "auto_reply.html",
                message="Auto-reply is already running for this account",
                message_type="failed",
                accounts=accounts,
                active_account=active_account,
                active_page="auto_reply",
                auto_reply_running=False,
            )
        _log_event(
            event_type="auto_reply",
            account=active_account,
            status="success",
            reference="Started",
            details=f"Delay {delay}s"
        )
        save_auto_reply_state(
            account=active_account,
            reply_message=reply_message,
            delay=delay,
            pin=messenger_pin,
        )
    except Exception as e:
        tb_text = traceback.format_exc()
        _log_event(
            event_type="auto_reply",
            account=active_account,
            status="failed",
            reference="Auto-reply start failed",
            details=str(e),
            tb_text=tb_text
        )
        return render_template(
            "auto_reply.html",
            message=f"Failed to start auto-reply: {e}",
            message_type="failed",
            accounts=accounts,
            active_account=active_account,
            active_page="auto_reply",
            auto_reply_running=False,
        )

    return redirect(url_for("web.auto_reply_status"))

@web_bp.route("/auto_reply_status", methods=["GET", "POST"])
def auto_reply_status():
    """
    Status page for auto-reply of the selected account.
    Account is determined from active_account in session.
    """
    accounts = get_all_accounts()
    selected_account = get_active_account()  # from session

    if not selected_account:
        return render_template(
            "auto_reply_status.html",
            accounts_status=[],
            accounts=accounts,
            active_account=None,
            active_page="auto_reply",
            message="No active account selected",
            message_type="failed"
        )

    # Get or create manager for the selected account
    if selected_account not in auto_reply_managers:
        auto_reply_managers[selected_account] = AutoReplyManager(selected_account)

    manager = auto_reply_managers[selected_account]

    account_status = {
        "account": selected_account,
        "status": "running" if manager.active else "stopped",
        "reply_message": manager.reply_message if manager.active else "-",
        "delay": manager.delay if manager.active else "-",
        "replied_items": manager.get_replied_items_snapshot() if manager.active else []
    }

    return render_template(
        "auto_reply_status.html",
        accounts_status=[account_status], 
        accounts=accounts,                 
        active_account=selected_account,   
        active_page="auto_reply"
    )

@web_bp.route("/auto_reply_status_data", methods=["GET"])
def auto_reply_status_data():
    selected_account = (request.args.get("account") or "").strip()
    if not selected_account:
        return jsonify({
            "ok": False,
            "status": "stopped",
            "account": None,
            "replied_items": []
        })

    # Strict account isolation: only return data for the requested account.
    if selected_account not in set(get_all_accounts()):
        return jsonify({
            "ok": False,
            "status": "stopped",
            "account": selected_account,
            "replied_items": []
        })

    if selected_account not in auto_reply_managers:
        auto_reply_managers[selected_account] = AutoReplyManager(selected_account)
    manager = auto_reply_managers[selected_account]

    return jsonify({
        "ok": True,
        "status": "running" if manager.active else "stopped",
        "account": selected_account,
        "delay": manager.delay if manager.active else None,
        "reply_message": manager.reply_message if manager.active else "",
        "replied_items": manager.get_replied_items_snapshot() if manager.active else []
    })
@web_bp.route("/auto_reply_stop", methods=["POST"])
def auto_reply_stop():
    selected_account = (request.form.get("account") or "").strip() or get_active_account()
    if selected_account and selected_account not in set(get_all_accounts()):
        selected_account = None
    if selected_account and selected_account in auto_reply_managers:
        auto_reply_managers[selected_account].stop()
    clear_auto_reply_state()

    _log_event(
        event_type="auto_reply",
        account=selected_account,
        status="success",
        reference="Stopped",
        details=""
    )
    return redirect(url_for("web.auto_reply_status"))

@web_bp.route("/auto_reply_feed_delete", methods=["POST"])
def auto_reply_feed_delete():
    selected_account = (request.form.get("account") or "").strip()
    thread_id = (request.form.get("thread_id") or "").strip()
    replied_at = (request.form.get("replied_at") or "").strip() or None

    if not selected_account or not thread_id:
        return jsonify({"ok": False, "message": "Missing account or thread id"}), 400

    if selected_account not in auto_reply_managers:
        auto_reply_managers[selected_account] = AutoReplyManager(selected_account)
    manager = auto_reply_managers[selected_account]

    removed = manager.delete_replied_item(thread_id=thread_id, replied_at=replied_at)
    return jsonify({"ok": bool(removed)})

@web_bp.route("/auto_reply_feed_clear_all", methods=["POST"])
def auto_reply_feed_clear_all():
    selected_account = (request.form.get("account") or "").strip()

    if not selected_account:
        return jsonify({"ok": False, "message": "Missing account"}), 400

    if selected_account not in auto_reply_managers:
        auto_reply_managers[selected_account] = AutoReplyManager(selected_account)
    manager = auto_reply_managers[selected_account]

    cleared = manager.clear_replied_items()
    return jsonify({"ok": True, "cleared": bool(cleared)})

# -------------------------
# Activity Log
# -------------------------

@web_bp.route("/activity_log", methods=["GET"])
def activity_log_page():
    accounts = get_all_accounts()
    active_account = get_active_account()
    global activity_log
    with activity_log_lock:
        activity_log = _load_activity_log()
        log_items = list(activity_log)
    return render_template(
        "activity_log.html",
        accounts=accounts,
        active_account=active_account,
        active_page="activity_log",
        log_items=log_items
    )

@web_bp.route("/activity_log_clear", methods=["POST"])
def activity_log_clear():
    global activity_log
    mode = (request.form.get("mode") or "failed").strip().lower()
    with activity_log_lock:
        activity_log = _load_activity_log()
        if mode == "all":
            activity_log = []
        else:
            activity_log = [item for item in activity_log if item.get("status") != "failed"]
        _save_activity_log(activity_log)
    return redirect(url_for("web.activity_log_page"))


_restore_auto_reply_if_enabled()
_restore_california_automations()
_restore_carsforsale_automations()
scheduler.add_job(
    _refresh_due_listing_snapshots,
    trigger="interval",
    minutes=5,
    id=LISTING_AUTO_REFRESH_CHECK_JOB_ID,
    replace_existing=True,
    next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    max_instances=1,
    coalesce=True,
)
