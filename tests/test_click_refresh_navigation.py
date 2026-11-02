import unittest
from unittest.mock import MagicMock, patch, call

from selenium.common.exceptions import TimeoutException
from automation import delete_listing_import as importer


class ClickRefreshNavigationTests(unittest.TestCase):
    def test_direct_url_does_not_click_manage_or_reload_selling(self):
        driver = MagicMock()
        with patch.object(importer, '_open_available_url', return_value=True), \
             patch.object(importer, 'WebDriverWait') as wait, \
             patch.object(importer, '_click_element_safely') as click:
            self.assertEqual(importer._open_click_refresh_search_page(driver), 'Direct Available URL')
        driver.get.assert_not_called()
        click.assert_not_called()
        wait.return_value.until.assert_called_once_with(importer._find_listing_search_box)

    def test_failed_url_clicks_manage_and_waits_for_search(self):
        driver, control = MagicMock(), MagicMock()
        driver.find_elements.return_value = [control]
        with patch.object(importer, '_open_available_url', return_value=False), \
             patch.object(importer, 'WebDriverWait') as wait, \
             patch.object(importer, '_find_listing_search_box', return_value=MagicMock()), \
             patch.object(importer, '_click_element_safely', return_value=True) as click:
            wait.return_value.until.side_effect = lambda condition: condition(driver)
            self.assertEqual(importer._open_click_refresh_search_page(driver), 'Manage Listings')
        driver.get.assert_called_once_with(importer.SELLING_URL)
        click.assert_called_once_with(driver, control)

    def test_url_without_search_falls_back_to_already_open_selling_search(self):
        driver = MagicMock()
        with patch.object(importer, '_open_available_url', return_value=True), \
             patch.object(importer, 'WebDriverWait') as wait, \
             patch.object(importer, '_click_element_safely') as click:
            wait.return_value.until.side_effect = [TimeoutException(), True, ('search', MagicMock())]
            self.assertEqual(importer._open_click_refresh_search_page(driver), 'Selling search')
        driver.get.assert_called_once_with(importer.SELLING_URL)
        click.assert_not_called()

    def test_slow_fallback_navigation_still_checks_rendered_page(self):
        driver = MagicMock()
        driver.get.side_effect = TimeoutException()
        with patch.object(importer, '_open_available_url') as url, \
             patch.object(importer, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = [True, ('search', MagicMock())]
            self.assertEqual(importer._open_click_refresh_search_page(driver, prefer_url=False), 'Selling search')
        url.assert_not_called()
        driver.execute_script.assert_called_once_with('window.stop();')

    def test_metric_timeout_retries_once_and_reuses_fallback_for_next_listing(self):
        driver = MagicMock()
        listings = [dict(id=str(i), year='2020', make='Ford', model='Focus') for i in (1, 2)]
        with patch.object(importer, 'load_listings', return_value={'listings': listings}), \
             patch.object(importer.listing_store, 'is_backup', return_value=False), \
             patch.object(importer, '_open_prelogged_facebook', return_value=driver), \
             patch.object(importer, '_open_click_refresh_search_page', side_effect=['Direct Available URL', 'Manage Listings']) as page, \
             patch.object(importer, '_search_listing_click_metric', side_effect=[TimeoutException(), 12, 0]) as metric, \
             patch.object(importer.listing_store, 'update_fields') as save, \
             patch.object(importer, 'diagnostic_event'):
            importer.refresh_listing_clicks_from_facebook.__wrapped__('account')
        self.assertEqual(page.call_args_list, [call(driver), call(driver, prefer_url=False)])
        self.assertEqual([entry.args[1]['id'] for entry in metric.call_args_list], ['1', '1', '2'])
        self.assertEqual([entry.args[2]['clicks'] for entry in save.call_args_list], [12, 0])
        driver.quit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
