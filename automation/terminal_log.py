"""Thread-safe, bounded mirror of application stdout and stderr."""
from collections import deque
import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from automation.responsiveness import RequestLock

_disk_handler = None


class QuietFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # A full disk or locked log must never interrupt automation or recurse.
        pass


def start_disk_capture(folder=None):
    global _disk_handler
    with _lock:
        if _disk_handler is not None:
            return
        try:
            from automation.runtime_paths import data_dir
            from pathlib import Path
            directory = Path(folder) if folder is not None else data_dir()
            directory.mkdir(parents=True, exist_ok=True)
            handler = QuietFileHandler(directory / 'diagnostics.log', maxBytes=5_000_000,
                                       backupCount=2, encoding='utf-8')
            handler.terminator = ''
            handler.setFormatter(logging.Formatter('%(message)s'))
            _disk_handler = handler
        except Exception:
            pass


def export_text():
    """Read bounded log history, including previous launches, without client data files."""
    from pathlib import Path
    from datetime import datetime, timezone
    from automation.diagnostics import _readable_summary
    with _lock:
        parts = ['Real Estate Automation diagnostic report\n',
                 f'Exported (UTC): {datetime.now(timezone.utc).isoformat()}\n\n']
        history = []
        if _disk_handler is not None:
            _disk_handler.flush()
            path = Path(_disk_handler.baseFilename)
            for name in (str(path) + '.2', str(path) + '.1', str(path)):
                try:
                    history.append(Path(name).read_text(encoding='utf-8', errors='replace'))
                except OSError:
                    pass
        else:
            history.append(snapshot()['text'])
        # Slow-request thread captures use a separate writer so a blocked
        # stdout/logging lock cannot prevent collecting evidence of a freeze.
        try:
            from automation.runtime_paths import data_dir
            freeze = data_dir() / 'freeze-diagnostics.log'
            for suffix in ('.2', '.1', ''):
                source = Path(str(freeze) + suffix)
                if source.is_file():
                    history.append(source.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            pass
        detailed = ''.join(history)
        # Keep group import out of the real-estate report while preserving
        # other group-related activity such as publishing posts to groups.
        detailed = re.sub(
            r'^.*(?:\[CHECK\]\s+\S+\s+GROUP_IMPORT(?:_[A-Z_]+)?\s+\S+\s+\{.*\}|\[Groups\]\[(?:Info|BuySell)\].*)(?:\r?\n|$)',
            '', detailed, flags=re.MULTILINE,
        )
        parts.append(_readable_summary(detailed))
        parts.extend(['DETAILED ACTIVITY LOG\n',
                      'The lines below show recorded steps and messages in time order. Search for FAILED, STOPPED, REVIEW, or an account/listing name to investigate.\n\n',
                      detailed])
        return ''.join(parts)

_lock = RequestLock('terminal_log', reentrant=True)
_chunks = deque()
_sequence = 0
_size = 0
MAX_CHARS = 2_000_000


def _record(text):
    global _sequence, _size
    with _lock:
        if _disk_handler is not None:
            try:
                _disk_handler.handle(logging.LogRecord('diagnostics', logging.INFO, '', 0, text, (), None))
            except Exception:
                pass
        # Split large writes so a single traceback cannot evict the entire tail.
        for start in range(0, len(text), 8192):
            chunk = text[start:start + 8192]
            _sequence += 1
            _chunks.append((_sequence, chunk))
            _size += len(chunk)
        while _size > MAX_CHARS:
            _size -= len(_chunks.popleft()[1])


class TerminalStream:
    def __init__(self, original):
        self.original = original

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError('write() requires a string')
        _record(text)
        if self.original is not None:
            return self.original.write(text)
        return len(text)

    def flush(self):
        if self.original is not None:
            self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


def install():
    """Preserve console/file output while mirroring it for the web viewer."""
    start_disk_capture()
    replacements = {}
    for name in ('stdout', 'stderr'):
        original = getattr(sys, name)
        if not isinstance(original, TerminalStream):
            wrapper = TerminalStream(original)
            replacements[id(original)] = wrapper
            setattr(sys, name, wrapper)
    if replacements:
        try:
            import json
            from automation.runtime_paths import resource_root
            from automation.diagnostics import event
            version = json.loads((resource_root() / 'app_config.json').read_text(encoding='utf-8')).get('version', 'unknown')
            event('SESSION', 'START', version=version, python=sys.version.split()[0], platform=sys.platform)
        except Exception:
            pass
    # Logging handlers may have captured stderr before this module was installed.
    loggers = [logging.getLogger()] + [
        logger for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    for logger in loggers:
        for handler in logger.handlers:
            replacement = replacements.get(id(getattr(handler, 'stream', None)))
            if replacement is not None and isinstance(handler, logging.StreamHandler):
                handler.setStream(replacement)


def snapshot(after=0):
    with _lock:
        reset = bool(_chunks and (after < _chunks[0][0] - 1 or after > _sequence))
        return {
            'text': ''.join(text for sequence, text in _chunks if reset or sequence > after),
            'cursor': _sequence,
            'reset': reset,
        }
