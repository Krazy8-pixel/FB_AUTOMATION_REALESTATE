import threading
import unittest
from unittest.mock import MagicMock, call, patch

from automation import delete_listing_import as importer


class DeleteSearchFlowTests(unittest.TestCase):
    def setUp(self):
        self.driver = MagicMock()
        self.stored = {'id': 'one', 'year': '2018', 'make': 'Dodge', 'model': 'Challenger',
                       'price': '21000', '_match_by_vehicle_details_only': True}
        self.card = {'title': '2018 Dodge Challenger', 'text': 'MAD 21,000', 'element': MagicMock()}
        self.events = []
        self.stop = threading.Event()
        self.patchers = []

    def stub(self, name, **kwargs):
        patcher = patch.object(importer, name, **kwargs)
        self.patchers.append(patcher)
        return patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def prepare(self):
        self.stub('_open_prelogged_facebook', return_value=self.driver)
        self.stub('_open_delete_selling_page')
        self.search = self.stub('_search_delete_candidate', return_value=self.card)
        self.manage = self.stub('_click_manage_listings', return_value=True)
        self.scan = self.stub('_scan_delete_candidate', return_value=self.card)
        self.stub('_wait_for_inventory')
        self.open_card = self.stub('_open_delete_candidate')
        self.dialog = self.stub('_complete_facebook_delete_dialog')
        self.stub('diagnostic_event')

    def run_delete(self):
        return importer.delete_selected_listings_from_facebook.__wrapped__(
            'account', [self.stored], stop_event=self.stop, progress_callback=self.events.append)

    def test_search_succeeds_without_opening_manage_listings(self):
        self.prepare()
        result = self.run_delete()
        self.assertEqual(result['deleted_ids'], ['one'])
        self.assertEqual([event['status'] for event in self.events], ['searching', 'deleted'])
        self.assertEqual(self.events[-1]['method'], 'Selling search')
        self.manage.assert_not_called()
        self.scan.assert_not_called()
        self.dialog.assert_called_once()
        self.driver.quit.assert_called_once()

    def test_missing_search_result_uses_manage_listings(self):
        self.prepare()
        self.search.return_value = None
        result = self.run_delete()
        self.assertEqual(result['deleted_ids'], ['one'])
        self.assertEqual([event['status'] for event in self.events], ['searching', 'fallback', 'deleted'])
        self.assertEqual(self.events[-1]['method'], 'Manage Listings')
        self.manage.assert_called_once()
        self.scan.assert_called_once()

    def test_both_methods_missing_marks_one_failure(self):
        self.prepare()
        self.search.return_value = None
        self.scan.return_value = None
        result = self.run_delete()
        self.assertEqual(result['not_found_ids'], ['one'])
        self.assertEqual(self.events[-1]['status'], 'failed')
        self.assertEqual(self.events[-1]['error'], 'Listing not present on Facebook.')
        self.dialog.assert_not_called()

    def test_unconfirmed_delete_does_not_attempt_a_second_deletion(self):
        self.prepare()
        def uncertain(driver, timeout, on_delete_started=None):
            on_delete_started()
            raise RuntimeError('Confirmation timed out')
        self.dialog.side_effect = uncertain
        result = self.run_delete()
        self.assertEqual(result['deleted_ids'], [])
        self.assertEqual(self.events[-1]['status'], 'failed')
        self.assertIn('Confirmation timed out', self.events[-1]['error'])
        self.manage.assert_not_called()

    def test_missing_manage_page_is_reported_as_an_error_not_absence(self):
        self.prepare()
        self.search.return_value = None
        self.manage.return_value = False
        self.scan.return_value = None
        result = self.run_delete()
        self.assertEqual(result['not_found_ids'], [])
        self.assertEqual(self.events[-1]['status'], 'failed')
        self.assertIn('Could not open Manage Listings', self.events[-1]['error'])

    def test_unopenable_search_match_is_not_reported_as_absent(self):
        self.prepare()
        self.open_card.side_effect = RuntimeError('Could not open the search result')
        self.scan.return_value = None
        result = self.run_delete()
        self.assertEqual(result['not_found_ids'], [])
        self.assertEqual(self.events[-1]['status'], 'failed')
        self.assertIn('search found the listing', self.events[-1]['error'])

    def test_unavailable_search_is_not_reported_as_a_confirmed_absence(self):
        self.prepare()
        self.search.side_effect = RuntimeError('Search input unavailable')
        self.scan.return_value = None
        result = self.run_delete()
        self.assertEqual(result['not_found_ids'], [])
        self.assertIn('Selling search failed', self.events[-1]['error'])

    def test_search_uses_clean_vehicle_name_and_verifies_card_details(self):
        search_box = MagicMock()
        dirty = dict(self.stored, model='2018 Dodge Challenger \u00b7 SCAT PACK, MAD21,000, Active')
        wrong = {'title': '2018 Dodge Charger', 'text': 'MAD 21,000', 'element': MagicMock()}

        class ImmediateWait:
            def __init__(self, driver, timeout, poll_frequency=None):
                self.driver = driver

            def until(self, condition):
                result = condition(self.driver)
                if not result:
                    raise AssertionError('Expected a ready search field or matching card')
                return result

        with patch.object(importer, 'WebDriverWait', ImmediateWait), \
             patch.object(importer, '_find_listing_search_box', return_value=search_box), \
             patch.object(importer, 'extract_delete_candidates', return_value=[wrong, self.card]), \
             patch.object(importer.time, 'sleep'):
            candidate = importer._search_delete_candidate(self.driver, dirty, self.stop)
        self.assertIs(candidate, self.card)
        self.assertIn(call('2018 Dodge Challenger'), search_box.send_keys.call_args_list)

    def test_search_field_uses_span_before_supplied_input_attributes(self):
        box = MagicMock()
        self.driver.find_elements.side_effect = [[], [], [box]]
        self.assertIs(importer._find_listing_search_box(self.driver), box)
        locators = [entry.args[1] for entry in self.driver.find_elements.call_args_list]
        self.assertIn('//span[', locators[0])
        self.assertIn('@placeholder', locators[2])


if __name__ == '__main__':
    unittest.main()
