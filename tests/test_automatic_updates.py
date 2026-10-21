import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from automation import app_updates, desktop_preferences


class ManualUpdateTests(unittest.TestCase):
    def test_download_reports_each_chunk_and_total_size(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {'Content-Length': '6'}
        response.iter_content.return_value = [b'abc', b'', b'def']
        reports = []
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(app_updates, 'fetch', return_value=response), \
             patch.object(app_updates, 'set_status', side_effect=lambda **values: reports.append(values)):
            package = Path(folder) / 'update.zip'
            digest = app_updates.download_package('url', package)
            self.assertEqual(package.read_bytes(), b'abcdef')
        self.assertEqual(digest, hashlib.sha256(b'abcdef').hexdigest())
        self.assertEqual([item['downloaded_bytes'] for item in reports], [0, 3, 6])
        self.assertEqual(reports[1]['download_percent'], 50)
        self.assertEqual(reports[-1]['download_percent'], 100)
        self.assertEqual(reports[-1]['total_bytes'], 6)

    def test_unknown_size_keeps_showing_downloaded_bytes(self):
        with patch.object(app_updates, 'set_status') as report:
            app_updates.download_progress(2_500_000)
        values = report.call_args.kwargs
        self.assertIsNone(values['download_percent'])
        self.assertIn('2.5 MB', values['message'])

    def test_incomplete_download_is_rejected(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {'Content-Length': '6'}
        response.iter_content.return_value = [b'abc']
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(app_updates, 'fetch', return_value=response), \
             patch.object(app_updates, 'set_status'):
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                app_updates.download_package('url', Path(folder) / 'update.zip')

    def test_manual_install_requires_idle_automation(self):
        routes = MagicMock()
        with patch.object(app_updates, '_ready', ({'version': '2.0.0'}, b'', b'', 'url')), \
             patch.object(app_updates, '_status', {'state': 'available'}), \
             patch.object(app_updates.sys, 'frozen', True, create=True), \
             patch.object(app_updates, 'exit_callback', MagicMock()), \
             patch.object(app_updates, '_automation_busy', return_value=True), \
             patch.dict('sys.modules', {'web': SimpleNamespace(routes=routes)}):
            with self.assertRaisesRegex(ValueError, 'automation'):
                app_updates.install()
        routes.scheduler.pause.assert_not_called()

    def test_old_automatic_update_preference_is_discarded(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(desktop_preferences, 'data_dir', return_value=Path(folder)), \
             patch.object(desktop_preferences.sys, 'frozen', False, create=True):
            path = Path(folder) / 'desktop_preferences.json'
            path.write_text(json.dumps({'background_sync': True, 'start_with_windows': False, 'automatic_updates': True}))
            self.assertNotIn('automatic_updates', desktop_preferences.load())
            self.assertFalse(desktop_preferences.load()['background_sync'])
            desktop_preferences.save(True, False)
            saved = json.loads(path.read_text())
            self.assertNotIn('automatic_updates', saved)
            self.assertFalse(saved['background_sync'])
            self.assertFalse(saved['start_with_windows'])
