import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from automation import delete_listing_import as importer
from automation.chromedriver import build_chrome_driver


class RefreshScanTests(unittest.TestCase):
    def setUp(self):
        self.driver = MagicMock()
        self.state = {'y': 0, 'height': 1000, 'viewport': 1000}
        self.driver.execute_script.side_effect = lambda script, *args: (
            dict(self.state) if script.startswith('return {y:') else None)
        self.listings = [
            {'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus',
             'url': 'https://www.facebook.com/marketplace/item/123/'},
            {'id': 'two', 'year': '2021', 'make': 'Ford', 'model': 'Transit',
             'url': 'https://www.facebook.com/marketplace/item/456/'},
        ]

    def card(self, title, clicks, facebook_id='', element_id='row'):
        element = MagicMock()
        element.id = element_id
        element.is_displayed.return_value = True
        return {'title': title, 'text': f'{clicks} clicks on listing',
                'href': f'https://www.facebook.com/marketplace/item/{facebook_id}/' if facebook_id else '',
                'source': 'full_row_dom', 'element': element}

    def scan(self, listings, candidates):
        with patch.object(importer, '_click_scan_candidates', return_value=candidates), \
             patch.object(importer, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = TimeoutException()
            return importer._scan_listing_click_counts(self.driver, listings)

    def test_exact_ids_match_and_zero_clicks_are_saved(self):
        cards = [self.card('2020 Ford Focus', 0, '123', 'one'),
                 self.card('2021 Ford Transit', 17, '456', 'two')]
        with patch.object(importer, '_click_scan_candidates', return_value=cards), \
             patch.object(importer, 'WebDriverWait') as wait:
            result = importer._scan_listing_click_counts(self.driver, self.listings)
        self.assertEqual(result, {0: 0, 1: 17})
        wait.assert_not_called()

    def test_same_title_without_ids_is_left_for_search(self):
        same = [{'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus'},
                {'id': 'two', 'year': '2020', 'make': 'Ford', 'model': 'Focus'}]
        cards = [self.card('2020 Ford Focus', 4, element_id='one'),
                 self.card('2020 Ford Focus', 9, element_id='two')]
        self.assertEqual(self.scan(same, cards), {})

    def test_one_saved_listing_with_two_unknown_cards_is_ambiguous(self):
        saved = [{'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus'}]
        cards = [self.card('2020 Ford Focus', 4, element_id='one'),
                 self.card('2020 Ford Focus', 9, element_id='two')]
        self.assertEqual(self.scan(saved, cards), {})

    def test_generic_url_does_not_hide_the_edit_url_facebook_id(self):
        saved = [{'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus',
                  'url': 'https://www.facebook.com/marketplace/',
                  'edit_url': 'https://www.facebook.com/marketplace/edit/?listing_id=123'}]
        wrong = self.card('2020 Ford Focus', 4, '456', 'wrong')
        self.assertEqual(self.scan(saved, [wrong]), {})
        self.assertEqual(self.scan(saved, [self.card('2020 Ford Focus', 9, '123', 'right')]), {0: 9})

    def test_late_loaded_cards_at_bottom_are_scanned(self):
        first = self.card('2020 Ford Focus', 3, '123', 'one')
        second = self.card('2021 Ford Transit', 4, '456', 'two')
        def next_batch(condition):
            self.state['height'] = 2000
            return condition(self.driver)
        with patch.object(importer, '_click_scan_candidates', side_effect=[[first], [first, second], [first, second]]), \
             patch.object(importer, 'WebDriverWait') as wait, patch.object(importer.time, 'sleep'):
            wait.return_value.until.side_effect = next_batch
            result = importer._scan_listing_click_counts(self.driver, self.listings)
        self.assertEqual(result, {0: 3, 1: 4})

    def test_only_unresolved_listings_use_search(self):
        with patch.object(importer, 'load_listings', return_value={'listings': self.listings}), \
             patch.object(importer.listing_store, 'is_backup', return_value=False), \
             patch.object(importer, '_open_prelogged_facebook', return_value=self.driver), \
             patch.object(importer, '_open_click_refresh_search_page', return_value='Direct Available URL'), \
             patch.object(importer, '_scan_listing_click_counts', return_value={0: 7}), \
             patch.object(importer, '_search_listing_click_metric', return_value=3) as search, \
             patch.object(importer.listing_store, 'update_fields') as save, \
             patch.object(importer, 'diagnostic_event'):
            importer.refresh_listing_clicks_from_facebook.__wrapped__('account')
        search.assert_called_once_with(self.driver, self.listings[1], '2021 Ford Transit', timeout=30,
                                       known_listings=self.listings)
        self.assertEqual([entry.args[2]['clicks'] for entry in save.call_args_list], [7, 3])

    def test_failed_scan_searches_every_listing(self):
        with patch.object(importer, 'load_listings', return_value={'listings': self.listings}), \
             patch.object(importer.listing_store, 'is_backup', return_value=False), \
             patch.object(importer, '_open_prelogged_facebook', return_value=self.driver), \
             patch.object(importer, '_open_click_refresh_search_page', return_value='Direct Available URL'), \
             patch.object(importer, '_scan_listing_click_counts', side_effect=RuntimeError('page changed')), \
             patch.object(importer, '_search_listing_click_metric', side_effect=[1, 2]) as search, \
             patch.object(importer.listing_store, 'update_fields') as save, \
             patch.object(importer, 'diagnostic_event'):
            importer.refresh_listing_clicks_from_facebook.__wrapped__('account')
        self.assertEqual(search.call_count, 2)
        self.assertEqual([entry.args[2]['clicks'] for entry in save.call_args_list], [1, 2])

    def test_search_does_not_assign_a_shared_title_without_card_id(self):
        saved = [{'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus'},
                 {'id': 'two', 'year': '2020', 'make': 'Ford', 'model': 'Focus'}]
        candidate = self.card('2020 Ford Focus', 4)
        search_box = MagicMock()
        def until(condition):
            if condition is importer._find_listing_search_box:
                return search_box
            result = condition(self.driver)
            if not result:
                raise TimeoutException()
            return result
        with patch.object(importer, '_click_scan_candidates', return_value=[candidate]), \
             patch.object(importer, 'WebDriverWait') as wait, patch.object(importer.time, 'sleep'):
            wait.return_value.until.side_effect = until
            with self.assertRaises(TimeoutException):
                importer._search_listing_click_metric(self.driver, saved[0], '2020 Ford Focus',
                                                      known_listings=saved)


class RefreshScanBrowserTests(unittest.TestCase):
    def test_scan_reads_distinct_cards_from_rendered_inventory(self):
        options = Options()
        options.add_argument('--headless=new')
        driver = build_chrome_driver(options)
        try:
            html = '''<main role="main">
            <div><input aria-label="Select Listing" type="checkbox"><img src="data:,">
            <div role="button" aria-label="2020 Ford Focus">2020 Ford Focus</div>
            <span>12 clicks on listing</span><a style="display:block" href="https://www.facebook.com/marketplace/item/123/">View</a>
            <div role="button" aria-label="More options for 2020 Ford Focus"></div></div>
            <div><input aria-label="Select Listing" type="checkbox"><img src="data:,">
            <div role="button" aria-label="2021 Ford Transit">2021 Ford Transit</div>
            <span>0 clicks on listing</span><a style="display:block" href="https://www.facebook.com/marketplace/item/456/">View</a>
            <div role="button" aria-label="More options for 2021 Ford Transit"></div></div></main>'''
            driver.get('data:text/html,' + quote(html))
            listings = [{'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus',
                         'url': 'https://www.facebook.com/marketplace/item/123/'},
                        {'id': 'two', 'year': '2021', 'make': 'Ford', 'model': 'Transit',
                         'url': 'https://www.facebook.com/marketplace/item/456/'}]
            candidates = importer._click_scan_candidates(driver)
            context = [(item.get('title'), item.get('text'), item.get('href'),
                        item.get('element').is_displayed(), importer._confirmed_click_count(item))
                       for item in candidates]
            self.assertEqual(importer._scan_listing_click_counts(driver, listings), {0: 12, 1: 0}, context)
        finally:
            driver.quit()


if __name__ == '__main__':
    unittest.main()
