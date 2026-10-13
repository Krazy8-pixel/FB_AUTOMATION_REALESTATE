import base64
from contextlib import closing
import io
import json
import hashlib
import sqlite3
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from automation.release_security import verify_manifest
from automation import desktop_state, maintenance
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

    def test_group_fetch_starts_once_after_success(self):
        calls = []
        routes = SimpleNamespace(post_locks_by_account={}, listing_import_jobs={}, listing_click_jobs={},
            facebook_delete_jobs={}, groups_jobs={}, scheduled_posts={}, repost_jobs={},
            scheduler=SimpleNamespace(get_jobs=lambda: []), _get_post_lock=lambda account: threading.Lock(),
            get_all_accounts=lambda: ['account'], _listing_snapshot_is_due=lambda account: False,
            _start_groups_import_job=lambda account: (calls.append(account) or (True, 'Started')))
        with tempfile.TemporaryDirectory() as folder, patch.dict('sys.modules', {'web': SimpleNamespace(routes=routes)}), patch.object(desktop_state, 'checkpoint'), patch('automation.desktop_preferences.load', return_value={'background_sync': True}), patch.object(maintenance, 'data_dir', return_value=Path(folder)):
            maintenance.record('account', 'clicks', True)
            maintenance.tick()
            maintenance.record('account', 'groups', True)
            maintenance.tick()
        self.assertEqual(calls, ['account'])

    def test_two_days_off_runs_overdue_maintenance_once_per_account(self):
        calls = []
        routes = SimpleNamespace(post_locks_by_account={}, listing_import_jobs={}, listing_click_jobs={}, facebook_delete_jobs={}, groups_jobs={}, scheduled_posts={}, repost_jobs={}, scheduler=SimpleNamespace(get_jobs=lambda: []), _get_post_lock=lambda account: threading.Lock(), _listing_snapshot_is_due=lambda account: True, get_all_accounts=lambda: ['first', 'second'])
        routes._start_listing_import_job = lambda account, automatic: (calls.append(('import', account)) or (True, 'Started'))
        routes._start_listing_click_refresh_job = lambda account: (calls.append(('clicks', account)) or (True, 'Started'))
        with tempfile.TemporaryDirectory() as folder, patch.dict('sys.modules', {'web': SimpleNamespace(routes=routes)}), patch.object(desktop_state, 'checkpoint'), patch('automation.desktop_preferences.load', return_value={'background_sync': True}), patch.object(maintenance, 'data_dir', return_value=Path(folder)), patch.object(maintenance.time, 'time', return_value=172800):
            (Path(folder) / 'maintenance.json').write_text(json.dumps({name: {'groups': {'last_success': 1}} for name in ['first', 'second']}))
            for _ in range(6):
                maintenance.tick()
        self.assertEqual(calls, [('import', 'first'), ('clicks', 'first'), ('import', 'second'), ('clicks', 'second')])

    def run_update_scenario(self, fail=False):
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
                archive.writestr('_internal/app_config.json', json.dumps({'version': '1.1.0'}))
            manifest = {'app': 'TayyaraMotors', 'version': '1.1.0', 'sha256': hashlib.sha256(package.read_bytes()).hexdigest(), 'reset_databases': ['listings'] if fail else []}
            raw = json.dumps(manifest).encode()
            (updates / 'manifest.json').write_bytes(raw)
            (updates / 'manifest.sig').write_bytes(self.key.sign(raw))
            plan = updates / 'plan.json'
            plan.write_text(json.dumps({'package': str(package), 'public_key': self.public, 'install_dir': str(root), 'data_dir': str(data), 'pid': 1}))
            launches = []

            def launch(args):
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
            self.assertEqual((root / 'Uninstall.exe').read_bytes(), b'uninstaller')
            self.assertEqual((data / 'session.pkl').read_bytes(), b'saved-session')
            with closing(sqlite3.connect(data / 'listings.db')) as db:
                self.assertEqual(db.execute('SELECT value FROM saved').fetchone()[0], 'client-listing')
            self.assertEqual(len(launches), 2 if fail else 1)

    def test_update_replaces_program_and_preserves_client_data(self):
        self.run_update_scenario()

    def test_failed_update_restores_program_and_reset_database(self):
        self.run_update_scenario(fail=True)


if __name__ == '__main__':
    unittest.main()
