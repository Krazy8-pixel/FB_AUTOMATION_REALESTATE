import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from selenium.webdriver.chrome.options import Options
from automation import chromedriver


class RegularChromeTests(unittest.TestCase):
    def test_windows_policy_block_has_specific_message(self):
        error = OSError('blocked')
        error.winerror = 4551
        with patch.dict(os.environ, {'FB_AUTOMATION_DESKTOP': '1'}), patch.object(chromedriver, 'installed_chrome_path', return_value='regular.exe'), patch.object(chromedriver, 'data_dir', return_value='cache'), patch.object(chromedriver, 'SeleniumManager') as manager, patch.object(chromedriver.webdriver, 'Chrome', side_effect=error):
            manager.return_value.binary_paths.return_value = {'driver_path': 'driver.exe'}
            with self.assertRaisesRegex(RuntimeError, 'Windows Application Control blocked'):
                chromedriver.build_chrome_driver(Options())

    def test_missing_chrome_has_install_message(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(chromedriver.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(FileNotFoundError, 'Install regular Google Chrome'):
                chromedriver.installed_chrome_path()

    def test_desktop_uses_detected_browser_and_manager_even_with_old_controls(self):
        options = Options()
        manager = MagicMock()
        manager.binary_paths.return_value = {'driver_path': 'matching-driver.exe'}
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {
            'FB_AUTOMATION_DESKTOP': '1', 'FB_AUTOMATION_FORCE_BUNDLED_CHROMEDRIVER': '1',
            'CHROMEDRIVER_PATH': 'old-driver.exe',
        }), patch.object(chromedriver, 'installed_chrome_path', return_value='regular-chrome.exe'), patch.object(chromedriver, 'data_dir', return_value=cache), patch.object(chromedriver, 'SeleniumManager', return_value=manager), patch.object(chromedriver.webdriver, 'Chrome') as create:
            chromedriver.build_chrome_driver(options)
            self.assertEqual(options.binary_location, 'regular-chrome.exe')
            args = manager.binary_paths.call_args.args[0]
            self.assertIn('--avoid-browser-download', args)
            self.assertEqual(args[args.index('--browser-path') + 1], 'regular-chrome.exe')
            self.assertEqual(create.call_args.kwargs['service'].path, 'matching-driver.exe')

    def test_manager_failure_does_not_download_fallback_browser(self):
        with patch.dict(os.environ, {'FB_AUTOMATION_DESKTOP': '1'}), patch.object(chromedriver, 'installed_chrome_path', return_value='regular.exe'), patch.object(chromedriver, 'data_dir', return_value='cache'), patch.object(chromedriver, 'SeleniumManager') as manager, patch.object(chromedriver.webdriver, 'Chrome') as create:
            manager.return_value.binary_paths.side_effect = RuntimeError('offline')
            with self.assertRaisesRegex(RuntimeError, 'RuntimeError: offline'):
                chromedriver.build_chrome_driver(Options())
            create.assert_not_called()
