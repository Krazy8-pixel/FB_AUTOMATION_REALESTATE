"""Manual task coordination and compatibility with retired maintenance schedules."""
import json
import os
import threading
import time
from automation.runtime_paths import data_dir

_lock = threading.RLock()
_cookie_running = False


def start_cookie_check(account):
    """Compatibility entry point; daily automatic cookie checks were removed."""
    return False, 'Automatic cookie checks are disabled. Check sessions manually in Accounts.'


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
    """No-op for old persisted jobs; automatic Facebook maintenance is disabled."""
    return


def start_scheduler_without_maintenance(scheduler):
    """Remove the legacy recurring job before any persisted schedule can run."""
    from apscheduler.jobstores.base import JobLookupError
    if scheduler.running:
        scheduler.pause()
    else:
        scheduler.start(paused=True)
    try:
        scheduler.remove_job('delete-listings-auto-refresh-check')
    except JobLookupError:
        pass
    scheduler.resume()
