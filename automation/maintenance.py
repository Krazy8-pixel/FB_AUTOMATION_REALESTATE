"""One maintenance task at a time, with persistent clocks and retry backoff."""
import json
import os
import threading
import time
from automation.runtime_paths import data_dir

_lock = threading.RLock()
_cookie_running = False


def start_cookie_check(account):
    global _cookie_running
    with _lock:
        if _cookie_running:
            return False, 'Cookie check already running.'
        _cookie_running = True

    from automation.task_control import cancellable

    @cancellable
    def daily_cookie_check(account):
        global _cookie_running
        from automation.session_health import check_account_login_cookie, set_session_health, UNKNOWN
        try:
            status = check_account_login_cookie(account)
            if status is not None:
                set_session_health(account, status)
            record(account, 'cookies', status not in {None, UNKNOWN}, '' if status not in {None, UNKNOWN} else 'Cookie check could not finish.')
        except Exception as exc:
            record(account, 'cookies', False, str(exc))
        finally:
            with _lock:
                _cookie_running = False
    threading.Thread(target=daily_cookie_check, args=(account,), daemon=True, name='daily-cookie-check').start()
    return True, 'Cookie check started.'


def state():
    with _lock:
        path = data_dir() / 'maintenance.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def record(account, task, success, error=''):
    with _lock:
        payload = state()
        item = payload.setdefault(account, {}).setdefault(task, {})
        item.update(last_attempt=time.time(), error=str(error))
        if success:
            item['last_success'] = time.time()
        path = data_dir() / 'maintenance.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        os.replace(temporary, path)


def pause_groups(account, paused=True):
    with _lock:
        payload = state()
        payload.setdefault(account, {}).setdefault('groups', {})['paused'] = paused
        path = data_dir() / 'maintenance.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        os.replace(temporary, path)


def busy(routes, account=None):
    if _cookie_running:
        return True
    groups = [routes.listing_import_jobs, routes.listing_click_jobs, routes.facebook_delete_jobs, routes.groups_jobs]
    if any(lock.locked() for lock in list(routes.post_locks_by_account.values())):
        return True
    if any(job.get('running') for group in groups for job in list(group.values())):
        return True
    if any(job.get('status') in {'posting', 'reading', 'deleting', 'waiting'} for group in [routes.scheduled_posts, routes.repost_jobs] for job in list(group.values())):
        return True
    if account and routes._get_post_lock(account).locked():
        return True
    # Give user-started work priority, including a post about to start.
    now = time.time()
    return any(getattr(job, 'next_run_time', None) and job.next_run_time.timestamp() <= now + 60 and job.id not in {routes.LISTING_AUTO_REFRESH_CHECK_JOB_ID, 'desktop-state-checkpoint'} for job in routes.scheduler.get_jobs())


def tick():
    from web import routes
    from automation.desktop_state import checkpoint
    checkpoint()
    from automation.desktop_preferences import load
    if not load().get('background_sync', True):
        return
    if getattr(routes, 'desktop_update_pending', False) or busy(routes):
        return
    payload = state()
    for account in routes.get_all_accounts():
        if busy(routes, account):
            continue
        for task, interval in [('groups', None), ('import', 24 * 3600), ('clicks', 4 * 3600), ('cookies', 24 * 3600)]:
            item = payload.get(account, {}).get(task, {})
            if task == 'groups' and item.get('paused'):
                continue
            if time.time() - item.get('last_attempt', 0) < 15 * 60:
                continue
            if task == 'groups':
                due = not item.get('last_success')
            elif task == 'import':
                due = routes._listing_snapshot_is_due(account)
            elif task == 'cookies':
                due = time.time() - max(item.get('last_success', 0), item.get('last_attempt', 0)) >= interval
            else:
                due = time.time() - item.get('last_success', 0) >= interval
            if not due:
                continue
            record(account, task, False, 'Waiting for completion')
            ok, message = (routes._start_groups_import_job(account) if task == 'groups' else
                           routes._start_listing_import_job(account, automatic=True) if task == 'import' else
                           routes._start_listing_click_refresh_job(account) if task == 'clicks' else start_cookie_check(account))
            if not ok:
                record(account, task, False, message)
            return
