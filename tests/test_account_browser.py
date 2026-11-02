import ctypes
from ctypes import wintypes
import threading
import unittest
from unittest.mock import MagicMock, patch

from automation import account_browser


class AccountBrowserTests(unittest.TestCase):
    def setUp(self):
        account_browser._account = None
        account_browser._account_lock = None
        account_browser._chrome_hwnd = None
        account_browser._needs_focus = False

    def tearDown(self):
        account_browser._account = None
        account_browser._account_lock = None
        account_browser._chrome_hwnd = None
        account_browser._needs_focus = False

    def test_busy_account_cannot_open(self):
        with patch.dict('os.environ', {'FB_AUTOMATION_DESKTOP': '1'}), \
             patch.object(account_browser.cdp_login, 'get_cdp_account', return_value={'status': 'ready', 'port': 9222}), \
             patch.object(account_browser, 'active_tasks', return_value=[{'action': 'post'}]), \
             patch.object(account_browser.cdp_login, '_launch_profile') as launch:
            with self.assertRaisesRegex(RuntimeError, 'running automation'):
                account_browser.open_account('Main')
            launch.assert_not_called()

    def test_stop_closes_only_selected_account_and_releases_lock(self):
        lock = threading.Lock()
        lock.acquire()
        account_browser._account = 'Main'
        account_browser._account_lock = lock
        account_browser._chrome_hwnd = 123
        with patch.object(account_browser, 'active_tasks', return_value=[]), \
             patch.object(account_browser.cdp_login, 'get_cdp_account', return_value={'port': 9222}), \
             patch.object(account_browser.cdp_login, '_port_open', return_value=False), \
             patch.object(account_browser.cdp_login, 'close_cdp_browser') as close:
            result = account_browser.stop()
        self.assertFalse(result['open'])
        self.assertFalse(lock.locked())
        close.assert_called_once_with('Main')

    def test_stop_refuses_active_automation(self):
        lock = threading.Lock()
        lock.acquire()
        account_browser._account = 'Main'
        account_browser._account_lock = lock
        with patch.object(account_browser, 'active_tasks', return_value=[{'action': 'post'}]):
            with self.assertRaisesRegex(RuntimeError, 'active automation'):
                account_browser.stop()
        self.assertTrue(lock.locked())
        lock.release()

    def test_layout_fits_actual_app_client_area(self):
        account_browser._account = 'Main'
        account_browser._chrome_hwnd = 123
        fake_user32 = MagicMock()
        fake_user32.IsWindow.return_value = True
        fake_user32.FindWindowW.return_value = 456

        def client_rect(_hwnd, pointer):
            rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1200, 800
            return True

        fake_user32.GetClientRect.side_effect = client_rect
        fake_gdi32 = MagicMock()
        fake_gdi32.CreateRectRgn.return_value = 789
        account_browser._needs_focus = True
        with patch.object(account_browser, 'user32', fake_user32), \
             patch.object(account_browser, 'gdi32', fake_gdi32), \
             patch.object(account_browser, '_focus_browser') as focus:
            account_browser.layout(200, 100, 700, 600, 1000, 700)
        args = fake_user32.SetWindowPos.call_args.args
        self.assertEqual(args[2:6], (228, 82, 864, 728))
        fake_gdi32.CreateRectRgn.assert_called_once_with(12, 32, 852, 717)
        fake_user32.SetWindowRgn.assert_called_once_with(123, 789, True)
        focus.assert_called_once_with(123, 456)
        self.assertFalse(account_browser._needs_focus)
