"""One maintenance task at a time, with persistent clocks and retry backoff."""
import json
import os
import threading
import time
from automation.runtime_paths import data_dir

_lock = threading.RLock()


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
        for task, interval in [('groups', None), ('import', 24 * 3600), ('clicks', 4 * 3600)]:
            item = payload.get(account, {}).get(task, {})
            if task == 'groups' and item.get('paused'):
                continue
            if time.time() - item.get('last_attempt', 0) < 15 * 60:
                continue
            if task == 'groups':
                due = not item.get('last_success')
            elif task == 'import':
                due = routes._listing_snapshot_is_due(account)
            else:
                due = time.time() - item.get('last_success', 0) >= interval
            if not due:
                continue
            record(account, task, False, 'Waiting for completion')
            ok, message = (routes._start_groups_import_job(account) if task == 'groups' else
                           routes._start_listing_import_job(account, automatic=True) if task == 'import' else routes._start_listing_click_refresh_job(account))
            if not ok:
                record(account, task, False, message)
            return
