import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from automation import app_updates, desktop_preferences


class AutomaticUpdateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.original_install = app_updates.install
        self.install = MagicMock()
        self.check = MagicMock()
        self.preferences = {'automatic_updates': True}
        patches = [
            patch.object(app_updates.sys, 'frozen', True, create=True),
            patch.object(app_updates, 'exit_callback', MagicMock()),
            patch.object(app_updates, '_status', {'state': 'idle'}),
            patch.object(app_updates, '_ready', None),
            patch.object(app_updates, '_last_check_attempt', None),
            patch.object(app_updates, '_last_install_attempt', None),
            patch.object(app_updates, 'data_dir', return_value=self.root),
            patch.object(app_updates, '_automation_busy', return_value=False),
            patch.object(app_updates, 'check', self.check),
            patch.object(app_updates, 'install', self.install),
            patch.object(desktop_preferences, 'load', return_value=self.preferences),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def available(self, reset=False):
        app_updates._status.update(state='available', available=True)
        app_updates._ready = ({'version': '2.0.0', 'reset_databases': ['listings'] if reset else []}, b'', b'', 'url')

    def test_startup_checks_and_respects_six_hour_interval(self):
        app_updates.automatic_tick()
        self.check.assert_called_once()
        self.check.reset_mock()
        app_updates._status['state'] = 'current'
        with patch.object(app_updates.time, 'monotonic', return_value=100):
            app_updates._last_check_attempt = 99
            app_updates.automatic_tick()
        self.check.assert_not_called()
        with patch.object(app_updates.time, 'monotonic', return_value=100 + app_updates.CHECK_INTERVAL):
            app_updates.automatic_tick()
        self.check.assert_called_once()

    def test_network_errors_retry_after_fifteen_minutes(self):
        app_updates._status['state'] = 'error'
        app_updates._last_check_attempt = 100
        with patch.object(app_updates.time, 'monotonic', return_value=999):
            app_updates.automatic_tick()
        self.check.assert_not_called()
        with patch.object(app_updates.time, 'monotonic', return_value=1000):
            app_updates.automatic_tick()
        self.check.assert_called_once()

    def test_disabled_and_source_mode_do_not_update(self):
        self.preferences['automatic_updates'] = False
        app_updates.automatic_tick()
        self.preferences['automatic_updates'] = True
        with patch.object(app_updates.sys, 'frozen', False):
            app_updates.automatic_tick()
        self.check.assert_not_called()
        self.install.assert_not_called()

    def test_update_waits_for_automation_then_installs(self):
        self.available()
        with patch.object(app_updates, '_automation_busy', return_value=True):
            app_updates.automatic_tick()
        self.install.assert_not_called()
        self.assertIn('automation finishes', app_updates._status['message'])
        app_updates.automatic_tick()
        self.install.assert_called_once_with(automatic=True)

    def test_reset_releases_need_manual_confirmation(self):
        self.available(reset=True)
        app_updates.automatic_tick()
        self.install.assert_not_called()
        self.assertIn('Settings', app_updates._status['message'])

    def test_failed_install_has_retry_backoff(self):
        self.available()
        self.install.side_effect = ValueError('Busy')
        app_updates.automatic_tick()
        app_updates.automatic_tick()
        self.install.assert_called_once()

    def test_failed_startup_version_is_not_automatically_retried(self):
        self.available()
        (self.root / 'last-update.json').write_text(json.dumps({'version': '2.0.0', 'success': False}))
        app_updates.automatic_tick()
        self.install.assert_not_called()
        self.assertIn('rolled back', app_updates._status['message'])

    def test_download_and_install_are_not_started_twice(self):
        self.available()
        for state in ('checking', 'downloading', 'installing'):
            app_updates._status['state'] = state
            app_updates.automatic_tick()
        self.install.assert_not_called()
        self.check.assert_not_called()

    def test_actual_install_rechecks_busy_state_before_pausing_scheduler(self):
        self.available()
        with patch.object(app_updates, '_automation_busy', return_value=True), \
             patch.dict('sys.modules', {'web': SimpleNamespace(routes=MagicMock())}):
            with self.assertRaisesRegex(ValueError, 'automation'):
                # Call the implementation beneath the mock installed in setUp.
                self.original_install(automatic=True)


class UpdatePreferenceTests(unittest.TestCase):
    def test_existing_preferences_enable_automatic_updates_and_preserve_choices(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop_preferences, 'data_dir', return_value=Path(folder)):
            path = Path(folder) / 'desktop_preferences.json'
            path.write_text(json.dumps({'background_sync': False, 'start_with_windows': False}))
            self.assertTrue(desktop_preferences.load()['automatic_updates'])
            desktop_preferences.save(False, False, automatic_updates=False)
            self.assertFalse(desktop_preferences.load()['automatic_updates'])
            desktop_preferences.save(True, False)
            self.assertFalse(desktop_preferences.load()['automatic_updates'])
