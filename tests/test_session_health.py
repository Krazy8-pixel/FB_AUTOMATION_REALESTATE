import tempfile
import pickle
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from selenium.common.exceptions import TimeoutException
from automation import session_health as health
from automation import cdp_login


class SessionHealthTests(unittest.TestCase):
    def check(self, *, url='https://www.facebook.com/', timeout=False, error=False):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'account.pkl'
            path.write_bytes(pickle.dumps([{'name': 'c_user', 'value': '123'}]))
            driver = MagicMock()
            driver.current_url = url
            driver.find_elements.return_value = []
            wait = MagicMock()
            if timeout:
                wait.until.side_effect = TimeoutException()
            with patch.object(health, '_session_file_for_account', return_value=str(path)), \
                 patch.object(health, 'build_chrome_driver', side_effect=RuntimeError('network') if error else None, return_value=driver), \
                 patch.object(health, '_safe_get'), patch.object(health.time, 'sleep'), \
                 patch.object(health, 'WebDriverWait', return_value=wait):
                return health.check_account_login_cookie('account')

    def test_authenticated_browser_is_valid(self):
        self.assertEqual(self.check(), health.HEALTHY)

    def test_login_redirect_is_expired(self):
        self.assertEqual(self.check(url='https://www.facebook.com/login/', timeout=True), health.EXPIRED)

    def test_browser_failure_is_not_expired(self):
        self.assertEqual(self.check(error=True), health.UNKNOWN)
        self.assertEqual(self.check(timeout=True), health.EXPIRED)

    def test_missing_cookie_file_is_expired(self):
        with patch.object(health, '_session_file_for_account', return_value='missing-session-file.pkl'):
            self.assertEqual(health.check_account_login_cookie('account'), health.EXPIRED)

    def test_relogin_timeout_does_not_delete_account(self):
        with patch.object(cdp_login, 'list_cdp_accounts', return_value={'account': {'status': 'waiting', 'relogin': True, 'login_started_at': 1}}), \
             patch.object(cdp_login, '_update_account') as update, \
             patch.object(cdp_login, 'delete_cdp_account') as delete:
            cdp_login.cleanup_expired_cdp_logins()
        delete.assert_not_called()
        self.assertEqual(update.call_args.kwargs['status'], 'relogin_expired')

    def test_cdp_confirmation_requires_home_ui_and_cookie(self):
        driver = MagicMock()
        driver.current_url = 'https://www.facebook.com/'
        driver.get_cookies.return_value = [{'name': 'c_user', 'value': '123'}]
        with patch.object(cdp_login, '_is_home_screen_visible', return_value=False):
            self.assertFalse(cdp_login._facebook_login_confirmed(driver))
        with patch.object(cdp_login, '_is_home_screen_visible', return_value=True):
            self.assertTrue(cdp_login._facebook_login_confirmed(driver))
            driver.current_url = 'https://www.facebook.com/checkpoint/'
            self.assertFalse(cdp_login._facebook_login_confirmed(driver))
