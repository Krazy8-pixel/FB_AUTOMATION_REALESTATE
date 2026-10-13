import unittest
from unittest.mock import MagicMock, patch

from automation import cdp_login


class LoginDetectionTests(unittest.TestCase):
    def test_failed_cdp_close_closes_dedicated_windows(self):
        driver = MagicMock()
        driver.execute_cdp_cmd.side_effect = RuntimeError('CDP disconnected')
        driver.window_handles = ['one', 'two']
        cdp_login._close_dedicated_browser(driver)
        self.assertEqual(driver.close.call_count, 2)
        driver.service.stop.assert_called_once()

    def test_refresh_closes_browser_it_launched(self):
        import tempfile
        driver = MagicMock()
        driver.get_cookies.return_value = []
        with tempfile.TemporaryDirectory() as folder, patch.object(cdp_login, 'get_cdp_account', return_value={'port': 9222}), patch.object(cdp_login, '_port_open', side_effect=[False, True]), patch.object(cdp_login, '_launch_profile'), patch.object(cdp_login, '_attach', return_value=driver), patch.object(cdp_login, '_facebook_login_confirmed', return_value=True), patch.object(cdp_login, 'sessions_dir', return_value=folder), patch.object(cdp_login, '_update_account'), patch.object(cdp_login, '_close_dedicated_browser') as close:
            cdp_login.refresh_cdp_cookies('account')
        close.assert_called_once_with(driver)

    def test_interrupted_login_is_no_longer_waiting(self):
        with patch.object(cdp_login, 'list_cdp_accounts', return_value={
            'attempt': {'status': 'waiting', 'login_started_at': 9999999999},
        }), patch.object(cdp_login, '_watchers', {}), patch.object(cdp_login, '_update_account') as update:
            cdp_login.cleanup_expired_cdp_logins()
        self.assertEqual(update.call_args.kwargs['status'], 'failed')

    def test_restore_failure_closes_new_browser(self):
        from automation import reposting
        import tempfile
        import pickle
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'account.pkl'
            path.write_bytes(pickle.dumps([]))
            driver = MagicMock()
            with patch.object(reposting, '_resolve_session_file', return_value=str(path)), patch.object(reposting, 'build_chrome_driver', return_value=driver), patch.object(reposting, '_restore_facebook_cookies', side_effect=RuntimeError('network')):
                with self.assertRaisesRegex(RuntimeError, 'network'):
                    reposting._open_prelogged_facebook('account', force_headless=True)
            driver.quit.assert_called_once()

    def test_authenticated_second_tab_is_selected_for_cookie_export(self):
        driver = MagicMock()
        driver.current_window_handle = 'first'
        driver.window_handles = ['first', 'facebook']
        with patch.object(cdp_login, '_facebook_login_confirmed', side_effect=[False, True]):
            self.assertTrue(cdp_login._find_logged_in_tab(driver))
        driver.switch_to.window.assert_called_once_with('facebook')

    def test_no_authenticated_tab_restores_original(self):
        driver = MagicMock()
        driver.current_window_handle = 'first'
        driver.window_handles = ['first', 'other']
        with patch.object(cdp_login, '_facebook_login_confirmed', return_value=False):
            self.assertFalse(cdp_login._find_logged_in_tab(driver))
        self.assertEqual(driver.switch_to.window.call_args.args, ('first',))

    def test_lookalike_domain_cannot_confirm_login(self):
        driver = MagicMock()
        driver.current_url = 'https://facebook.com.example.org/'
        self.assertFalse(cdp_login._facebook_login_confirmed(driver))
        driver.get_cookies.assert_not_called()

    def test_checkpoint_with_cookie_stays_unconfirmed(self):
        driver = MagicMock()
        driver.current_url = 'https://www.facebook.com/checkpoint/'
        self.assertFalse(cdp_login._facebook_login_confirmed(driver))


if __name__ == '__main__':
    unittest.main()
