"""Runs outside the installed app so Windows permits replacing its files."""
import ctypes
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile


def safe_extract(archive, destination):
    root = destination.resolve()
    for item in archive.infolist():
        path = (root / item.filename).resolve()
        if not path.is_relative_to(root) or ':' in item.filename or (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError('Unsafe update archive path')
    archive.extractall(root)


def process_alive(pid):
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    return code.value == 259


def startup_healthy(path, version):
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('version') == version
    except (OSError, json.JSONDecodeError):
        return False


def main(plan_path):
    from automation.release_security import verify_manifest
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    folder = plan_path.parent
    manifest = verify_manifest((folder / 'manifest.json').read_bytes(), (folder / 'manifest.sig').read_bytes(), plan['public_key'])
    package = Path(plan['package']).resolve()
    with package.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
            raise ValueError('Update checksum mismatch')
    root = Path(plan['install_dir']).resolve()
    data = Path(plan['data_dir']).resolve()
    if not (root / 'TayyaraMotors.exe').is_file() or data.is_relative_to(root):
        raise ValueError('Invalid installation/data layout')
    staging = root.with_name(root.name + '.staging-' + str(os.getpid()))
    previous = root.with_name(root.name + '.previous-' + str(os.getpid()))
    if staging.exists() or previous.exists():
        raise ValueError('Update staging folder already exists')
    staging.mkdir()
    with zipfile.ZipFile(package) as archive:
        safe_extract(archive, staging)
    if not (staging / 'TayyaraMotors.exe').is_file():
        raise ValueError('The release is missing its executable')
    new_config = json.loads((staging / '_internal/app_config.json').read_text(encoding='utf-8'))
    if new_config['version'] != manifest['version']:
        raise ValueError('Release version mismatch')
    deadline = time.monotonic() + 90
    while process_alive(plan['pid']):
        if time.monotonic() > deadline:
            raise RuntimeError('The app did not close; no update was installed')
        time.sleep(.5)
    # Keep installer integration when replacing program files.
    if not (staging / 'Uninstall.exe').exists() and (root / 'Uninstall.exe').exists():
        shutil.copy2(root / 'Uninstall.exe', staging / 'Uninstall.exe')
    backup = folder / ('listings-before-' + manifest['version'] + '.db')
    root.rename(previous)
    reset_done = False
    try:
        if manifest.get('reset_databases') and (data / 'listings.db').exists():
            import sqlite3
            with closing(sqlite3.connect(data / 'listings.db')) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            reset_done = True
            for suffix in ['', '-wal', '-shm']:
                (data / ('listings.db' + suffix)).unlink(missing_ok=True)
        staging.rename(root)
        health = folder / 'healthy.json'
        health.unlink(missing_ok=True)
        child = subprocess.Popen([str(root / 'TayyaraMotors.exe'), '--health-file', str(health)])
        deadline = time.monotonic() + 90
        while not startup_healthy(health, manifest['version']):
            if child.poll() is not None or time.monotonic() > deadline:
                child.terminate()
                child.wait(timeout=15)
                raise RuntimeError('New version failed startup; restoring previous version')
            time.sleep(1)
    except Exception as exc:
        if root.exists():
            root.rename(staging)
        previous.rename(root)
        if reset_done and backup.exists():
            for suffix in ['', '-wal', '-shm']:
                (data / ('listings.db' + suffix)).unlink(missing_ok=True)
            shutil.copy2(backup, data / 'listings.db')
        try:
            (folder / 'last-update.json').write_text(json.dumps({'version': manifest['version'], 'success': False, 'error': str(exc)}), encoding='utf-8')
        except OSError:
            pass
        subprocess.Popen([str(root / 'TayyaraMotors.exe')])
        raise

    # The new app is healthy. Cleanup errors must never trigger rollback.
    try:
        (folder / 'last-update.json').write_text(json.dumps({'version': manifest['version'], 'success': True}), encoding='utf-8')
        # Keep one previous program version; remove only verified sibling backups.
        for old in root.parent.glob(root.name + '.previous-*'):
            if old != previous and (old / 'TayyaraMotors.exe').is_file():
                shutil.rmtree(old)
        package.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'Tayyara Motors update', 0x10)
