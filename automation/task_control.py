"""Cooperative cancellation of browser tasks without affecting tray lifetime."""
from automation.responsiveness import RequestLock
import functools
import inspect
import threading
import time
from uuid import uuid4
import os

_lock = RequestLock('task_control._lock', reentrant=True)
_local = threading.local()
_tasks = {}
_window_open = True
BACKGROUND_ACTIONS = {'import_groups_from_facebook', 'import_listings_from_facebook', 'refresh_listing_clicks_from_facebook', 'check_account_login_cookie', 'daily_cookie_check'}


def set_window_open(opened):
    global _window_open
    with _lock:
        _window_open = opened
        drivers = []
        if not opened:
            for task in _tasks.values():
                if task['action'] not in BACKGROUND_ACTIONS:
                    task['cancel'].set()
                    drivers.extend(task.get('drivers', []))
    if drivers:
        def close_browsers():
            for driver, execute in drivers:
                try:
                    execute('executeCdpCommand', {'cmd': 'Browser.close', 'params': {}})
                except Exception:
                    pass
                try:
                    driver.quit()
                except Exception:
                    pass
        threading.Thread(target=close_browsers, daemon=True).start()


def stop_foreground_work(routes):
    """Cancel permanently, including queued schedules, while preserving maintenance."""
    set_window_open(False)
    for group, lock in [(routes.scheduled_posts, routes.scheduled_posts_lock),
                        (routes.repost_jobs, routes.repost_jobs_lock)]:
        with lock:
            for meta in group.values():
                if meta.get('status') in {'pending', 'waiting', 'posting', 'reading', 'deleting'}:
                    meta.update(status='cancelled', error='Cancelled when the app window closed.')
    for job in routes.scheduler.get_jobs():
        if job.id not in {routes.LISTING_AUTO_REFRESH_CHECK_JOB_ID, 'desktop-state-checkpoint'}:
            try:
                routes.scheduler.remove_job(job.id)
            except Exception:
                pass
    from automation.desktop_state import checkpoint
    checkpoint()


def cancellable(function):
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if getattr(_local, 'task', None) is not None:
            check_cancelled()
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs).arguments
        task_id = uuid4().hex
        task = {'id': task_id, 'account': bound.get('account') or bound.get('account_email'),
                'job_id': bound.get('job_id'), 'action': function.__name__,
                'started_at': time.time(), 'cancel': threading.Event(), 'drivers': []}
        if task['account'] and os.getenv('FB_AUTOMATION_DESKTOP') == '1':
            from automation.account_browser import status as account_browser_status
            if account_browser_status().get('account') == task['account']:
                raise RuntimeError('Close this account in Account Browser before starting automation for it.')
        with _lock:
            if os.getenv('FB_AUTOMATION_DESKTOP') == '1' and not _window_open and task['action'] not in BACKGROUND_ACTIONS:
                raise RuntimeError('Task cancelled: the app window is closed. Start it again from the app.')
            _tasks[task_id] = task
        _local.task = task
        try:
            result = function(*args, **kwargs)
            check_cancelled()
            return result
        finally:
            _local.task = None
            with _lock:
                _tasks.pop(task_id, None)
    return wrapped


def check_cancelled():
    task = getattr(_local, 'task', None)
    if task and task['cancel'].is_set():
        raise RuntimeError('Cancelled by user. Review Facebook before retrying: completed actions cannot be undone.')


def guard_driver(driver):
    task = getattr(_local, 'task', None)
    if not task:
        return driver
    original = driver.execute

    def execute(command, params=None):
        cleanup = command in {'quit', 'close'} or (command == 'executeCdpCommand' and (params or {}).get('cmd') == 'Browser.close')
        if not cleanup:
            check_cancelled()
        return original(command, params)
    driver.execute = execute
    with _lock:
        task['drivers'].append((driver, original))
    if task['cancel'].is_set():
        try:
            original('executeCdpCommand', {'cmd': 'Browser.close', 'params': {}})
            driver.quit()
        finally:
            check_cancelled()
    return driver


def cancellable_sleep(seconds):
    task = getattr(_local, 'task', None)
    if not task:
        time.sleep(seconds)
    elif task['cancel'].wait(seconds):
        check_cancelled()


def has_active_tasks():
    with _lock:
        return bool(_tasks)


def active_tasks(account):
    with _lock:
        return [{key: value for key, value in task.items() if key not in {'cancel', 'drivers'}} |
                {'cancel_requested': task['cancel'].is_set()}
                for task in _tasks.values() if task['account'] == account]


def cancel(task_id, account):
    with _lock:
        task = _tasks.get(task_id)
        if not task or task['account'] != account:
            return False
        task['cancel'].set()
        return True


def cancel_job(job_id, account):
    with _lock:
        matches = [task for task in _tasks.values() if task['job_id'] == job_id and task['account'] == account]
        for task in matches:
            task['cancel'].set()
        return bool(matches)
