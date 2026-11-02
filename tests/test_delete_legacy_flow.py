import unittest
from unittest.mock import MagicMock, patch, call

from automation import delete_listing_import as importer


class DeleteLegacyFlowTests(unittest.TestCase):
    def test_deletion_uses_manage_listings_scan_for_each_selected_listing(self):
        driver = MagicMock()
        listings = [
            {'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus', '_source_listing_id': '123'},
            {'id': 'two', 'year': '2021', 'make': 'Ford', 'model': 'Transit', '_source_listing_id': '456'},
        ]
        cards = [
            {'title': '2020 Ford Focus', 'text': '$100', 'element': MagicMock(), 'source': 'full_row_dom'},
            {'title': '2021 Ford Transit', 'text': '$200', 'element': MagicMock(), 'source': 'full_row_dom'},
        ]
        with patch.object(importer, '_open_prelogged_facebook', return_value=driver), \
             patch.object(importer, '_click_manage_listings') as manage, \
             patch.object(importer, 'extract_delete_candidates', side_effect=[[cards[0]], [cards[1]]]), \
             patch.object(importer, '_verify_repost_source', return_value=True) as verify, \
             patch.object(importer, '_complete_facebook_delete_dialog') as confirm, \
             patch.object(importer, '_search_listing_click_metric') as search, \
             patch.object(importer, 'WebDriverWait'), patch.object(importer, 'diagnostic_event'):
            result = importer.delete_selected_listings_from_facebook.__wrapped__('account', listings)
        self.assertEqual(driver.get.call_args_list, [call(importer.SELLING_URL), call(importer.SELLING_URL)])
        self.assertEqual(manage.call_count, 2)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(confirm.call_count, 2)
        search.assert_not_called()
        self.assertEqual(result['deleted_ids'], ['one', 'two'])
        driver.quit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
