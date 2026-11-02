import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import (
    NoSuchElementException, StaleElementReferenceException, TimeoutException,
)

from automation import reposting

REAL_WAIT = reposting.WebDriverWait


def fallback_wait(driver, timeout, *args, **kwargs):
    """Keep selection polling real; simulate an unavailable popup public link."""
    if timeout == 20:
        return REAL_WAIT(driver, timeout, *args, **kwargs)
    wait = MagicMock()
    def until(condition, message=''):
        if 'Selected popup did not expose' in message:
            raise TimeoutException('No public link in fallback fixture')
        result = condition(driver)
        if not result:
            raise TimeoutException(message)
        return result
    wait.until.side_effect = until
    return wait


class PublicListingNavigationTests(unittest.TestCase):
    def driver(self):
        driver = MagicMock()
        driver.current_url = 'https://www.facebook.com/marketplace/you/selling'
        driver.current_window_handle = 'inventory'
        driver.window_handles = ['inventory']
        driver.timeouts.page_load = 45
        driver.get.side_effect = lambda url: setattr(driver, 'current_url', url)
        def new_window(kind):
            driver.current_window_handle = 'public'
            driver.window_handles.append('public')
        driver.switch_to.new_window.side_effect = new_window
        driver.switch_to.window.side_effect = lambda handle: setattr(
            driver, 'current_window_handle', handle)
        driver.close.side_effect = lambda: driver.window_handles.remove(driver.current_window_handle)
        return driver

    def edit_link(self, listing_id='123'):
        link = MagicMock()
        link.is_displayed.return_value = True
        link.get_attribute.return_value = (
            'https://www.facebook.com/marketplace/edit/?listing_id=' + listing_id)
        return link

    def read(self, driver, listing=None):
        sources = {'image_urls': ['public-photo'], 'video_urls': [], 'video_errors': []}
        with patch('selenium.webdriver.support.wait.time.sleep'), \
             patch.object(reposting, 'WebDriverWait', side_effect=fallback_wait), \
             patch('automation.public_listing_media.read_public_listing_media',
                   return_value=sources) as gallery:
            result = reposting._public_media_in_new_tab(driver, listing or {})
        self.assertEqual(result, sources)
        gallery.assert_called_once_with(driver, '123')
        driver.get.assert_called_once_with('https://www.facebook.com/marketplace/item/123/')
        driver.switch_to.new_window.assert_called_once_with('tab')
        driver.close.assert_called_once()
        self.assertEqual(driver.switch_to.window.call_args.args, ('inventory',))
        self.assertEqual(driver.current_window_handle, 'inventory')
        self.assertEqual([call.args for call in driver.set_page_load_timeout.call_args_list],
                         [(30,), (45,)])

    def test_stale_dialog_retries_with_fresh_elements(self):
        driver = self.driver()
        stale, fresh = MagicMock(), MagicMock()
        stale.is_displayed.side_effect = StaleElementReferenceException('dialog replaced')
        fresh.is_displayed.return_value = True
        fresh.find_elements.return_value = [self.edit_link()]
        driver.find_elements.side_effect = [[stale], [fresh]]
        self.read(driver, {'url': 'https://www.facebook.com/marketplace/item/999/'})
        self.assertEqual(driver.find_elements.call_count, 2)
        stale.find_elements.assert_not_called()

    def test_missing_edit_link_retries_instead_of_accepting_saved_url(self):
        driver = self.driver()
        old, fresh = self.edit_link(), self.edit_link()
        old.get_attribute.side_effect = NoSuchElementException('link replaced')
        dialog = MagicMock()
        dialog.is_displayed.return_value = True
        dialog.find_elements.side_effect = [[old], [fresh]]
        driver.find_elements.return_value = [dialog]
        self.read(driver, {'edit_url': 'https://www.facebook.com/marketplace/edit/?listing_id=999'})
        self.assertEqual(driver.find_elements.call_count, 2)
        self.assertEqual(dialog.find_elements.call_count, 2)

    def test_dialog_still_opening_does_not_use_old_saved_listing(self):
        driver = self.driver()
        fresh = MagicMock()
        fresh.is_displayed.return_value = True
        fresh.find_elements.return_value = [self.edit_link()]
        # First poll has no dialog or Edit link; second poll finds the selection.
        driver.find_elements.side_effect = [[], [], [fresh]]
        self.read(driver, {'url': 'https://www.facebook.com/marketplace/item/999/'})
        self.assertEqual(driver.find_elements.call_count, 3)

    def test_ambiguous_links_wait_for_one_selected_id(self):
        driver = self.driver()
        dialog = MagicMock()
        dialog.is_displayed.return_value = True
        dialog.find_elements.side_effect = [
            [self.edit_link('999'), self.edit_link()], [self.edit_link()]]
        driver.find_elements.return_value = [dialog]
        self.read(driver, {'url': 'https://www.facebook.com/marketplace/item/999/'})
        self.assertEqual(dialog.find_elements.call_count, 2)

    def test_current_listing_url_still_has_precedence(self):
        driver = self.driver()
        driver.current_url = 'https://www.facebook.com/marketplace/item/123/?ref=marketplace'
        self.read(driver, {'url': 'https://www.facebook.com/marketplace/item/999/'})
        driver.find_elements.assert_not_called()

    def test_unresolved_selected_listing_times_out_without_opening_saved_id(self):
        driver = self.driver()
        driver.find_elements.return_value = []
        with patch('selenium.webdriver.support.wait.time.monotonic', side_effect=[0, 21]), \
             self.assertRaisesRegex(TimeoutException, 'within 20 seconds'):
            reposting._public_media_in_new_tab(driver,
                {'url': 'https://www.facebook.com/marketplace/item/999/'})
        driver.switch_to.new_window.assert_not_called()
        driver.get.assert_not_called()

    def test_cancellation_exception_is_not_retried_or_swallowed(self):
        class ImportCancelled(RuntimeError):
            pass

        driver = self.driver()
        driver.find_elements.side_effect = ImportCancelled('stopped')
        with self.assertRaisesRegex(ImportCancelled, 'stopped'):
            reposting._public_media_in_new_tab(driver, {})
        self.assertEqual(driver.find_elements.call_count, 1)
        driver.switch_to.new_window.assert_not_called()

    def test_popup_public_link_is_used_before_new_tab_fallback(self):
        driver = self.driver()
        original_url = driver.current_url
        dialog, public = MagicMock(), MagicMock()
        dialog.is_displayed.return_value = True
        public.is_displayed.return_value = True
        public.get_attribute.return_value = 'https://www.facebook.com/marketplace/item/123/'
        public.click.side_effect = lambda: setattr(driver, 'current_url', public.get_attribute('href'))
        driver.back.side_effect = lambda: setattr(driver, 'current_url', original_url)
        edit = self.edit_link()
        dialog.find_elements.side_effect = lambda by, selector: [edit] if 'edit/' in selector else [public]
        driver.find_elements.side_effect = lambda by, selector: [dialog] if 'dialog' in selector else [edit]
        sources = {'image_urls': ['public-photo'], 'video_urls': [], 'video_errors': []}
        with patch('automation.public_listing_media.read_public_listing_media', return_value=sources) as gallery:
            self.assertEqual(reposting._public_media_in_new_tab(driver, {}), sources)
        gallery.assert_called_once_with(driver, '123')
        public.click.assert_called_once()
        driver.switch_to.new_window.assert_not_called()
        driver.get.assert_not_called()
        driver.close.assert_not_called()
        driver.back.assert_called_once()
        self.assertEqual(driver.current_window_handle, 'inventory')
        self.assertEqual(driver.current_url, original_url)


if __name__ == '__main__':
    unittest.main()
