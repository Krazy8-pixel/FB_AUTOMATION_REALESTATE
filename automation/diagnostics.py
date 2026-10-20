"""Readable, timestamped workflow markers; diagnostics never change outcomes."""
from datetime import datetime, timezone
from functools import wraps
import json
from uuid import uuid4


def event(action, status, **details):
    try:
        print(f'[CHECK] {datetime.now(timezone.utc).isoformat(timespec="seconds")} '
              f'{action} {status} {json.dumps(details, ensure_ascii=False, default=str)}', flush=True)
    except Exception:
        pass


def traced(action):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            run_id = uuid4().hex[:12]
            account = kwargs.get('account') or (args[1] if action == 'POST' and len(args) > 1 else args[0] if args else '')
            event(action, 'START', run_id=run_id, account=account)
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                event(action, 'STOPPED', run_id=run_id, account=account,
                      error_type=type(exc).__name__, error=str(exc))
                raise
            event(action, 'RETURNED_FALSE' if result is False else 'FINISHED',
                  run_id=run_id, account=account,
                  records=len(result) if isinstance(result, list) else None)
            return result
        return run
    return decorate
