import unittest
from unittest.mock import MagicMock, patch

import test_listing_routes as listing_route_fixture
from automation import listing_store


class DeleteResultsFlowTests(unittest.TestCase):
    setUpClass = classmethod(listing_route_fixture.ListingRouteTests.setUpClass.__func__)
    setUp = listing_route_fixture.ListingRouteTests.setUp
    tearDown = listing_route_fixture.ListingRouteTests.tearDown

    def test_duplicate_local_id_prefers_live_listing_and_preserves_backup(self):
        live = listing_store.load('test-account')['listings'][0]
        backup = dict(live, facebook_present=False, source_deleted=True, make='Backup')
        listing_store.add_at_top('test-account', backup)
        workers = []
        def delete(account, items, **kwargs):
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['make'], 'Ford')
            kwargs['progress_callback']({'listing_id':'one', 'status':'deleted', 'confirmed_deleted':True})
            return {'deleted_ids':['one'], 'deleted':1}
        with patch.object(self.routes.threading, 'Thread', side_effect=lambda *args, **kwargs: (workers.append(kwargs['target']) or MagicMock())), \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', side_effect=delete), \
             patch.object(self.routes, 'submit_failure'), patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/delete', json={'ids':['one'], 'account':'test-account'})
            self.assertEqual(response.status_code, 200)
            workers[0]()
        backups = listing_store.load('test-account')['listings']
        self.assertEqual(len(backups), 2)
        self.assertEqual({item['make'] for item in backups}, {'Backup', 'Ford'})
        self.assertEqual(len({item['id'] for item in backups}), 2)

    def test_missing_selection_does_not_block_valid_listing(self):
        workers = []
        with patch.object(self.routes.threading, 'Thread', side_effect=lambda *args, **kwargs: (workers.append(kwargs['target']) or MagicMock())), \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', return_value={'deleted_ids':['one'], 'deleted':1}), \
             patch.object(self.routes, 'submit_failure'), patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/delete', json={'ids':['one','missing'], 'account':'test-account',
                                        'selection_titles':{'missing':'2019 Toyota RAV4'}})
            self.assertEqual(response.status_code, 200)
            workers[0]()
        items = self.client.get('/delete_results/data').json['job']['items']
        missing = next(item for item in items if item['listing_id'] == 'missing')
        self.assertEqual(missing['title'], '2019 Toyota RAV4')
        self.assertEqual(missing['status'], 'failed')
        self.assertIn('select it again', missing['error'])

    def test_search_and_fallback_progress_and_failure_are_visible(self):
        workers = []
        def make_worker(*args, **kwargs):
            workers.append(kwargs['target'])
            return MagicMock()

        def delete(account, items, **kwargs):
            callback = kwargs['progress_callback']
            callback({'listing_id': 'one', 'status': 'searching', 'method': 'Selling search',
                      'detail': 'Searching Facebook...', 'message': 'Searching Facebook...', 'clicked': 0, 'deleted': 0})
            item = self.client.get('/delete_results/data').json['job']['items'][0]
            self.assertEqual((item['status'], item['method']), ('searching', 'Selling search'))
            callback({'listing_id': 'one', 'status': 'fallback', 'method': 'Manage Listings',
                      'detail': 'Checking Manage Listings...', 'message': 'Checking Manage Listings...', 'clicked': 0, 'deleted': 0})
            callback({'listing_id': 'one', 'status': 'failed', 'method': 'Manage Listings',
                      'error': 'Listing not present on Facebook.', 'message': 'Listing not found.', 'clicked': 0, 'deleted': 0})
            return {'deleted': 0, 'deleted_ids': [], 'not_found': 1, 'not_found_ids': ['one']}

        with patch.object(self.routes.threading, 'Thread', side_effect=make_worker), \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', side_effect=delete), \
             patch.object(self.routes, 'submit_failure'), patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/delete', json={'ids': ['one'], 'account': 'test-account'})
            self.assertEqual(response.status_code, 200)
            workers[0]()
        item = self.client.get('/delete_results/data').json['job']['items'][0]
        self.assertEqual((item['status'], item['method'], item['error']),
                         ('failed', 'Manage Listings', 'Listing not present on Facebook.'))
        html = self.client.get('/delete_results').get_data(as_text=True)
        self.assertNotIn('<th>Method</th>', html)
        self.assertIn('<th>What happened</th>', html)
        self.assertIn('Listing not present on Facebook.', html)


if __name__ == '__main__':
    unittest.main()
