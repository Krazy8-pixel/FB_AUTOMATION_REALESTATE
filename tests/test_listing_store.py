import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from automation import listing_store


class ListingStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.patch = patch.object(listing_store, 'data_dir', return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.folder.cleanup()

    def test_migration_preserves_backup_and_clear_does_not_remigrate(self):
        legacy = self.root / 'old.json'
        legacy.write_text(json.dumps({'updated_at': 'old', 'listings': [{'id': 'one', 'model': 'Original'}]}))
        self.assertEqual(listing_store.load('a', legacy)['listings'][0]['model'], 'Original')
        self.assertTrue(legacy.exists())
        listing_store.save('a', [])
        self.assertEqual(listing_store.load('a', legacy)['listings'], [])

    def test_reimport_preserves_preferences_and_account_isolation(self):
        listing_store.save('a', [{'id': 'one', 'details': {'price': '100'}}])
        listing_store.save('b', [{'id': 'one', 'details': {'price': '200'}}])
        preferences = {'fields': {'price': '300'}, 'image_order': ['second', 'first']}
        self.assertTrue(listing_store.save_preferences('a', 'one', preferences))
        listing_store.save('a', [{'id': 'one', 'details': {'price': '150'}}])
        record = listing_store.load('a')['listings'][0]
        self.assertEqual(record['details']['price'], '150')
        self.assertEqual(record['repost_preferences'], preferences)
        self.assertEqual(listing_store.load('b')['listings'][0]['repost_preferences'], {})
        self.assertFalse(listing_store.save_preferences('b', 'missing', preferences))

    def test_failed_snapshot_rolls_back(self):
        listing_store.save('a', [{'id': 'one'}])
        with self.assertRaises(KeyError):
            listing_store.save('a', [{'missing': 'id'}])
        self.assertEqual(listing_store.load('a')['listings'][0]['id'], 'one')

    def test_insert_at_top_preserves_other_records_preferences_and_import_clock(self):
        listing_store.save('a', [{'id': 'old'}])
        listing_store.save_preferences('a', 'old', {'fields': {'price': '100'}})
        listing_store.mark_import_complete('a')
        imported = listing_store.load('a')['last_import_at']
        listing_store.add_at_top('a', {'id': 'new', 'clicks': 0})
        saved = listing_store.load('a')
        self.assertEqual([item['id'] for item in saved['listings']], ['new', 'old'])
        self.assertEqual(saved['listings'][1]['repost_preferences']['fields']['price'], '100')
        self.assertEqual(saved['last_import_at'], imported)
        self.assertEqual(listing_store.load('b')['listings'], [])

    def test_remove_is_account_scoped_and_does_not_remove_other_backups(self):
        listing_store.save('a', [{'id': 'one'}, {'id': 'two'}])
        listing_store.save('b', [{'id': 'one'}])
        self.assertEqual(listing_store.remove('a', ['one']), 1)
        self.assertEqual([item['id'] for item in listing_store.load('a')['listings']], ['two'])
        self.assertEqual(len(listing_store.load('b')['listings']), 1)

    def test_import_clock_is_independent_of_snapshot_updates(self):
        listing_store.save('a', [{'id': 'one'}])
        listing_store.mark_import_complete('a')
        imported = listing_store.load('a')['last_import_at']
        listing_store.save('a', [{'id': 'one', 'clicks': 20}])
        listing_store.remove('a', ['one'])
        self.assertEqual(listing_store.load('a')['last_import_at'], imported)

    def test_move_to_top_is_atomic_account_scoped_and_preserves_refresh_clock(self):
        listing_store.save('a', [{'id': 'one'}, {'id': 'two'}, {'id': 'three'}])
        listing_store.save('b', [{'id': 'one'}, {'id': 'two'}])
        listing_store.mark_import_complete('a')
        imported = listing_store.load('a')['last_import_at']
        self.assertTrue(listing_store.update_fields('a', 'three', {'clicks': 0}, move_to_top=True))
        self.assertEqual([item['id'] for item in listing_store.load('a')['listings']], ['three', 'one', 'two'])
        self.assertTrue(listing_store.update_fields('a', 'two', {}, move_to_top=True))
        self.assertFalse(listing_store.update_fields('a', 'missing', {}, move_to_top=True))
        self.assertEqual([item['id'] for item in listing_store.load('a')['listings']], ['two', 'three', 'one'])
        self.assertEqual([item['id'] for item in listing_store.load('b')['listings']], ['one', 'two'])
        self.assertEqual(listing_store.load('a')['last_import_at'], imported)

    def test_backups_shared_but_live_listings_account_scoped(self):
        listing_store.save('a', [{'id':'live-a'}, {'id':'backup-a', 'facebook_present':False}])
        listing_store.save('b', [{'id':'live-b'}, {'id':'backup-b', 'source_deleted':True}])
        self.assertEqual({item['id'] for item in listing_store.load('a')['listings']}, {'live-a','backup-a','backup-b'})
        self.assertEqual({item['id'] for item in listing_store.load('b')['listings']}, {'live-b','backup-a','backup-b'})
        listing_store.save_preferences('b', 'backup-a', {'fields':{'price':'100'}})
        self.assertEqual(next(item for item in listing_store.load('a')['listings'] if item['id']=='backup-a')['repost_preferences']['fields']['price'], '100')
        listing_store.save('b', [{'id':'live-b-new'}])
        self.assertEqual({item['id'] for item in listing_store.load('b')['listings']}, {'live-b-new','backup-a','backup-b'})
        self.assertTrue(listing_store.remove_backup('b', 'backup-a'))
        self.assertEqual(listing_store.clear_backups('a'), 1)
        self.assertEqual([item['id'] for item in listing_store.load('a')['listings']], ['live-a'])
        self.assertEqual([item['id'] for item in listing_store.load('b')['listings']], ['live-b-new'])

    def test_confirmed_deletion_shares_backup_and_repost_returns_to_source_only(self):
        listing_store.save('a', [{'id':'one', 'details':{'title':'Vehicle'}}])
        listing_store.save_preferences('a', 'one', {'fields':{'price':'200'}})
        listing_store.update_fields('a', 'one', {'source_deleted':True, 'facebook_present':False})
        self.assertEqual(listing_store.load('a')['listings'], listing_store.load('b')['listings'])
        listing_store.update_fields('a', 'one', {'source_deleted':True, 'facebook_reposted':True, 'facebook_present':True})
        self.assertEqual(listing_store.load('b')['listings'], [])
        self.assertEqual(listing_store.load('a')['listings'][0]['repost_preferences']['fields']['price'], '200')

    def test_existing_account_backups_migrate_without_changing_media_or_live_rows(self):
        import sqlite3
        db = sqlite3.connect(str(self.root / 'listings.db'))
        db.execute('CREATE TABLE listings (account TEXT,id TEXT,position INTEGER,data TEXT,preferences TEXT,PRIMARY KEY(account,id))')
        for account, item in [('a', {'id':'live-a'}), ('b', {'id':'live-b'}), ('a', {'id':'backup', 'facebook_present':False, 'downloaded_images':[{'name':'photo.jpg','path':'original.jpg'}]})]:
            db.execute('INSERT INTO listings VALUES (?,?,?,?,?)', (account,item['id'],0,json.dumps(item),json.dumps({'fields':{'price':'300'}})))
        db.commit()
        db.close()
        self.assertEqual({item['id'] for item in listing_store.load('b')['listings']}, {'live-b','backup'})
        backup = next(item for item in listing_store.load('a')['listings'] if item['id']=='backup')
        self.assertEqual(backup['downloaded_images'][0]['path'], 'original.jpg')
        self.assertEqual(backup['repost_preferences']['fields']['price'], '300')
        listing_store.clear_backups('b')
        self.assertEqual([item['id'] for item in listing_store.load('a')['listings']], ['live-a'])


if __name__ == '__main__':
    unittest.main()
