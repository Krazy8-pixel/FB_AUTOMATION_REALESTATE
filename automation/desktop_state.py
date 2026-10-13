"""Durable desktop job metadata. Data is never shipped in a release."""
import json
import os
import threading
from datetime import datetime
from automation.runtime_paths import data_dir

_lock = threading.RLock()
_last_saved = None


def encode(value):
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    raise TypeError(type(value).__name__)


def decode(value):
    if set(value) == {"__datetime__"}:
        return datetime.fromisoformat(value["__datetime__"])
    return value


def save_state(routes):
    global _last_saved
    if os.getenv('FB_AUTOMATION_DESKTOP') != '1':
        return
    with _lock, routes.scheduled_posts_lock, routes.repost_jobs_lock:
        path = data_dir() / 'desktop_jobs.json'
        raw = json.dumps({'posts': routes.scheduled_posts, 'reposts': routes.repost_jobs}, default=encode)
        if raw == _last_saved and path.exists():
            return
        temporary = path.with_suffix('.tmp')
        temporary.write_text(raw, encoding='utf-8')
        os.replace(temporary, path)
        _last_saved = raw


def restore_state(routes):
    path = data_dir() / 'desktop_jobs.json'
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding='utf-8'), object_hook=decode)
    for target, key in [(routes.scheduled_posts, 'posts'), (routes.repost_jobs, 'reposts')]:
        target.update(payload.get(key, {}))
        for job in target.values():
            if job.get('status') in {'posting', 'reading', 'deleting', 'waiting'}:
                job.update(status='failed', error='The app stopped during this job. Review Facebook before retrying to avoid duplicates.')


def checkpoint():
    from web import routes
    save_state(routes)
