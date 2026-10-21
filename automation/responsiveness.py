"""Bound UI lock waits and record slow requests without interrupting posting."""
import logging
from logging.handlers import RotatingFileHandler
import sys
import threading
import time
import traceback
from collections import deque

_wait_lock = threading.Lock()
_wait_events = deque(maxlen=30)


def in_request():
    try:
        from flask import has_request_context
        return has_request_context()
    except ImportError:
        return False


class AppBusyError(RuntimeError):
    pass


class _FreezeFileHandler(RotatingFileHandler):
    def handleError(self, record):
        pass  # Never recurse through stderr when diagnostic disk writes fail.


class RequestLock:
    """Background workers retain normal locking; UI requests wait at most 0.5s."""
    def __init__(self, name, reentrant=False, request_timeout=0.5):
        self.name = name
        self.request_timeout = request_timeout
        self._lock = threading.RLock() if reentrant else threading.Lock()

    def acquire(self, blocking=True, timeout=-1):
        if blocking and in_request():
            timeout = self.request_timeout if timeout < 0 else min(timeout, self.request_timeout)
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        if not self.acquire():
            with _wait_lock:
                _wait_events.append(f'UI lock wait timed out: {self.name}')
            raise AppBusyError(f'Timed out waiting for {self.name}')
        return self

    def __exit__(self, *args):
        self.release()


class RequestMonitor:
    """Capture Python stacks, never form values, cookies, or frame locals."""
    def __init__(self, folder, slow_seconds=10, clock=time.monotonic):
        self.clock = clock
        self.slow_seconds = slow_seconds
        self.pending = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_capture = float('-inf')
        self.handler = _FreezeFileHandler(folder / 'freeze-diagnostics.log',
                                          maxBytes=2_000_000, backupCount=2, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('[FREEZE] %(asctime)s %(message)s'))

    def begin(self, method, path):
        key = object()
        with self._lock:
            self.pending[key] = (self.clock(), threading.get_ident(), method, path)
        return key

    def end(self, key):
        with self._lock:
            self.pending.pop(key, None)

    def inspect(self):
        now = self.clock()
        with self._lock:
            slow = [value for value in self.pending.values() if now - value[0] >= self.slow_seconds]
            with _wait_lock:
                if (not slow and not _wait_events) or now - self._last_capture < 60:
                    return
                waits = list(_wait_events)
                _wait_events.clear()
            if not slow and not waits:
                return
            self._last_capture = now
        lines = ['Server responsiveness capture:', *waits, 'Slow server requests:']
        lines.extend(f'{method} {path} age={now-started:.1f}s thread={thread}'
                     for started, thread, method, path in slow)
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            lines.append(f'\nThread {ident} ({names.get(ident, "unknown")}):\n' + ''.join(traceback.format_stack(frame)))
        self.handler.handle(logging.LogRecord('freeze', logging.WARNING, '', 0, '\n'.join(lines), (), None))

    def start(self):
        if self._thread is None:
            def watch():
                while not self._stop.wait(2):
                    try:
                        self.inspect()
                    except Exception:
                        pass  # Monitoring must never alter an automation result.
            self._thread = threading.Thread(target=watch, daemon=True, name='request-monitor')
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.handler.close()


def install_request_monitor(app):
    from flask import g, request
    from automation.runtime_paths import data_dir
    try:
        monitor = RequestMonitor(data_dir())
    except OSError:
        return
    app.extensions['request_monitor'] = monitor
    monitor.start()

    @app.before_request
    def track_request():
        # Path only: queries and request bodies can contain private data.
        g.request_monitor_key = monitor.begin(request.method, request.path)

    @app.teardown_request
    def finish_request(error):
        monitor.end(g.pop('request_monitor_key', None))
