"""Create one private GitHub issue for each failed app operation.

The desktop app is a public client, so it never contains a GitHub client
secret. A GitHub App device-flow token is protected with Windows DPAPI.
"""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from uuid import uuid4
import webbrowser

import requests

from automation.runtime_paths import data_dir, resource_root

GITHUB_API = 'https://api.github.com'
DEVICE_CODE_URL = 'https://github.com/login/device/code'
ACCESS_TOKEN_URL = 'https://github.com/login/oauth/access_token'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
APP_CONFIG_FILE = resource_root() / 'app_config.json'
MEDIA_FIELDS = {'image_urls', 'image_paths', 'images', 'photos', 'downloaded_images',
                'video_urls', 'video_paths', 'videos', 'downloaded_videos'}
_lock = threading.RLock()
_login = {'state': 'disconnected'}
_retry_thread = None
_active_reports = set()


def _settings():
    try:
        config = json.loads(APP_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception:
        config = {}
    values = config.get('github_reports') or {}
    try:
        runtime_values = json.loads((data_dir('github_reports') / 'app.json').read_text(encoding='utf-8'))
    except Exception:
        runtime_values = {}
    repository = str(runtime_values.get('repository') or values.get('repository') or 'Krazy8-pixel/Fb_automation').strip()
    client_id = str(runtime_values.get('client_id') or values.get('client_id') or os.getenv('FB_AUTOMATION_GITHUB_APP_CLIENT_ID') or '').strip()
    return repository, client_id


def _configured_repository(value):
    repository = str(value or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Enter a GitHub repository in owner/name format.')
    return repository


def _report_fields(value):
    if isinstance(value, dict):
        return {str(key): _report_fields(item) for key, item in value.items()
                if str(key).casefold() not in MEDIA_FIELDS and not str(key).startswith('_')}
    if isinstance(value, (list, tuple)):
        return [_report_fields(item) for item in value]
    return value


def save_settings(repository, client_id):
    repository = _configured_repository(repository)
    client_id = str(client_id or '').strip()
    if client_id and not re.fullmatch(r'[A-Za-z0-9_.-]{8,120}', client_id):
        raise ValueError('Enter the public GitHub App Client ID from its settings page.')
    current_repository, current_client_id = _settings()
    if (repository.casefold() != current_repository.casefold()
            or (client_id and current_client_id and client_id != current_client_id)):
        _clear_token()
        with _lock:
            _login.clear()
            _login.update(state='disconnected')
    target = data_dir('github_reports') / 'app.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'repository': repository, 'client_id': client_id}, indent=2), encoding='utf-8')
    temporary.replace(target)
    for queued_report in (data_dir('github_reports') / 'pending').glob('*.json'):
        threading.Thread(target=_deliver, args=(queued_report,), name=f'github-report-retry-{queued_report.stem[:8]}', daemon=True).start()
    return status()


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _protect(data):
    if os.name != 'nt':
        raise RuntimeError('GitHub report credentials can only be saved with Windows DPAPI.')
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    if not crypt.CryptProtectData(ctypes.byref(source), 'Tayyara Motors GitHub Reports', None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def _unprotect(data):
    if os.name != 'nt':
        raise RuntimeError('GitHub report credentials can only be read with Windows DPAPI.')
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def _token_path():
    return data_dir('github_reports') / 'github_token.dpapi'


def _save_token(token):
    if sys.platform == 'darwin':
        import keyring
        keyring.set_password('FBAutomationRealEstate', 'github_reports', json.dumps(token))
        return
    path = _token_path()
    path.write_bytes(_protect(json.dumps(token).encode('utf-8')))


def _load_token():
    if sys.platform == 'darwin':
        try:
            import keyring
            saved = keyring.get_password('FBAutomationRealEstate', 'github_reports')
            return json.loads(saved) if saved else None
        except Exception:
            return None
    try:
        return json.loads(_unprotect(_token_path().read_bytes()).decode('utf-8'))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _clear_token():
    if sys.platform == 'darwin':
        try:
            import keyring
            keyring.delete_password('FBAutomationRealEstate', 'github_reports')
        except Exception:
            pass
    _token_path().unlink(missing_ok=True)


def _api_headers(token):
    return {'Accept': 'application/vnd.github+json', 'Authorization': f"Bearer {token}",
            'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'Tayyara-Motors-Failure-Reporter'}


def status():
    repository, client_id = _settings()
    token = _load_token()
    queued = len(list((data_dir('github_reports') / 'pending').glob('*.json')))
    with _lock:
        login = dict(_login)
    last_error = ''
    pending_dir = data_dir('github_reports') / 'pending'
    for queued_report in sorted(pending_dir.glob('*.json'), key=lambda item: item.stat().st_mtime, reverse=True)[:1]:
        try:
            last_error = str(json.loads(queued_report.read_text(encoding='utf-8')).get('last_error') or '')
        except Exception:
            pass
    return {'configured': bool(client_id), 'client_id': client_id, 'repository': repository,
            'connected': bool(token and token.get('access_token')),
            'github_user': (token or {}).get('login', ''), 'queued_reports': queued,
            'last_error': last_error, 'login': login}


def connect():
    global _login
    repository, client_id = _settings()
    if not client_id:
        return {'ok': False, 'message': 'GitHub App client ID has not been configured yet.'}, 409
    try:
        response = requests.post(DEVICE_CODE_URL, headers={'Accept': 'application/json'},
                                 data={'client_id': client_id}, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get('device_code') or not data.get('user_code'):
            raise RuntimeError(data.get('error_description') or data.get('error') or 'GitHub did not return a device code.')
    except Exception as exc:
        return {'ok': False, 'message': f'Could not start GitHub authorization: {exc}'}, 502
    with _lock:
        _login = {'state': 'waiting', 'user_code': data['user_code'],
                  'verification_uri': data.get('verification_uri') or 'https://github.com/login/device',
                  'expires_at': time.time() + int(data.get('expires_in') or 900), 'message': 'Enter the code on GitHub.'}
    try:
        webbrowser.open(_login['verification_uri'])
    except Exception:
        pass
    worker = threading.Thread(target=_poll_device_code,
                              args=(data['device_code'], client_id, repository, int(data.get('interval') or 5)),
                              name='github-report-device-auth', daemon=True)
    worker.start()
    return {'ok': True, **status()}, 200


def _poll_device_code(device_code, client_id, repository, interval):
    global _login
    deadline = time.time() + 900
    while time.time() < deadline:
        time.sleep(max(5, interval))
        try:
            response = requests.post(ACCESS_TOKEN_URL, headers={'Accept': 'application/json'},
                                     data={'client_id': client_id, 'device_code': device_code,
                                           'grant_type': DEVICE_GRANT}, timeout=15)
            data = response.json()
        except Exception as exc:
            with _lock:
                _login.update(state='error', message=f'GitHub authorization request failed: {exc}')
            return
        if data.get('error') == 'authorization_pending':
            continue
        if data.get('error') == 'slow_down':
            interval += 5
            continue
        if data.get('error'):
            with _lock:
                _login.update(state='error', message=data.get('error_description') or data['error'])
            return
        access_token = data.get('access_token')
        if not access_token:
            with _lock:
                _login.update(state='error', message='GitHub authorization returned no access token.')
            return
        try:
            headers = _api_headers(access_token)
            user_response = requests.get(f'{GITHUB_API}/user', headers=headers, timeout=15)
            user_response.raise_for_status()
            repo_details = _validate_private_repository(repository, headers)
            if repo_details.get('full_name', '').casefold() != repository.casefold():
                raise RuntimeError(f'The GitHub App token could not access {repository}. Install the app on that repository.')
            token = {'access_token': access_token, 'refresh_token': data.get('refresh_token'),
                     'expires_at': time.time() + int(data['expires_in']) if data.get('expires_in') else None,
                     'refresh_expires_at': time.time() + int(data['refresh_token_expires_in']) if data.get('refresh_token_expires_in') else None,
                     'login': user_response.json().get('login', '')}
            _save_token(token)
            with _lock:
                _login = {'state': 'connected', 'message': f"Connected as {token['login']}.", 'user_code': ''}
            retry_pending()
            return
        except Exception as exc:
            with _lock:
                _login.update(state='error', message=f'GitHub connected but repository access failed: {exc}')
            return
    with _lock:
        _login.update(state='expired', message='The GitHub code expired. Start Connect again for a new code.')


def disconnect():
    _clear_token()
    with _lock:
        _login.clear()
        _login.update(state='disconnected')
    return status()


def _valid_access_token():
    token = _load_token()
    if not token:
        return None
    if token.get('expires_at') and token['expires_at'] < time.time() + 60:
        refresh_token = token.get('refresh_token')
        repository, client_id = _settings()
        if not refresh_token or not client_id:
            _clear_token()
            return None
        response = requests.post(ACCESS_TOKEN_URL, headers={'Accept': 'application/json'},
                                 data={'client_id': client_id, 'grant_type': 'refresh_token',
                                       'refresh_token': refresh_token}, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get('access_token'):
            _clear_token()
            return None
        token.update(access_token=data['access_token'], refresh_token=data.get('refresh_token', refresh_token),
                     expires_at=time.time() + int(data['expires_in']) if data.get('expires_in') else None,
                     refresh_expires_at=time.time() + int(data['refresh_token_expires_in']) if data.get('refresh_token_expires_in') else None)
        _save_token(token)
    repository, _ = _settings()
    try:
        repo_details = _validate_private_repository(repository, _api_headers(token['access_token']))
        if not repo_details.get('full_name', '').casefold() == repository.casefold():
            return None
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in {401, 403, 404}:
            _clear_token()
            return None
        raise
    return token


def _validate_private_repository(repository, headers):
    response = requests.get(f'{GITHUB_API}/repos/{repository}', headers=headers, timeout=15)
    response.raise_for_status()
    details = response.json()
    if details.get('private') is not True:
        raise RuntimeError(f'{repository} is public. Failure reports include private listing details; choose a private repository.')
    return details


def _report_body(report):
    metadata = report.get('metadata') or {}
    details = report.get('data') or {}
    lines = ['## Failure details', '',
             f"- **When:** {report.get('timestamp')}",
             f"- **Action:** {report.get('action')}",
             f"- **Account:** {report.get('account') or '(empty)'}",
             f"- **Listing / job:** {report.get('reference') or '(empty)'}",
             f"- **Error:** {report.get('error') or '(no error text returned)'}", '']
    lines.extend(['## Operation metadata', '', '```json', json.dumps(metadata, ensure_ascii=False, indent=2, default=str), '```', '',
                  '## Listing fields (empty values are shown as `(empty)`)', '', '| Field | Value |', '| --- | --- |'])
    for key, value in details.items():
        if key.casefold() in MEDIA_FIELDS or key.startswith('_'):
            continue
        rendered = '(empty)' if value is None or value == '' else json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list, tuple)) else str(value)
        rendered = rendered.replace('|', '\\|').replace('\n', '<br>')
        lines.append(f'| `{key}` | {rendered} |')
    media = report.get('media') or {}
    lines.extend(['', '## Media (files are not attached)', '',
                  f"- Photos: {media.get('photo_count', 0)}",
                  f"- Videos: {media.get('video_count', 0)}",
                  f"- Photo files: {', '.join(media.get('photos') or []) or '(none)'}",
                  f"- Video files: {', '.join(media.get('videos') or []) or '(none)'}", '',
                  '## Operation logs', '',
                  'The complete operation logs and unabridged payload are attached below as a GitHub issue artifact.', '',
                  '```text', (report.get('logs') or '(No captured logs.)')[-8000:], '```'])
    body = '\n'.join(lines)
    return body[-60000:]


def _find_existing_issue(repository, token, report_id):
    marker = f'[FBA-{report_id[:10]}]'
    response = requests.get(f'{GITHUB_API}/repos/{repository}/issues',
                            headers=_api_headers(token), params={'state': 'all', 'per_page': 100}, timeout=20)
    response.raise_for_status()
    for issue in response.json():
        if marker in str(issue.get('title') or ''):
            return issue
    return None


def _report_artifact(report):
    return json.dumps({
        'report_id': report.get('id'), 'timestamp': report.get('timestamp'),
        'action': report.get('action'), 'account': report.get('account'),
        'reference': report.get('reference'), 'error': report.get('error'),
        'metadata': report.get('metadata') or {}, 'fields': _report_fields(report.get('data') or {}),
        'media': report.get('media') or {}, 'complete_operation_logs': report.get('logs') or '',
    }, ensure_ascii=False, indent=2, default=str)


def _queue_path(report_id):
    folder = data_dir('github_reports', 'pending')
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f'{report_id}.json'


def submit_failure(action, account='', reference='', error='', data=None, logs='', metadata=None, media=None):
    """Persist an independent issue report, then attempt delivery immediately."""
    report_id = uuid4().hex
    report_data = dict(data or {})
    for field in ('title', 'vehicle_type', 'year', 'make', 'model', 'mileage', 'price', 'body_style',
                  'exterior_color', 'interior_color', 'vehicle_condition', 'fuel_type', 'transmission',
                  'description', 'location', 'clean_title', 'vin', 'trim', 'category', 'item_condition', 'sku'):
        report_data.setdefault(field, '')
    report = {'id': report_id, 'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'action': str(action or 'operation').strip(), 'account': str(account or ''),
              'reference': str(reference or ''), 'error': str(error or ''), 'data': report_data,
              'logs': str(logs or ''), 'metadata': metadata or {}, 'media': media or {},
              'attempts': 0, 'created_at': time.time()}
    try:
        path = _queue_path(report_id)
        path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        threading.Thread(target=_deliver, args=(path,), name=f'github-failure-{report_id[:8]}', daemon=True).start()
    except Exception as exc:
        try:
            from automation.terminal_log import _record
            _record(f'[GitHubReports] Could not queue failure report {report_id}: {exc}\n')
        except Exception:
            pass
    return report_id


def _deliver(path):
    key = str(path)
    with _lock:
        if key in _active_reports:
            return
        _active_reports.add(key)
    try:
        _deliver_once(path)
    finally:
        with _lock:
            _active_reports.discard(key)


def _deliver_once(path):
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        token = _valid_access_token()
        if not token:
            return
        repository, _ = _settings()
        heading = f"[FBA-{report['id'][:10]}] {report['action']} failed: {report.get('reference') or 'Untitled'}"
        if len(heading) > 240:
            heading = heading[:237] + '...'
        if not report.get('issue_number'):
            try:
                created = _find_existing_issue(repository, token['access_token'], report['id'])
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    _clear_token()
                    return
                raise
            if not created:
                response = requests.post(f'{GITHUB_API}/repos/{repository}/issues', headers=_api_headers(token['access_token']),
                                         json={'title': heading, 'body': _report_body(report)}, timeout=30)
                if response.status_code == 401:
                    _clear_token()
                    return
                response.raise_for_status()
                created = response.json()
            report['issue_number'] = created.get('number')
            report['issue_url'] = created.get('html_url')
            path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        artifacts_url = f"{GITHUB_API}/repos/{repository}/issues/{report['issue_number']}/artifacts"
        existing_artifacts = requests.get(artifacts_url, headers=_api_headers(token['access_token']),
                                          params={'type': 'generic', 'per_page': 100}, timeout=20)
        existing_artifacts.raise_for_status()
        duplicate_artifact = next((item for item in existing_artifacts.json()
                                   if item.get('name') == 'Complete failure report and logs'
                                   and report['id'] in str(item.get('body') or '')), None)
        if not duplicate_artifact:
            response = requests.post(
                artifacts_url,
                headers=_api_headers(token['access_token']),
                json={'type': 'generic', 'name': 'Complete failure report and logs', 'body': _report_artifact(report)},
                timeout=30,
            )
            response.raise_for_status()
        path.unlink(missing_ok=True)
        try:
            from automation.terminal_log import _record
            _record(f"[GitHubReports] Sent report {report['id']} as issue #{report.get('issue_number')}\n")
        except Exception:
            pass
    except Exception as exc:
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
            report['attempts'] = int(report.get('attempts') or 0) + 1
            report['last_error'] = str(exc)
            report['last_attempt_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        except Exception:
            pass


def retry_pending():
    folder = data_dir('github_reports', 'pending')
    for path in list(folder.glob('*.json')):
        threading.Thread(target=_deliver, args=(path,), name=f'github-report-retry-{path.stem[:8]}', daemon=True).start()


def start_retry_worker():
    global _retry_thread
    if _retry_thread and _retry_thread.is_alive():
        return
    def worker():
        while True:
            time.sleep(300)
            retry_pending()
    _retry_thread = threading.Thread(target=worker, name='github-report-retry', daemon=True)
    _retry_thread.start()


def begin_log_capture():
    try:
        from automation.terminal_log import snapshot
        return snapshot()['cursor']
    except Exception:
        return 0


def collect_logs(cursor):
    try:
        from automation.terminal_log import snapshot
        return snapshot(int(cursor or 0))['text'][-50000:]
    except Exception:
        return ''


def media_summary(data):
    data = data or {}
    photos = []
    videos = []
    for key in ('image_urls', 'image_paths', 'images', 'photos', 'downloaded_images'):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            photos.extend(str(item.get('path') or item.get('name') or '') if isinstance(item, dict) else str(item) for item in value if item)
    for key in ('video_paths', 'video_urls', 'videos', 'downloaded_videos'):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            videos.extend(str(item.get('path') or item.get('name') or '') if isinstance(item, dict) else str(item) for item in value if item)
    photos = list(dict.fromkeys(photos))
    videos = list(dict.fromkeys(videos))
    return {'photo_count': len(photos), 'video_count': len(videos),
            'photos': [Path(item).name if re.match(r'^[A-Za-z]:\\|^/', item) else item for item in photos],
            'videos': [Path(item).name if re.match(r'^[A-Za-z]:\\|^/', item) else item for item in videos]}
