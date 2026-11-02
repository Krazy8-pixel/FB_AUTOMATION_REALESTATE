"""User-initiated updates with pinned Ed25519 signatures."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from uuid import uuid4

import requests
from packaging.version import Version
from automation.release_security import verify_manifest
from automation.runtime_paths import resource_root, data_dir

_lock = threading.RLock()
_status = {'state': 'idle', 'message': 'Click Check for Updates when notified.', 'available': False}
_ready = None
exit_callback = None


def _automation_busy(routes):
    from automation.maintenance import busy
    from automation.task_control import has_active_tasks
    return busy(routes) or has_active_tasks()


def config():
    return json.loads((resource_root() / 'app_config.json').read_text(encoding='utf-8'))


def status():
    with _lock:
        return dict(_status, current_version=config()['version'])


def set_status(**values):
    with _lock:
        _status.update(values)


def download_progress(downloaded, total=0):
    percent = min(100, int(downloaded * 100 / total)) if total else None
    amount = f'{downloaded / 1_000_000:.1f} MB'
    if total:
        amount += f' / {total / 1_000_000:.1f} MB ({percent}%)'
    set_status(downloaded_bytes=downloaded, total_bytes=total,
               download_percent=percent, message='Downloading update: ' + amount)


def download_package(url, package, total=0):
    digest = hashlib.sha256()
    downloaded = 0
    with fetch(url, stream=True) as response, package.open('wb') as stream:
        if not total:
            try:
                total = max(0, int(response.headers.get('Content-Length', 0)))
            except (ValueError, TypeError):
                total = 0
        download_progress(0, total)
        for block in response.iter_content(256 * 1024):
            if block:
                stream.write(block)
                digest.update(block)
                downloaded += len(block)
                download_progress(downloaded, total)
    if total and downloaded != total:
        raise ValueError('The download was incomplete. Please try again.')
    return digest.hexdigest()



def fetch(url, **kwargs):
    response = requests.get(url, timeout=(10, 60), **kwargs)
    response.raise_for_status()
    return response


def check():
    global _ready
    with _lock:
        if _status['state'] in {'checking', 'downloading', 'verifying', 'installing'}:
            return
        _status.update(state='checking', message='Checking GitHub Releases...', available=False,
                       downloaded_bytes=0, total_bytes=0, download_percent=None)
        _ready = None
    def worker():
        global _ready
        try:
            cfg = config()
            release = fetch('https://api.github.com/repos/' + cfg['repository'] + '/releases/latest', headers={'Accept': 'application/vnd.github+json'}).json()
            assets = {item['name']: item['browser_download_url'] for item in release['assets']}
            raw = fetch(assets['manifest.json']).content
            signature = fetch(assets['manifest.sig']).content
            manifest = verify_manifest(raw, signature, cfg['update_public_key'])
            size = next((item.get('size', 0) for item in release['assets'] if item['name'] == manifest['asset']), 0)
            available = Version(manifest['version']) > Version(cfg['version'])
            _ready = (manifest, raw, signature, assets[manifest['asset']]) if available else None
            set_status(state='available' if available else 'current', available=available, latest_version=manifest['version'], message=f'An update is available ({size / 1_000_000:.1f} MB).' if available and size else 'An update is available.' if available else 'You have the latest version.', total_bytes=size, resets_listings=bool(manifest.get('reset_databases')))
        except Exception as exc:
            set_status(state='error', available=False, message='Could not check for updates: ' + str(exc))
    threading.Thread(target=worker, daemon=True).start()


def install():
    from web import routes
    with _lock:
        if not _ready or _status['state'] != 'available':
            raise ValueError('Check for an available update first.')
        if not getattr(sys, 'frozen', False) or not exit_callback:
            raise ValueError('Updates can be installed from the packaged desktop app.')
        if _automation_busy(routes):
            raise ValueError('Wait for current automation to finish before updating.')
        routes.desktop_update_pending = True
        routes.scheduler.pause()
        _status.update(state='downloading', message='Starting download...', available=False,
                       downloaded_bytes=0, download_percent=0)

    def worker():
        try:
            manifest, raw, signature, url = _ready
            folder = data_dir('updates')
            package = folder / 'update.zip'
            digest = download_package(url, package, status().get('total_bytes', 0))
            set_status(state='verifying', message='Download complete. Verifying update...')
            if digest != manifest['sha256']:
                raise ValueError('The downloaded update failed verification.')
            (folder / 'manifest.json').write_bytes(raw)
            (folder / 'manifest.sig').write_bytes(signature)
            plan = {'install_dir': str(Path(sys.executable).resolve().parent), 'data_dir': str(data_dir()), 'package': str(package), 'public_key': config()['update_public_key'], 'pid': os.getpid()}
            (folder / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
            # A previous updater may still be cleaning up after restarting the UI.
            updater = folder / ('RealEstateUpdater-' + uuid4().hex + '.exe')
            shutil.copy2(resource_root() / 'RealEstateUpdater.exe', updater)
            from automation.desktop_state import checkpoint
            checkpoint()
            set_status(state='installing', message='Installing update. The app will restart.')
            from automation.update_process import launch_independent
            launch_independent([str(updater), str(folder / 'plan.json')], folder)
            exit_callback()
        except Exception as exc:
            routes.desktop_update_pending = False
            routes.scheduler.resume()
            set_status(state='available', available=True, message='Update failed; your installed app is unchanged. ' + str(exc))
    threading.Thread(target=worker, daemon=True).start()
