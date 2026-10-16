from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_from_directory
import copy
import sys
from automation.fb_login import MAX_ACCOUNTS
from automation.cdp_login import cleanup_expired_cdp_logins, cleanup_orphaned_cdp_accounts, close_cdp_browser, delete_cdp_account, get_cdp_account, list_cdp_accounts, start_cdp_login
from automation.remote_login import (
    cleanup_finished_remote_login_sessions,
    create_remote_login_session,
    get_remote_login_session,
)
from automation.auto_listing_excel_poster import read_excel_flexible, post_to_fb_marketplace
from automation.reposting import _normalize_price, _extract_listing_id
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
import re
import tempfile
import pandas as pd
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
import json
import threading
from contextlib import contextmanager
from automation.task_control import cancellable
from urllib.parse import urlparse
from uuid import uuid4
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from automation.runtime_paths import activity_log_path, runtime_log_path, sessions_dir, screenshots_dir, data_dir, project_root
import time
from automation.browser_settings import set_run_in_background_enabled
from automation.listing_store import save_preferences, update_fields as update_listing_fields, remove as remove_listing_backups, mark_import_complete
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
web_bp = Blueprint("web", __name__)

DESKTOP_MODE = os.getenv("FB_AUTOMATION_DESKTOP") == "1"
if DESKTOP_MODE:
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    scheduler = BackgroundScheduler(daemon=True, jobstores={'default': SQLAlchemyJobStore(url='sqlite:///' + str(data_dir() / 'schedules.db'))}, job_defaults={'misfire_grace_time': None, 'coalesce': True})
else:
    scheduler = BackgroundScheduler(daemon=True)


scheduled_posts = {}
cross_post_queue_lock = threading.Lock()
PORTAL_AUTH_PATH = str(data_dir() / "portal_auth.json")
portal_auth_lock = threading.RLock()
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
    return sorted(set([
        filename_to_email(f)
        for f in os.listdir(SESSION_DIR)
        if f.endswith(".pkl")
    ] + [account for account, meta in list_cdp_accounts().items()
         if meta.get('status') == 'ready' or meta.get('relogin')]))

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


desktop_update_pending = False

@web_bp.before_request
def desktop_update_guard():
    if desktop_update_pending and request.method == 'POST' and not request.path.startswith('/settings/updates'):
        return jsonify(ok=False, message='The app is preparing an update. Please wait.'), 503

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
            session.permanent = DESKTOP_MODE
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
            session.permanent = DESKTOP_MODE
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
            "transmission", "location", "clean_title",
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
    with scheduled_posts_lock:
        for meta in scheduled_posts.values():
            if (meta.get('source') == 'Cross posting' and meta.get('source_account') == account
                    and meta.get('status') in {'pending', 'waiting', 'posting'}):
                active.add(str(meta['source_listing_id']))
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


@cancellable
def _run_saved_listing_repost(listing, account, job_id=None):
    if job_id:
        with repost_jobs_lock:
            meta = repost_jobs.get(job_id)
            if not meta or meta.get("status") != "pending":
                return
    run_lock = _get_repost_run_lock(account)
    with run_lock:
        account_post_lock = _get_post_lock(account)
        if job_id:
            with repost_jobs_lock:
                if job_id in repost_jobs:
                    repost_jobs[job_id]["status"] = "reading"
                    repost_jobs[job_id]["started_at"] = datetime.now()
        if DESKTOP_MODE:
            from automation.desktop_state import checkpoint
            checkpoint()
        update_listing_fields(account, str(listing.get('id') or ''), {'repost_status': 'reading'})

        try:
            with account_post_lock:
                # Scheduled jobs use the saved source and frozen preferences.
                if not listing.get('details'):
                    raise ValueError('Saved listing details are missing. Import this listing first.')
                if not listing.get('source_deleted') and not _extract_listing_id(listing.get('edit_url') or listing.get('url') or ''):
                    raise ValueError('The saved Facebook listing ID is missing. Import this listing again before reposting.')
                post = dict(listing['details'])
                preferences = listing.get('repost_preferences') or {}
                post.update(preferences.get('fields') or {})
                edited_fields = preferences.get('fields') or {}
                if 'title' not in edited_fields and any(field in edited_fields for field in ('year', 'make', 'model')):
                    post['title'] = ' '.join(str(post.get(field) or '').strip() for field in ('year', 'make', 'model')).strip()
                photos = listing.get('downloaded_images') or []
                order = preferences.get('image_order') or [photo['name'] for photo in photos]
                by_name = {photo['name']: photo['path'] for photo in photos}
                post['image_urls'] = [by_name[name] for name in order if name in by_name]
                if listing.get('image_errors'):
                    raise ValueError('Some imported photos failed to download. Re-import before reposting.')
                if not post['image_urls'] or any(not os.path.isfile(path) for path in post['image_urls']):
                    raise ValueError('Imported photos are missing. Import this listing again before reposting.')
                payloads = _build_repost_payloads({'posts': [post]})
                if not payloads:
                    raise ValueError('Saved listing data is incomplete.')

                # Validate every payload before removing the Facebook source.
                for payload in payloads:
                    title = str(payload.get("title") or "").strip()
                    price = str(payload.get("price") or "").strip()
                    has_image = any(
                        str(payload.get(f"image_path{index}") or "").strip()
                        for index in range(1, 11)
                    )
                    if not title or not price or not has_image:
                        raise Exception(
                            "Saved listing data is incomplete; source was not deleted "
                            "(title, price, and at least one image are required)"
                        )

                # Delete only the verified original, keeping its stored backup.
                # Archived backups have no remaining source to delete.
                if not listing.get('source_deleted'):
                    if job_id:
                        with repost_jobs_lock:
                            if job_id in repost_jobs:
                                repost_jobs[job_id]["status"] = "deleting"
                    update_listing_fields(account, str(listing['id']), {'repost_status': 'deleting'})
                    source = dict(listing)
                    source['_source_listing_id'] = _extract_listing_id(listing.get('edit_url') or listing.get('url') or '')
                    result = delete_selected_listings_from_facebook(account, [source])
                    if int((result or {}).get('deleted') or 0) != 1:
                        raise RuntimeError('The original Facebook listing could not be deleted. Reposting was stopped; the saved backup was kept.')
                    update_listing_fields(account, str(listing['id']), {
                        'source_deleted': True, 'facebook_deleted_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})

                if job_id:
                    with repost_jobs_lock:
                        if job_id in repost_jobs:
                            repost_jobs[job_id]["status"] = "posting"
                update_listing_fields(account, str(listing['id']), {'repost_status': 'posting'})
                for payload in payloads:
                    try:
                        payload['_require_publish_confirmation'] = True
                        result = post_to_fb_marketplace(payload, account)
                        if not result:
                            raise RuntimeError('Posting did not return a success signal')
                    except Exception as exc:
                        raise RuntimeError(
                            f'Posting failed: {exc}. The original Facebook listing has been removed; '
                            'the saved backup was kept and can be reposted again.'
                        ) from exc

            reposted_at = datetime.now(timezone.utc)
            posted_details = dict(post)
            posted_details.pop('image_urls', None)
            changes = {
                **posted_details,
                'details': posted_details,
                'listed_date': reposted_at.astimezone(timezone(timedelta(hours=5))).date().isoformat(),
                'clicks': 0, 'clicks_confirmed': True,
                'clicks_checked_at': reposted_at.isoformat(timespec='seconds'),
                'downloaded_images': [next(photo for photo in photos if photo['name'] == name)
                                      for name in order if name in by_name],
                'repost_status': 'done', 'repost_error': '', 'facebook_reposted': True,
                'facebook_present': True,
                'facebook_reposted_at': reposted_at.isoformat(timespec='seconds'),
            }
            if not update_listing_fields(account, str(listing.get('id') or ''), changes, move_to_top=True):
                raise RuntimeError('Repost was confirmed, but its saved listing record was not found for updating.')
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
            update_listing_fields(account, str(listing.get('id') or ''), {'repost_status': 'failed', 'repost_error': str(e)})


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


@cancellable
def _post_single_listing(post_data, account, job_id=None):
    account_post_lock = _get_post_lock(account)
    if job_id:
        with scheduled_posts_lock:
            meta = scheduled_posts.get(job_id)
            if not meta or meta.get("status") != "pending":
                return
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
            if DESKTOP_MODE:
                from automation.desktop_state import checkpoint
                checkpoint()
            result = post_to_fb_marketplace(post_data, account, post_mode=post_mode, group_names=group_names)
        if job_id:
            with scheduled_posts_lock:
                if job_id in scheduled_posts:
                    scheduled_posts[job_id]["status"] = "posted" if result else "failed"
                    scheduled_posts[job_id]["posted_at"] = datetime.now()
        status = "success" if result else "failed"
        title = (post_data or {}).get("title") or "Untitled"
        _log_event(
            event_type="post_listing",
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


def _register_scheduled_post(job_id, account, post_data, run_at, platform="facebook"):
    post_payload = post_data if isinstance(post_data, dict) else {}
    title = post_payload.get("title") or "Untitled"
    make = post_payload.get("make") or ""
    with scheduled_posts_lock:
        scheduled_posts[job_id] = {
            "job_id": job_id,
            "account": account,
            "platform": platform,
            "title": title,
            "make": make,
            "run_at": run_at,
            "status": "pending",
            "created_at": datetime.now(),
            "started_at": None,
            "posted_at": None
        }

    if DESKTOP_MODE:
        from automation.desktop_state import checkpoint
        checkpoint()


def _collect_account_schedule(active_account):
    now = datetime.now()
    items = []
    pending_count = 0

    for job in scheduler.get_jobs():
        with scheduled_posts_lock:
            known_meta = dict(scheduled_posts.get(job.id, {}))
        job_account = None
        post_data = None
        if job.args and len(job.args) >= 2:
            post_data = job.args[0]
            job_account = job.args[1]

        # This scheduler view is only for normal post jobs (dict payload),
        # not repost-read jobs where first arg is a URL string.
        if not isinstance(post_data, dict):
            continue

        if job_account != active_account:
            continue

        with scheduled_posts_lock:
            known_job = job.id in scheduled_posts
        if not known_job:
            _register_scheduled_post(
                job.id,
                job_account,
                post_data,
                job.next_run_time or now,
                platform="facebook",
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
            "make": job_meta.get("make", ""),
            "platform": "facebook",
            "source": "Manual / Excel",
            "status": status,
            "run_at": next_run,
            "remaining_seconds": remaining_seconds
        })

        if status == "pending":
            pending_count += 1

    # Include jobs already handed off by APScheduler. Posting jobs can wait
    # here for their account's one-at-a-time posting lock.
    visible_job_ids = {item["job_id"] for item in items}
    with scheduled_posts_lock:
        scheduled_snapshot = list(scheduled_posts.items())

    for job_id, meta in scheduled_snapshot:
        if meta.get('source') == 'Cross posting':
            if active_account not in {meta.get('account'), meta.get('source_account')} or meta.get('status') == 'deleted':
                continue
            run_at = meta['run_at']
            remaining = max(0, int((run_at - datetime.now()).total_seconds()))
            items.append({
                'job_id': job_id, 'title': meta['title'], 'make': meta.get('make', ''),
                'platform': 'facebook', 'source': 'Cross posting',
                'account': meta['account'], 'source_account': meta['source_account'],
                'error': meta.get('error', ''), 'status': meta['status'],
                'run_at': run_at, 'remaining_seconds': remaining,
            })
            if meta['status'] in {'pending', 'waiting'}:
                pending_count += 1
            continue
        if meta.get("account") != active_account:
            continue
        meta_status = str(meta.get("status") or "").strip().lower()
        if meta_status not in {"waiting", "posting"} or job_id in visible_job_ids:
            continue
        run_at = meta.get("started_at") or meta.get("run_at") or now
        items.append({
            "job_id": job_id,
            "title": meta.get("title", "Untitled"),
            "make": meta.get("make", ""),
            "platform": "facebook",
            "source": "Manual / Excel",
            "status": meta_status,
            "run_at": run_at,
            "remaining_seconds": 0
        })
        if meta_status == "waiting":
            pending_count += 1

    with scheduled_posts_lock:
        posted_count = sum(
            1 for p in scheduled_posts.values()
            if (p.get("account") == active_account or
                 (p.get('source') == 'Cross posting' and p.get('source_account') == active_account))
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

    # Fast path from in-memory job registry.
    with scheduled_posts_lock:
        for meta in scheduled_posts.values():
            if meta.get("account") != account:
                continue
            if str(meta.get("status") or "").strip().lower() in {"pending", "waiting", "posting"}:
                return True

    schedule_data = _collect_account_schedule(account)
    for item in schedule_data.get("items", []):
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

    for index, file in enumerate(files[:20], start=1):
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


def _save_manual_videos(files):
    """Optional media must never prevent the photos/listing from being scheduled."""
    saved = []
    for uploaded in files[:5]:
        try:
            name = secure_filename(str(uploaded.filename or ""))
            ext = os.path.splitext(name)[1].lower()
            if ext not in {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}:
                _runtime_log(f"[ManualVideo] Skipped unsupported video: {name}")
                continue
            folder = str(data_dir("manual_listing_videos"))
            path = os.path.join(folder, f"manual_{uuid4().hex}{ext}")
            uploaded.save(path)
            saved.append(path)
        except Exception as exc:
            _runtime_log(f"[ManualVideo] Could not save video; skipping: {exc}")
    return saved


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
    if DESKTOP_MODE:
        from automation.maintenance import pause_groups
        pause_groups(account_email, False)
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
            if DESKTOP_MODE:
                from automation.maintenance import record
                record(account_email, 'groups', True)
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

    if DESKTOP_MODE:
        from automation.maintenance import pause_groups
        pause_groups(active_account)

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
                with _get_post_lock(active_account):
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


@web_bp.route("/reposting", methods=["GET", "POST"])
def reposting_page():
    # Reposting is still started from a saved listing; this page is status only.
    if request.method == "POST":
        return redirect(url_for("web.delete_listings_page"))
    active_account = get_active_account()
    schedule = _collect_repost_schedule(active_account)
    return render_template(
        "reposting.html", accounts=get_all_accounts(), active_account=active_account,
        active_page="reposting", repost_items=schedule["items"],
        pending_count=schedule["pending_count"], running_count=schedule["running_count"],
        repost_history=_collect_repost_history(active_account),
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


@web_bp.route('/tasks/active')
def active_browser_tasks():
    from automation.task_control import active_tasks
    return jsonify(tasks=active_tasks(get_active_account()))


@web_bp.route('/tasks/cancel', methods=['POST'])
def cancel_browser_task():
    from automation.task_control import cancel, active_tasks
    data = request.get_json(silent=True) or {}
    task = next((item for item in active_tasks(get_active_account()) if item['id'] == str(data.get('id') or '')), None)
    if not cancel(str(data.get('id') or ''), get_active_account()):
        return jsonify(ok=False, message='Task has finished or belongs to another account.'), 404
    if DESKTOP_MODE and task and task['action'] == 'import_groups_from_facebook':
        from automation.maintenance import pause_groups
        pause_groups(get_active_account())
    return jsonify(ok=True, message='Cancellation requested. Completed Facebook actions cannot be undone; review Facebook before retrying.')


@web_bp.route('/reposting/cancel', methods=['POST'])
def cancel_repost():
    from automation.task_control import cancel_job
    account = get_active_account()
    job_id = str((request.get_json(silent=True) or {}).get('job_id') or '')
    with repost_jobs_lock:
        meta = repost_jobs.get(job_id)
        if not account or not meta or meta.get('account') != account:
            return jsonify(ok=False, message='Repost not found for this account.'), 404
        if meta.get('status') == 'pending':
            meta.update(status='cancelled', finished_at=datetime.now())
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass
        elif meta.get('status') in {'reading', 'deleting', 'posting'}:
            if not cancel_job(job_id, account):
                return jsonify(ok=False, message='Repost already finished; refresh the page.'), 409
        else:
            return jsonify(ok=False, message='Repost is no longer active.'), 409
    if DESKTOP_MODE:
        from automation.desktop_state import checkpoint
        checkpoint()
    return jsonify(ok=True, message='Cancellation requested. Already completed actions cannot be undone.')


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


@web_bp.route("/settings")
def settings_page():
    from automation.desktop_preferences import load
    from automation.app_updates import config
    active_account = get_active_account()
    return render_template(
        "settings.html",
        accounts=get_all_accounts(),
        active_account=active_account,
        active_page="settings",
        desktop_mode=DESKTOP_MODE,
        desktop_preferences=load(),
        app_version=config()["version"],
        auto_listing_running=any(_is_auto_listing_running_for_account(account) for account in get_all_accounts()),
    )


@web_bp.route('/settings/desktop', methods=['POST'])
def desktop_preferences_save():
    from automation.desktop_preferences import save
    save(request.form.get('background_sync') == '1', request.form.get('start_with_windows') == '1',
         automatic_updates=request.form.get('automatic_updates') == '1')
    return redirect(url_for('web.settings_page'))


@web_bp.route('/settings/updates/status')
def desktop_update_status():
    from automation.app_updates import status
    return jsonify(status())


@web_bp.route('/settings/updates/check', methods=['POST'])
def desktop_update_check():
    from automation.app_updates import check, status
    check()
    return jsonify(status())


@web_bp.route('/settings/updates/install', methods=['POST'])
def desktop_update_install():
    from automation.app_updates import install, status
    try:
        install()
        return jsonify(status())
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 409


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

# -------------------------
# Account management
# -------------------------

@web_bp.route("/account", methods=["GET", "POST"])
def account_page():
    cleanup_finished_remote_login_sessions()
    cleanup_expired_cdp_logins()
    # A missing cookie export is recoverable through Re-login. Keep the
    # registered profile instead of silently deleting the account here.
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
        session["account_message"] = f"Login successful: {account_title}. Facebook session saved."
        session["account_message_type"] = "success"
    return jsonify({
        "ok": True,
        "ready": ready,
        "status": meta.get("status", "waiting"),
        "message": meta.get("message", "Waiting for Facebook login."),
        "port": meta.get("port"),
    })


@web_bp.route('/account/relogin', methods=['POST'])
def account_relogin():
    account = str(request.form.get('account') or '').strip()
    if account not in get_all_accounts():
        return 'Saved account not found.', 404
    try:
        start_cdp_login(account, relogin=True)
        session['account_message'] = f'Log into the same Facebook account for {account}. Your saved listings and settings are kept.'
        session['account_message_type'] = 'success'
    except Exception as exc:
        session['account_message'] = f'Could not open re-login: {exc}'
        session['account_message_type'] = 'failed'
    return redirect(url_for('web.account_page'))


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
    auto_listing_running = any(_is_auto_listing_running_for_account(account) for account in get_all_accounts())
    redirect_target = request.referrer or url_for("web.auto_listing_page")

    if auto_listing_running:
        return redirect(redirect_target)

    folders = [screenshots_dir(), _manual_images_directory(), str(data_dir("manual_listing_videos"))]
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
                os.path.join(project_data, "manual_listing_videos"),
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

    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".jfif", ".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
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
        auto_listing_running=any(_is_auto_listing_running_for_account(account) for account in get_all_accounts()),
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
        auto_listing_running=any(_is_auto_listing_running_for_account(account) for account in get_all_accounts()),
        prefill=prefill,
        groups=groups,
    )


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
    if post_action not in {"simple", "group"}:
        return _render_manual("Unsupported posting action. Choose a Facebook posting option.", "failed")

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
    # Color validation is disabled for now. Exterior and interior colors are
    # intentionally optional and are not required before scheduling a post.
    # missing_color_fields = [
    #     label
    #     for field, label in (
    #         ("exterior_color", "Exterior color"),
    #         ("interior_color", "Interior color"),
    #     )
    #     if not post_data.get(field)
    # ]
    # if missing_color_fields:
    #     return _render_manual(
    #         f"{', '.join(missing_color_fields)} must be selected.",
    #         "failed",
    #     )
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
        if len(saved_image_paths) >= 20:
            break
        if p not in saved_image_paths:
            saved_image_paths.append(p)

    if not saved_image_paths:
        return _render_manual("At least one valid image is required.", "failed")

    for index, image_path in enumerate(saved_image_paths[:20], start=1):
        post_data[f"image_path{index}"] = image_path

    post_data["video_paths"] = _save_manual_videos(request.files.getlist("videos"))

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
    )

@web_bp.route("/post_schedular_data", methods=["GET"])
def post_schedular_data():
    active_account = get_active_account()
    schedule_data = _collect_account_schedule(active_account)

    items_payload = []
    for item in schedule_data["items"]:
        items_payload.append({
            "job_id": item["job_id"],
            "title": item["title"],
            "make": item.get("make", ""),
            "platform": item.get("platform", "facebook"),
            "source": item.get("source", ""),
            "account": item.get("account", active_account),
            "source_account": item.get("source_account", ""),
            "error": item.get("error", ""),
            "status": item["status"],
            "run_at": item["run_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_seconds": item["remaining_seconds"]
        })

    return jsonify({
        "ok": True,
        "account": active_account,
        "pending_count": schedule_data["pending_count"],
        "posted_count": schedule_data["posted_count"],
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
    if not active_account:
        return jsonify({"ok": False, "message": "No active account selected"}), 400
    if not job_meta or active_account not in {job_meta.get("account"), job_meta.get("source_account")}:
        return jsonify({"ok": False, "message": "Job not found for this account"}), 404

    if job_meta.get('status') == 'posting':
        from automation.task_control import cancel_job
        if cancel_job(job_id, job_meta.get('account')):
            return jsonify(ok=True, message='Cancellation requested. Completed Facebook actions cannot be undone.')
        return jsonify(ok=False, message='Posting already finished; refresh the schedule.'), 409

    cancellable = {'pending', 'waiting'} if job_meta.get('source') == 'Cross posting' else {'pending'}
    if job_meta.get("status") not in cancellable:
        return jsonify({"ok": False, "message": "Only pending jobs can be deleted"}), 400

    with scheduled_posts_lock:
        if scheduled_posts.get(job_id, {}).get('status') not in cancellable:
            return jsonify(ok=False, message='Posting has already started.'), 409
        if job_id in scheduled_posts:
            scheduled_posts[job_id]["status"] = "deleted"
            scheduled_posts[job_id]["deleted_at"] = datetime.now()
    if job_meta.get('source') != 'Cross posting':
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    if DESKTOP_MODE:
        from automation.desktop_state import checkpoint
        checkpoint()
    _log_event(
        event_type="scheduler",
        account=active_account,
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
    for listing in listings:
        listing['display_status'] = _repost_listing_status(active_account, listing)
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


@contextmanager
def _listing_import_account_lock(account, stop_event):
    lock = _get_post_lock(account)
    while not lock.acquire(timeout=0.2):
        if stop_event.is_set():
            raise ListingImportCancelled()
    try:
        if stop_event.is_set():
            raise ListingImportCancelled()
        yield
    finally:
        lock.release()


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
        if automatic and current.get('stop_requested'):
            return False, 'Automatic import paused after Stop Import. Start a manual import to resume.'
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
            if job.get('stop_requested'):
                return
            job["new_count"] = int(event.get("new_count") or 0)
            job["total_count"] = int(event.get("total_count") or 0)
            job["message"] = event.get("message") or job.get("message")
            listing_import_jobs[account] = job

    def _runner():
        try:
            # Imports merge into the backup collection; absent sources stay saved.
            with _listing_import_account_lock(account, stop_event):
                if stop_event.is_set():
                    raise ListingImportCancelled()
                import_listings_from_facebook(
                    account,
                    stop_event=stop_event,
                    progress_callback=_progress,
                    replace_existing=False,
                    only_new=(automatic and not DESKTOP_MODE),
                    use_visual_reader=not automatic,
                )
            if stop_event.is_set():
                raise ListingImportCancelled()
            mark_import_complete(account)
            if DESKTOP_MODE:
                from automation.maintenance import record
                record(account, "import", True)
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
                if DESKTOP_MODE:
                    from automation.maintenance import record
                    record(account, "import", False, str(exc))
        finally:
            with listing_import_jobs_lock:
                listing_import_cancel_events.pop(account, None)

    threading.Thread(target=_runner, daemon=True, name=f"listing-import-{account}").start()
    return True, "Automatic listing refresh started." if automatic else "Listing import started."


def _listing_snapshot_is_due(account, now=None):
    """Return True once an existing imported snapshot is at least 24 hours old."""
    updated_at = load_listings(account).get("last_import_at")
    if not updated_at:
        return True
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
            with _get_post_lock(account):
                refresh_listing_clicks_from_facebook(account, progress_callback=_progress)
            if DESKTOP_MODE:
                from automation.maintenance import record
                record(account, "clicks", True)
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
            if DESKTOP_MODE:
                from automation.maintenance import record
                record(account, "clicks", False, str(exc))

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
    for listing in stored.get('listings') or []:
        listing['display_status'] = _repost_listing_status(account, listing)
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


REPOST_EDIT_FIELDS = ('title', 'vehicle_type', 'year', 'make', 'model', 'mileage', 'price',
                     'body_style', 'exterior_color', 'interior_color', 'vehicle_condition',
                     'fuel_type', 'transmission', 'description', 'location', 'clean_title')


def _repost_preferences(listing, data):
    fields, order = data.get('fields'), data.get('image_order')
    names = [photo['name'] for photo in listing.get('downloaded_images', [])]
    if not isinstance(fields, dict) or any(key not in REPOST_EDIT_FIELDS or not isinstance(value, str) or len(value) > 20000 for key, value in fields.items()):
        raise ValueError('Invalid repost fields.')
    if not isinstance(order, list) or any(not isinstance(name, str) for name in order) or len(order) != len(names) or set(order) != set(names):
        raise ValueError('Image order must contain every imported photo once.')
    source = listing.get('details') or {}
    return {'fields': {key: value for key, value in fields.items() if value != str(source.get(key) or '')}, 'image_order': order}


def _repost_listing_status(account, listing):
    deletion = _facebook_delete_snapshot(account)
    if deletion.get('running') and listing.get('id') in deletion.get('ids', []):
        return 'facebook_deleting'
    with repost_jobs_lock:
        matches = [job for job in repost_jobs.values() if job.get('account') == account and str(job.get('listing_id')) == str(listing.get('id'))]
        if matches:
            latest = matches[-1]
            if latest.get('status') == 'pending' and latest.get('delay_seconds') == 0:
                return 'queued'
            return latest.get('status', '')
    # Pending jobs are in memory; don't show a stale schedule after a restart.
    if listing.get('repost_status') in {'done', 'failed'}:
        return listing['repost_status']
    return 'backup' if listing.get('source_deleted') else ''


@web_bp.route('/delete_listings/detail/<listing_id>', methods=['GET', 'POST'])
def listing_detail(listing_id):
    account = get_active_account()
    listing = next((item for item in load_listings(account).get('listings', []) if item.get('id') == listing_id), None) if account else None
    if listing is None:
        return 'Listing not found', 404
    busy = bool(listing_id in _active_repost_listing_ids(account) or _delete_import_snapshot(account).get('running')
                or _listing_click_snapshot(account).get('running') or _facebook_delete_snapshot(account).get('running'))
    if request.method == 'POST':
        if busy:
            return jsonify(ok=False, message='Wait for listing operations and scheduled reposts to finish.'), 409
        data = request.get_json(silent=True) or {}
        try:
            preferences = _repost_preferences(listing, data)
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
        save_preferences(account, listing_id, preferences)
        return jsonify(ok=True, message='Repost preferences saved.')
    return render_template('listing_detail.html', listing=listing, fields=REPOST_EDIT_FIELDS,
                           busy=busy, repost_status=_repost_listing_status(account, listing),
                           accounts=get_all_accounts(), active_account=account, active_page='delete')


@web_bp.route('/delete_listings/photo/<listing_id>/<name>')
def listing_photo(listing_id, name):
    account = get_active_account()
    listing = next((item for item in load_listings(account).get('listings', []) if item.get('id') == listing_id), None) if account else None
    photo = next((item for item in (listing or {}).get('downloaded_images', []) if item.get('name') == name), None)
    if not photo:
        return 'Photo not found', 404
    return send_from_directory(os.path.dirname(photo['path']), os.path.basename(photo['path']))


def _save_confirmed_cross_post(meta):
    import hashlib
    import shutil
    from automation.listing_store import add_at_top, data_dir as listing_data_dir
    confirmed_at = datetime.now(timezone.utc)
    record = copy.deepcopy(meta['listing_snapshot'])
    listing_id = meta['target_listing_id']
    folder = listing_data_dir() / 'cross_post_images' / hashlib.sha256(meta['account'].encode()).hexdigest()[:12] / listing_id
    folder.mkdir(parents=True, exist_ok=True)
    copied_photos = []
    for photo in record.get('downloaded_images') or []:
        copied = dict(photo)
        destination = folder / os.path.basename(photo['name'])
        shutil.copy2(photo['path'], destination)
        copied['path'] = str(destination.resolve())
        copied_photos.append(copied)
    record.update({
        'id': listing_id, 'downloaded_images': copied_photos,
        'listed_date': confirmed_at.astimezone(timezone(timedelta(hours=5))).date().isoformat(),
        'clicks': 0, 'clicks_confirmed': True,
        'clicks_checked_at': confirmed_at.isoformat(timespec='seconds'),
        'cross_posted_at': confirmed_at.isoformat(timespec='seconds'),
        'cross_posted_from': meta['source_account'],
        'url': '', 'edit_url': '', 'source_deleted': False, 'facebook_present': True,
        'repost_preferences': {'fields': {}, 'image_order': [photo['name'] for photo in copied_photos]},
    })
    add_at_top(meta['account'], record)


@cancellable
def _cross_post_payload(payload, account, job_id):
    return post_to_fb_marketplace(payload, account)


def _run_cross_post_batch(job_ids):
    # One global queue also prevents two cross-post batches overlapping.
    with cross_post_queue_lock:
        for job_id in job_ids:
            with scheduled_posts_lock:
                meta = scheduled_posts.get(job_id)
                if not meta or meta['status'] != 'pending':
                    continue
                meta['status'] = 'waiting'
                account = meta['account']
                payload = copy.deepcopy(meta['post_data'])
            # Reserve the target's posting lock before marking it started.
            # The posting function returns only after its browser work finishes.
            with _get_post_lock(account):
                with scheduled_posts_lock:
                    if meta['status'] == 'deleted':
                        continue
                    meta['status'] = 'posting'
                    meta['started_at'] = datetime.now()
                try:
                    if DESKTOP_MODE:
                        from automation.desktop_state import checkpoint
                        checkpoint()
                    if account not in get_all_accounts():
                        raise RuntimeError('Target account is no longer saved.')
                    if not _cross_post_payload(payload, account, job_id):
                        raise RuntimeError('Facebook did not confirm posting.')
                    with scheduled_posts_lock:
                        meta['status'] = 'posted'
                        meta['posted_at'] = datetime.now()
                    try:
                        _save_confirmed_cross_post(meta)
                    except Exception as storage_error:
                        with scheduled_posts_lock:
                            meta['error'] = f'Facebook posting was confirmed, but saving the target listing failed: {storage_error}'
                        _runtime_log(f'[CrossPosting] Target backup failed job={job_id}: {storage_error}')
                except Exception as exc:
                    with scheduled_posts_lock:
                        meta['status'] = 'failed'
                        meta['error'] = str(exc)
                        meta['posted_at'] = datetime.now()
                    _runtime_log(f'[CrossPosting] Failed job={job_id} account={account}: {exc}')
            _runtime_log(f'[CrossPosting] Completed job={job_id} account={account} status={meta["status"]}')


@web_bp.route('/delete_listings/cross_post', methods=['POST'])
def cross_post_listing():
    account = get_active_account()
    data = request.get_json(silent=True) or {}
    if not account:
        return jsonify(ok=False, message='Select a source account first.'), 400
    if data.get('account') != account:
        return jsonify(ok=False, message='The active account changed. Reopen the listing.'), 409
    targets = data.get('target_accounts')
    available = set(get_all_accounts()) - {account}
    if (not isinstance(targets, list) or not 1 <= len(targets) < 20
            or any(not isinstance(target, str) for target in targets)
            or len(set(targets)) != len(targets) or not set(targets) <= available):
        return jsonify(ok=False, message='Select 1 to 19 different saved accounts, excluding the source account.'), 400
    listing_id = str(data.get('listing_id') or '')
    listing = next((item for item in load_listings(account).get('listings', []) if str(item.get('id')) == listing_id), None)
    if not listing:
        return jsonify(ok=False, message='Listing was not found.'), 404
    if (listing_id in _active_repost_listing_ids(account) or _delete_import_snapshot(account).get('running')
            or _listing_click_snapshot(account).get('running') or _facebook_delete_snapshot(account).get('running')):
        return jsonify(ok=False, message='Wait for the current listing operation to finish.'), 409
    try:
        preferences = _repost_preferences(listing, data)
        if data.get('post_mode') not in {'now', 'schedule'}:
            raise ValueError('Choose Post now or Schedule for later.')
        seconds = 0
        if data['post_mode'] == 'schedule':
            value = data.get('delay_value')
            unit = data.get('delay_unit')
            limits = {'minutes': 59, 'hours': 24, 'days': 365}
            if type(value) is not int or unit not in limits or not 1 <= value <= limits[unit]:
                raise ValueError('Enter a valid whole-number delay.')
            seconds = value * {'minutes': 60, 'hours': 3600, 'days': 86400}[unit]
        photos = listing.get('downloaded_images') or []
        if not listing.get('details') or not photos or listing.get('image_errors'):
            raise ValueError('Import the listing details and photos first.')
        post = dict(listing['details'], **preferences['fields'])
        if 'title' not in preferences['fields'] and any(key in preferences['fields'] for key in ('year', 'make', 'model')):
            post['title'] = ' '.join(str(post.get(key) or '').strip() for key in ('year', 'make', 'model')).strip()
        by_name = {photo['name']: photo['path'] for photo in photos}
        post['image_urls'] = [by_name[name] for name in preferences['image_order']]
        if any(not os.path.isfile(path) for path in post['image_urls']):
            raise ValueError('Saved photos are missing. Import the listing again.')
        payloads = _build_repost_payloads({'posts': [post]})
        if not payloads or not payloads[0]['title'] or not payloads[0]['price']:
            raise ValueError('Title and price are required.')
    except (ValueError, KeyError) as exc:
        return jsonify(ok=False, message=str(exc)), 400
    run_at = datetime.now() + timedelta(seconds=seconds or 1)
    batch_id = 'cross_' + uuid4().hex
    job_ids = []
    with scheduled_posts_lock:
        for index, target in enumerate(targets):
            job_id = f'{batch_id}_{index}'
            payload = dict(payloads[0], _require_publish_confirmation=True)
            scheduled_posts[job_id] = {
                'job_id': job_id, 'account': target, 'source_account': account,
                'source_listing_id': listing_id, 'source': 'Cross posting', 'platform': 'facebook',
                'title': payload['title'], 'make': payload.get('make', ''), 'post_data': payload,
                'run_at': run_at, 'status': 'pending', 'created_at': datetime.now(),
                'started_at': None, 'posted_at': None, 'error': '',
                'target_listing_id': uuid4().hex[:20],
                'listing_snapshot': {
                    **{key: listing.get(key, '') for key in ('year', 'make', 'model')},
                    **{key: value for key, value in post.items() if key != 'image_urls'},
                    'details': {key: value for key, value in post.items() if key != 'image_urls'},
                    'downloaded_images': [copy.deepcopy(next(photo for photo in photos if photo['name'] == name))
                                          for name in preferences['image_order'][:10]],
                },
            }
            job_ids.append(job_id)
    try:
        scheduler.add_job(_run_cross_post_batch, trigger='date', run_date=run_at,
                          args=[job_ids], id=batch_id, misfire_grace_time=None)
    except Exception as exc:
        with scheduled_posts_lock:
            for job_id in job_ids:
                scheduled_posts.pop(job_id, None)
        return jsonify(ok=False, message=f'Could not queue cross posting: {exc}'), 500
    if DESKTOP_MODE:
        from automation.desktop_state import checkpoint
        checkpoint()
    return jsonify(ok=True, job_ids=job_ids, run_at=run_at.strftime('%Y-%m-%d %H:%M:%S'),
                   message=f'Cross posting queued for {len(targets)} account(s), one at a time. See Scheduled Posts for results.')


@web_bp.route("/delete_listings/repost", methods=["POST"])
def delete_listings_repost():
    account = get_active_account()
    if not account:
        return jsonify({"ok": False, "message": "No active account selected."}), 400
    data = request.get_json(silent=True) or {}
    if data.get('account') and data['account'] != account:
        return jsonify(ok=False, message='The active account changed. Reopen the listing before continuing.'), 409
    immediate = data.get('post_mode') == 'now'
    listing_id = str(data.get("listing_id") or "").strip()
    delay_unit = str(data.get("delay_unit") or "").strip().lower()
    try:
        delay_value = 0 if immediate else int(data.get("delay_value"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Enter a whole-number delay."}), 400
    if immediate:
        delay_unit = 'minutes'
    if delay_unit not in {"minutes", "hours", "days"}:
        return jsonify({"ok": False, "message": "Choose minutes, hours, or days."}), 400
    if not immediate and delay_unit == "minutes" and not (1 <= delay_value <= 59):
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
    if _delete_import_snapshot(account).get('running') or _listing_click_snapshot(account).get('running') or _facebook_delete_snapshot(account).get('running'):
        return jsonify(ok=False, message='Wait for the current listing operation to finish.'), 409
    photos = listing.get('downloaded_images') or []
    if not listing.get('details') or not photos or listing.get('image_errors') or any(not os.path.isfile(photo['path']) for photo in photos):
        return jsonify(ok=False, message='Import this listing again to finish saving its details and photos.'), 400
    if not listing.get('source_deleted') and not _extract_listing_id(listing.get('edit_url') or listing.get('url') or ''):
        return jsonify(ok=False, message='Import this listing again to save its Facebook listing ID.'), 400
    if 'fields' in data or 'image_order' in data:
        try:
            listing['repost_preferences'] = _repost_preferences(listing, data)
        except ValueError as exc:
            return jsonify(ok=False, message=str(exc)), 400
    effective = dict(listing['details'], **listing.get('repost_preferences', {}).get('fields', {}))
    if not str(effective.get('title') or '').strip() or not str(effective.get('price') or '').strip():
        return jsonify(ok=False, message='Title and price are required for reposting.'), 400

    delay_seconds = delay_value * {"minutes": 60, "hours": 3600, "days": 86400}[delay_unit]
    run_at = datetime.now() + timedelta(seconds=delay_seconds if not immediate else 1)
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
        if active_jobs and delay_seconds <= previous_delay:
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
        save_preferences(account, listing_id, listing.get('repost_preferences') or {})
        scheduler.add_job(
            _run_saved_listing_repost,
            trigger="date",
            run_date=run_at,
            args=[listing, account, job_id],
            id=job_id,
            replace_existing=False,
        )
        update_listing_fields(account, listing_id, {'repost_status': 'pending', 'repost_run_at': run_at.isoformat(timespec='seconds'), 'repost_error': ''})
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
        "message": f"{title} queued for reposting." if immediate else f"{title} scheduled for reposting.",
        "job_id": job_id,
        "run_at": run_at.strftime("%Y-%m-%d %H:%M:%S"),
    })


@web_bp.route("/delete_listings/delete", methods=["POST"])
def delete_listings_delete():
    account = get_active_account()
    if not account:
        return jsonify(ok=False, message="No active account selected."), 400
    if _delete_import_snapshot(account).get('running') or _listing_click_snapshot(account).get('running'):
        return jsonify(ok=False, message="Wait for the current listing refresh to finish."), 409
    data = request.get_json(silent=True) or {}
    if data.get('account') and data['account'] != account:
        return jsonify(ok=False, message='The active account changed. Reopen the listing before continuing.'), 409
    target = data.get('target', 'facebook')
    if target not in {'facebook', 'database', 'both'}:
        return jsonify(ok=False, message="Choose Facebook, database, or both."), 400
    ids = data.get('ids') or []
    if not isinstance(ids, list):
        return jsonify(ok=False, message="Invalid listing selection."), 400
    selected_ids = {str(value).strip() for value in ids if str(value).strip()}
    if not selected_ids:
        return jsonify(ok=False, message="Select at least one listing."), 400
    if _active_repost_listing_ids(account):
        return jsonify(ok=False, message="Wait for scheduled reposts to finish before deleting listings."), 409
    with facebook_delete_jobs_lock:
        if (facebook_delete_jobs.get(account) or {}).get('running'):
            return jsonify(ok=False, message="A listing deletion is already running."), 409
        selected = [item for item in load_listings(account).get('listings', []) if str(item['id']) in selected_ids]
        if len(selected) != len(selected_ids):
            return jsonify(ok=False, message="Selected listings were not found in this account's storage."), 404
        if target == 'database':
            removed = remove_listing_backups(account, selected_ids)
            return jsonify(ok=True, completed=True, removed_ids=sorted(selected_ids),
                           message=f"Removed {removed} saved listing(s) from the database. Facebook was unchanged.")
        live = []
        for item in selected:
            if item.get('source_deleted'):
                continue
            source = dict(item)
            source['_source_listing_id'] = _extract_listing_id(item.get('edit_url') or item.get('url') or '')
            if not source['_source_listing_id']:
                return jsonify(ok=False, message="Import the selected listing again to save its Facebook ID before deleting it."), 400
            live.append(source)
        stop_event = threading.Event()
        job_id = uuid4().hex
        facebook_delete_cancel_events[account] = stop_event
        facebook_delete_jobs[account] = {
            'job_id': job_id, 'ids': sorted(selected_ids), 'target': target,
            'running': True, 'started_at': time.time(), 'finished_at': None,
            'message': 'Deleting selected listings...', 'clicked': 0, 'deleted': 0,
            'total': len(selected), 'error': None, 'stop_requested': False,
        }

    def _progress(event):
        with facebook_delete_jobs_lock:
            facebook_delete_jobs[account].update({
                'clicked': int(event.get('clicked') or 0), 'deleted': int(event.get('deleted') or 0),
                'message': event.get('message') or 'Deleting selected listings...',
            })

    def _runner():
        try:
            with _get_post_lock(account):
                result = delete_selected_listings_from_facebook(account, live, stop_event=stop_event,
                    progress_callback=_progress) if live else {'deleted_ids': [], 'deleted': 0}
                deleted_ids = set(result.get('deleted_ids') or []) & selected_ids
                for listing_id in deleted_ids:
                    update_listing_fields(account, listing_id, {'source_deleted': True,
                        'facebook_deleted_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
                completed_ids = deleted_ids | {item['id'] for item in selected if item.get('source_deleted')}
                removed_ids = completed_ids if target == 'both' else set()
                if removed_ids:
                    remove_listing_backups(account, removed_ids)
                incomplete = selected_ids - completed_ids
                error = 'Some Facebook listings could not be deleted. Their saved backups were kept.' if incomplete and not result.get('stopped') else None
                message = ('Deletion stopped.' if result.get('stopped') else error or
                           ('Deleted from Facebook and the database.' if target == 'both' else 'Deleted from Facebook. The saved backup is kept.'))
                with facebook_delete_jobs_lock:
                    facebook_delete_jobs[account].update({
                        'running': False, 'finished_at': time.time(), 'clicked': int(result.get('clicked') or 0),
                        'deleted': len(deleted_ids), 'removed_ids': sorted(removed_ids),
                        'not_found': int(result.get('not_found') or 0), 'error': error, 'message': message,
                    })
        except Exception as exc:
            with facebook_delete_jobs_lock:
                facebook_delete_jobs[account].update({'running': False, 'finished_at': time.time(),
                    'error': None if stop_event.is_set() else str(exc),
                    'message': 'Deletion stopped.' if stop_event.is_set() else 'Deletion failed. The saved backup was kept.'})
            if not stop_event.is_set():
                _runtime_log(f"[DeleteListings] Deletion failed account={account}: {exc}")
        finally:
            with facebook_delete_jobs_lock:
                facebook_delete_cancel_events.pop(account, None)

    threading.Thread(target=_runner, daemon=True, name=f"facebook-listing-delete-{account}").start()
    return jsonify(ok=True, completed=False, job_id=job_id, message="Listing deletion started.")


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


if DESKTOP_MODE:
    from automation.maintenance import tick as maintenance_tick
    from automation.desktop_state import restore_state
    import sys
    restore_state(sys.modules[__name__])
else:
    maintenance_tick = _refresh_due_listing_snapshots

scheduler.add_job(
    maintenance_tick,
    trigger="interval",
    minutes=1 if DESKTOP_MODE else 5,
    id=LISTING_AUTO_REFRESH_CHECK_JOB_ID,
    replace_existing=True,
    next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    max_instances=1,
    coalesce=True,
)

if not scheduler.running:
    scheduler.start()
if DESKTOP_MODE:
    # Metadata for queued jobs is checkpointed without launching browsers.
    from automation.desktop_state import checkpoint
    scheduler.add_job(checkpoint, 'interval', seconds=5, id='desktop-state-checkpoint', replace_existing=True, coalesce=True, max_instances=1)
