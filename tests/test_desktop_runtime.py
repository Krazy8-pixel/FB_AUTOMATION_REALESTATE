import base64
from contextlib import closing
import io
import json
import hashlib
import sqlite3
import subprocess
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from automation.release_security import verify_manifest
from automation import desktop_state, maintenance
from automation import update_process
from updater import safe_extract
import updater


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes_raw()).decode()
        self.raw = json.dumps({'app': 'TayyaraMotors', 'version': '1.1.0', 'sha256': 'a' * 64, 'reset_databases': []}).encode()

    def test_release_signature_accepts_original_only(self):
        signature = self.key.sign(self.raw)
        self.assertEqual(verify_manifest(self.raw, signature, self.public)['version'], '1.1.0')
        with self.assertRaises(InvalidSignature):
            verify_manifest(self.raw.replace(b'1.1.0', b'9.9.0'), signature, self.public)

    def test_release_rejects_reset_of_accounts(self):
        raw = self.raw.replace(b'[]', b'["accounts"]')
        with self.assertRaises(ValueError):
            verify_manifest(raw, self.key.sign(raw), self.public)

    def test_archive_cannot_escape_installation(self):
        for name in ['../outside.txt', '/outside.txt', 'C:/outside.txt']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as archive:
                    archive.writestr(name, 'bad')
                with zipfile.ZipFile(stream) as archive, self.assertRaises(ValueError):
                    safe_extract(archive, Path(folder))

    def test_saved_pending_jobs_survive_and_interrupted_jobs_need_review(self):
        routes = SimpleNamespace(scheduled_posts={'pending': {'status': 'pending'}, 'in_progress': {'status': 'posting'}}, repost_jobs={}, scheduled_posts_lock=threading.Lock(), repost_jobs_lock=threading.Lock())
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', FB_AUTOMATION_DESKTOP='1'), patch.object(desktop_state, 'data_dir', return_value=Path(folder)):
            desktop_state.save_state(routes)
            routes.scheduled_posts.clear()
            desktop_state.restore_state(routes)
            self.assertEqual(routes.scheduled_posts['pending']['status'], 'pending')
            self.assertEqual(routes.scheduled_posts['in_progress']['status'], 'failed')
            self.assertIn('avoid duplicates', routes.scheduled_posts['in_progress']['error'])

    def test_maintenance_waits_for_manual_post_lock(self):
        lock = threading.Lock()
        routes = SimpleNamespace(post_locks_by_account={'account': lock}, listing_import_jobs={}, listing_click_jobs={}, facebook_delete_jobs={}, groups_jobs={}, scheduled_posts={}, repost_jobs={}, scheduler=SimpleNamespace(get_jobs=lambda: []))
        lock.acquire()
        self.assertTrue(maintenance.busy(routes))
        lock.release()
        self.assertFalse(maintenance.busy(routes))

    def test_legacy_maintenance_does_nothing_even_with_saved_sync_enabled(self):
        routes = MagicMock()
        with patch.dict('sys.modules', {'web': SimpleNamespace(routes=routes)}), \
             patch('automation.desktop_preferences.load', return_value={'background_sync': True}), \
             patch.object(maintenance, 'data_dir') as directory, \
             patch.object(desktop_state, 'checkpoint') as checkpoint:
            for _ in range(6):
                maintenance.tick()
            ok, _ = maintenance.start_cookie_check('account')
        self.assertFalse(ok)
        self.assertEqual(routes.mock_calls, [])
        directory.assert_not_called()
        checkpoint.assert_not_called()

    def test_persisted_maintenance_removed_before_scheduler_resumes(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from datetime import datetime, timedelta, timezone
        # Exercise the real SQLite job store without a scheduler thread racing
        # temporary-database cleanup on Windows.
        with tempfile.TemporaryDirectory() as folder, patch.object(BackgroundScheduler, '_main_loop'):
            url = 'sqlite:///' + str(Path(folder) / 'schedules.db')
            first = BackgroundScheduler(jobstores={'default': SQLAlchemyJobStore(url=url)})
            first.start(paused=True)
            first.add_job(maintenance.tick, 'interval', minutes=1,
                          id='delete-listings-auto-refresh-check',
                          next_run_time=datetime.now(timezone.utc) - timedelta(minutes=2))
            first.add_job(maintenance.tick, 'date', id='user-post',
                          run_date=datetime.now(timezone.utc) + timedelta(days=1))
            first.shutdown()
            restored = BackgroundScheduler(jobstores={'default': SQLAlchemyJobStore(url=url)})
            try:
                with patch.object(restored, 'resume', wraps=restored.resume) as resume:
                    maintenance.start_scheduler_without_maintenance(restored)
                resume.assert_called_once()
                self.assertIsNone(restored.get_job('delete-listings-auto-refresh-check'))
                self.assertIsNotNone(restored.get_job('user-post'))
            finally:
                restored.shutdown()

    def run_update_scenario(self, fail=False, include_new_uninstaller=False):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            root, data, updates = folder / 'app', folder / 'data', folder / 'updates'
            for path in [root, data, updates]:
                path.mkdir()
            (root / 'TayyaraMotors.exe').write_bytes(b'previous-program')
            (root / 'Uninstall.exe').write_bytes(b'uninstaller')
            (data / 'session.pkl').write_bytes(b'saved-session')
            with closing(sqlite3.connect(data / 'listings.db')) as db:
                db.execute('CREATE TABLE saved(value TEXT)')
                db.execute("INSERT INTO saved VALUES ('client-listing')")
                db.commit()
            package = updates / 'update.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr('TayyaraMotors.exe', b'new-program')
                if include_new_uninstaller:
                    archive.writestr('Uninstall.exe', b'new-uninstaller')
                archive.writestr('_internal/app_config.json', json.dumps({'version': '1.1.0'}))
            manifest = {'app': 'TayyaraMotors', 'version': '1.1.0', 'sha256': hashlib.sha256(package.read_bytes()).hexdigest(), 'reset_databases': ['listings'] if fail else []}
            raw = json.dumps(manifest).encode()
            (updates / 'manifest.json').write_bytes(raw)
            (updates / 'manifest.sig').write_bytes(self.key.sign(raw))
            plan = updates / 'plan.json'
            plan.write_text(json.dumps({'package': str(package), 'public_key': self.public, 'install_dir': str(root), 'data_dir': str(data), 'pid': 1}))
            launches = []

            def launch(args, **kwargs):
                self.assertEqual(kwargs['cwd'], str(data.resolve()))
                launches.append(args)
                if '--health-file' in args and not fail:
                    Path(args[-1]).write_text(json.dumps({'version': '1.1.0'}))
                return SimpleNamespace(poll=lambda: 0 if fail else None, terminate=lambda: None, wait=lambda timeout: None)

            with patch.object(updater, 'process_alive', return_value=False), patch.object(updater.subprocess, 'Popen', side_effect=launch):
                if fail:
                    with self.assertRaises(RuntimeError):
                        updater.main(plan)
                else:
                    updater.main(plan)
            self.assertEqual((root / 'TayyaraMotors.exe').read_bytes(), b'previous-program' if fail else b'new-program')
            self.assertEqual((root / 'Uninstall.exe').read_bytes(), b'new-uninstaller' if include_new_uninstaller and not fail else b'uninstaller')
            self.assertEqual((data / 'session.pkl').read_bytes(), b'saved-session')
            with closing(sqlite3.connect(data / 'listings.db')) as db:
                self.assertEqual(db.execute('SELECT value FROM saved').fetchone()[0], 'client-listing')
            self.assertEqual(len(launches), 2 if fail else 1)

    def test_update_replaces_program_and_preserves_client_data(self):
        self.run_update_scenario()

    def test_updater_leaves_inherited_install_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'app'
            updates = Path(folder) / 'updates'
            with patch.object(updater.Path, 'cwd', return_value=root / '_internal'), patch.object(updater.os, 'chdir') as change:
                updater.release_install_directory(root, updates)
            change.assert_called_once_with(updates)

    def test_folder_swap_retries_windows_sharing_violation(self):
        error = OSError('File is in use')
        error.winerror = 32
        with patch.object(updater.Path, 'rename', side_effect=[error, None]) as rename, patch.object(updater.time, 'sleep'):
            updater.rename_with_retry(Path('app'), Path('previous'))
        self.assertEqual(rename.call_count, 2)

    def test_folder_swap_timeout_explains_recovery(self):
        error = OSError('File is in use')
        error.winerror = 32
        with patch.object(updater.Path, 'rename', side_effect=error), patch.object(updater.time, 'monotonic', side_effect=[0, 61]):
            with self.assertRaisesRegex(RuntimeError, 'restart Windows'):
                updater.rename_with_retry(Path('app'), Path('previous'))

    def test_file_update_does_not_rename_install_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root, previous = Path(folder) / 'app', Path(folder) / 'previous'
            (root / '_internal').mkdir(parents=True)
            (root / 'TayyaraMotors.exe').write_bytes(b'old-exe')
            (root / '_internal/library.dll').write_bytes(b'old-library')
            moved = []
            with patch.object(updater, 'rename_with_retry', wraps=updater.rename_with_retry) as rename:
                updater.move_program_files(root, previous, moved)
            self.assertTrue(root.is_dir())
            self.assertEqual(len(moved), 2)
            self.assertEqual(moved[0], Path('TayyaraMotors.exe'))
            self.assertTrue(all(call.args[0].suffix in {'.exe', '.dll'} for call in rename.call_args_list))
            self.assertEqual((previous / '_internal/library.dll').read_bytes(), b'old-library')
            installed = []
            updater.move_program_files(previous, root, installed, executable_last=True)
            self.assertEqual(installed[-1], Path('TayyaraMotors.exe'))

    def test_partial_file_install_can_restore_exact_previous_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root, previous, staging = [Path(folder) / name for name in ('app', 'previous', 'staging')]
            for path in (root, previous, staging):
                path.mkdir()
            (previous / 'TayyaraMotors.exe').write_bytes(b'old-exe')
            (root / 'TayyaraMotors.exe').write_bytes(b'new-exe')
            (root / 'still-locked.dll').write_bytes(b'untouched-library')
            updater.restore_program_files(root, staging, previous,
                                          [Path('TayyaraMotors.exe')], [Path('TayyaraMotors.exe')])
            self.assertEqual((root / 'TayyaraMotors.exe').read_bytes(), b'old-exe')
            self.assertEqual((root / 'still-locked.dll').read_bytes(), b'untouched-library')
            self.assertEqual((staging / 'TayyaraMotors.exe').read_bytes(), b'new-exe')

    def test_update_launch_does_not_inherit_parent_runtime_or_handles(self):
        with patch.object(update_process.sys, 'frozen', False, create=True), \
             patch.object(update_process.subprocess, 'Popen') as launch:
            update_process.launch_independent(['TayyaraMotors.exe'], Path('data'))
        options = launch.call_args.kwargs
        self.assertTrue(options['close_fds'])
        self.assertEqual(options['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertEqual(options['cwd'], 'data')
        self.assertEqual(options['stdout'], subprocess.DEVNULL)

    def test_update_installs_updated_uninstaller(self):
        self.run_update_scenario(include_new_uninstaller=True)

    def test_failed_update_restores_program_and_reset_database(self):
        self.run_update_scenario(fail=True)


if __name__ == '__main__':
    unittest.main()
