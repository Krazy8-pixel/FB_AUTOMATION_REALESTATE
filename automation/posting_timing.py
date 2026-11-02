"""Persist actual job-run duration for successful posting results."""
import math
import time
from datetime import datetime


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value):
    """Convert an epoch value or datetime to epoch seconds."""
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    return _finite_number(value)


def request_origin(value_ms=None, received_at=None):
    """Use a plausible browser click timestamp, otherwise request arrival.

    Epoch seconds survive restarts and include deliberate schedule/queue time.
    A stale page value older than one day or a future/skewed clock is not used.
    """
    received = time.time() if received_at is None else float(received_at)
    milliseconds = _finite_number(value_ms)
    clicked = milliseconds / 1000.0 if milliseconds is not None else None
    if clicked is None or clicked <= 0 or clicked > received + 2 or received - clicked > 86400:
        return received
    return min(clicked, received)


def complete_posting(meta, success, completed_at=None):
    """Set elapsed seconds from job start through completion on success."""
    meta.pop('posting_duration_seconds', None)
    meta.pop('posting_duration_text', None)
    if not success:
        return
    start = _timestamp(meta.get('posting_started_at'))
    if start is None:
        start = _timestamp(meta.get('started_at'))
    end = _timestamp(completed_at)
    if end is None:
        end = _timestamp(meta.get('posted_at')) or _timestamp(meta.get('finished_at'))
    if end is None:
        end = time.time()
    if start is None or end is None or start <= 0 or end < start:
        return
    meta['posting_duration_seconds'] = round(end - start, 3)


def format_duration(seconds):
    value = _finite_number(seconds)
    if value is None or value < 0:
        return ''
    total = int(value)
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, seconds = divmod(total, 60)
    pieces = []
    for count, unit in ((days, 'd'), (hours, 'h'), (minutes, 'm')):
        if count:
            pieces.append(f'{count}{unit}')
    pieces.append(f'{seconds}s')
    return ' '.join(pieces)


def duration_fields(meta, status=None):
    """Keep unfinished/failed and historical untimed rows free of duration."""
    if (status or meta.get('status')) not in {'posted', 'reposted', 'done'}:
        return {}
    start = _timestamp(meta.get('posting_started_at'))
    if start is None:
        start = _timestamp(meta.get('started_at'))
    end = _timestamp(meta.get('posted_at')) or _timestamp(meta.get('finished_at'))
    if start is not None and end is not None and start > 0 and end >= start:
        # Recompute from job timestamps so older click-to-completion values
        # don't keep showing scheduled delay as posting time.
        seconds = round(end - start, 3)
    else:
        seconds = _finite_number(meta.get('posting_duration_seconds'))
    if seconds is None or seconds < 0:
        return {}
    return {'posting_duration_seconds': seconds,
            'posting_duration_text': format_duration(seconds)}
