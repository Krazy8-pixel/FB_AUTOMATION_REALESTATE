"""Manual GitHub release updates with pinned Ed25519 signatures."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import requests
from packaging.version import Version
from automation.release_security import verify_manifest
from automation.runtime_paths import resource_root, data_dir

_lock = threading.RLock()
_status = {'state': 'idle', 'message': 'Click Check for Updates when notified.', 'available': False}
_ready = None
exit_callback = None


def config():
    return json.loads((resource_root() / 'app_config.json').read_text(encoding='utf-8'))


def status():
    with _lock:
        return dict(_status, current_version=config()['version'])


def set_status(**values):
    with _lock:
        _status.update(values)



def fetch(url, **kwargs):
    response = requests.get(url, timeout=(10, 60), **kwargs)
    response.raise_for_status()
    return response


def check():
    global _ready
    with _lock:
        if _status['state'] in {'checking', 'downloading', 'installing'}:
            return
        _status.update(state='checking', message='Checking GitHub Releases...', available=False)
    def worker():
        global _ready
        try:
            cfg = config()
            release = fetch('https://api.github.com/repos/' + cfg['repository'] + '/releases/latest', headers={'Accept': 'application/vnd.github+json'}).json()
            assets = {item['name']: item['browser_download_url'] for item in release['assets']}
            raw = fetch(assets['manifest.json']).content
            signature = fetch(assets['manifest.sig']).content
            manifest = verify_manifest(raw, signature, cfg['update_public_key'])
            available = Version(manifest['version']) > Version(cfg['version'])
            _ready = (manifest, raw, signature, assets[manifest['asset']]) if available else None
            set_status(state='available' if available else 'current', available=available, latest_version=manifest['version'], message='An update is available.' if available else 'You have the latest version.', resets_listings=bool(manifest.get('reset_databases')))
        except Exception as exc:
            set_status(state='error', available=False, message='Could not check for updates: ' + str(exc))
    threading.Thread(target=worker, daemon=True).start()


def install():
    from web import routes
    from automation.maintenance import busy
    with _lock:
        if not _ready or _status['state'] != 'available':
            raise ValueError('Check for an available update first.')
        if not getattr(sys, 'frozen', False) or not exit_callback:
            raise ValueError('Updates can be installed from the packaged desktop app.')
        if busy(routes):
            raise ValueError('Wait for current automation to finish before updating.')
        routes.desktop_update_pending = True
        routes.scheduler.pause()
        _status.update(state='downloading', message='Downloading and verifying the update...', available=False)

    def worker():
        try:
            manifest, raw, signature, url = _ready
            folder = data_dir('updates')
            package = folder / 'update.zip'
            digest = hashlib.sha256()
            with fetch(url, stream=True) as response, package.open('wb') as stream:
                for block in response.iter_content(1024 * 1024):
                    if block:
                        stream.write(block)
                        digest.update(block)
            if digest.hexdigest() != manifest['sha256']:
                raise ValueError('The downloaded update failed verification.')
            (folder / 'manifest.json').write_bytes(raw)
            (folder / 'manifest.sig').write_bytes(signature)
            plan = {'install_dir': str(Path(sys.executable).resolve().parent), 'data_dir': str(data_dir()), 'package': str(package), 'public_key': config()['update_public_key'], 'pid': os.getpid()}
            (folder / 'plan.json').write_text(json.dumps(plan), encoding='utf-8')
            updater = folder / 'TayyaraUpdater.exe'
            shutil.copy2(resource_root() / 'TayyaraUpdater.exe', updater)
            from automation.desktop_state import checkpoint
            checkpoint()
            set_status(state='installing', message='Installing update. The app will restart.')
            subprocess.Popen([str(updater), str(folder / 'plan.json')], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            exit_callback()
        except Exception as exc:
            routes.desktop_update_pending = False
            routes.scheduler.resume()
            set_status(state='available', available=True, message='Update failed; your installed app is unchanged. ' + str(exc))
    threading.Thread(target=worker, daemon=True).start()
