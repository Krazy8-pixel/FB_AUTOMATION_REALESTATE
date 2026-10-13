"""Cooperative cancellation of browser tasks without affecting tray lifetime."""
import functools
import inspect
import threading
import time
from uuid import uuid4

_lock = threading.RLock()
_local = threading.local()
_tasks = {}


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
                'started_at': time.time(), 'cancel': threading.Event()}
        with _lock:
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
    return driver


def cancellable_sleep(seconds):
    task = getattr(_local, 'task', None)
    if not task:
        time.sleep(seconds)
    elif task['cancel'].wait(seconds):
        check_cancelled()


def active_tasks(account):
    with _lock:
        return [{key: value for key, value in task.items() if key != 'cancel'} |
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
