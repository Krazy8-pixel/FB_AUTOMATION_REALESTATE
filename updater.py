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
from automation.update_process import launch_independent


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


def release_install_directory(root, folder):
    # Windows also locks a directory used as a process's current directory.
    # Older launchers inherited the installed app's working directory.
    if Path.cwd().resolve().is_relative_to(root.resolve()):
        os.chdir(folder)


def rename_with_retry(source, destination, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            source.rename(destination)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33):
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f'Windows is still using {source.name}. Close all FB Automation Real Estate '
                    'windows and try again, or restart Windows and run the latest installer. '
                    'Your saved listings are stored separately.'
                ) from exc
            time.sleep(.5)


def move_program_files(source, destination, moved, executable_last=False):
    """Move files individually so an open folder cannot block the update."""
    paths = [path for path in source.rglob('*') if path.is_file()]
    paths.sort(key=lambda path: (
        (path.name.casefold() == 'tayyaramotors.exe') == executable_last, str(path)))
    for path in paths:
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        rename_with_retry(path, target)
        moved.append(relative)


def restore_program_files(root, staging, previous, installed, backed_up):
    for relative in reversed(installed):
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        rename_with_retry(root / relative, target)
    for relative in reversed(backed_up):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        rename_with_retry(previous / relative, target)


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
    if not (root / 'FBAutomationRealEstate.exe').is_file() or data.is_relative_to(root):
        raise ValueError('Invalid installation/data layout')
    release_install_directory(root, folder)
    staging = root.with_name(root.name + '.staging-' + str(os.getpid()))
    previous = root.with_name(root.name + '.previous-' + str(os.getpid()))
    if staging.exists() or previous.exists():
        raise ValueError('Update staging folder already exists')
    staging.mkdir()
    with zipfile.ZipFile(package) as archive:
        safe_extract(archive, staging)
    if not (staging / 'FBAutomationRealEstate.exe').is_file():
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
    previous.mkdir()
    backed_up, installed = [], []
    reset_done = False
    try:
        move_program_files(root, previous, backed_up)
        if manifest.get('reset_databases') and (data / 'listings.db').exists():
            import sqlite3
            with closing(sqlite3.connect(data / 'listings.db')) as source, closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            reset_done = True
            for suffix in ['', '-wal', '-shm']:
                (data / ('listings.db' + suffix)).unlink(missing_ok=True)
        # Expose the new launcher only after its libraries are in place.
        move_program_files(staging, root, installed, executable_last=True)
        health = folder / 'healthy.json'
        health.unlink(missing_ok=True)
        child = launch_independent([str(root / 'FBAutomationRealEstate.exe'), '--health-file', str(health)], data)
        deadline = time.monotonic() + 90
        while not startup_healthy(health, manifest['version']):
            if child.poll() is not None or time.monotonic() > deadline:
                child.terminate()
                child.wait(timeout=15)
                raise RuntimeError('New version failed startup; restoring previous version')
            time.sleep(1)
    except Exception as exc:
        restore_program_files(root, staging, previous, installed, backed_up)
        if reset_done and backup.exists():
            for suffix in ['', '-wal', '-shm']:
                (data / ('listings.db' + suffix)).unlink(missing_ok=True)
            shutil.copy2(backup, data / 'listings.db')
        try:
            (folder / 'last-update.json').write_text(json.dumps({'version': manifest['version'], 'success': False, 'error': str(exc)}), encoding='utf-8')
        except OSError:
            pass
        launch_independent([str(root / 'FBAutomationRealEstate.exe')], data)
        raise

    # The new app is healthy. Cleanup errors must never trigger rollback.
    try:
        (folder / 'last-update.json').write_text(json.dumps({'version': manifest['version'], 'success': True}), encoding='utf-8')
        shutil.rmtree(staging)
        # Keep one previous program version; remove only verified sibling backups.
        for old in root.parent.glob(root.name + '.previous-*'):
            if old != previous and (old / 'FBAutomationRealEstate.exe').is_file():
                shutil.rmtree(old)
        package.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'FB Automation Real Estate update', 0x10)
