from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from PIL import Image
from automation import delete_listing_import as importer
from automation import reposting


class ImportDetailsTests(unittest.TestCase):
    def test_fallback_fills_only_unreadable_fields_once(self):
        post = {'price': '100', 'description': '', 'model': '',
                'field_states': {'price': 'read', 'description': 'empty', 'model': 'unavailable'}}
        fallback = MagicMock(return_value={'posts': [{'price': '999', 'description': 'wrong',
            'model': 'Civic', 'exterior_color': 'Blue', 'field_states':
            {'price': 'read', 'description': 'read', 'model': 'read', 'exterior_color': 'read'}}]})
        reposting.recover_unreadable_fields(MagicMock(), post, fallback)
        fallback.assert_called_once()
        self.assertEqual(post['price'], '100')
        self.assertEqual(post['description'], '')
        self.assertEqual(post['model'], 'Civic')
        self.assertEqual(post['exterior_color'], 'Blue')
        self.assertEqual(post['field_sources']['model'], 'visual_fallback')

    def test_optional_colours_alone_do_not_trigger_fallback(self):
        post = {key: 'saved' for key in reposting.EDIT_FIELDS}
        post['field_states'] = {key: 'read' for key in reposting.EDIT_FIELDS}
        for key in ('exterior_color', 'interior_color'):
            post[key] = ''
            post['field_states'][key] = 'unavailable'
        fallback = MagicMock()
        reposting.recover_unreadable_fields(MagicMock(), post, fallback)
        fallback.assert_not_called()

    def test_fallback_error_preserves_primary_result(self):
        post = {'price': '100', 'field_states': {'price': 'read'}}
        reposting.recover_unreadable_fields(MagicMock(), post,
            MagicMock(side_effect=RuntimeError('OCR failed')))
        self.assertEqual(post['price'], '100')
        self.assertEqual(post['fallback_error'], 'OCR failed')

    def test_edit_read_returns_to_inventory_without_public_gallery_navigation(self):
        driver = MagicMock()
        edit_url = 'https://www.facebook.com/marketplace/edit/?listing_id=123'
        driver.current_url = edit_url
        driver.find_elements.return_value = []
        link = MagicMock()
        link.get_attribute.return_value = edit_url
        snapshot = {key: {'value': 'saved', 'state': 'read'} for key in reposting.EDIT_FIELDS}
        snapshot['price']['value'] = '100'
        with patch.object(reposting, '_open_manage_listings', return_value=True) as manage, \
             patch.object(reposting, '_find_listing_card_with_scroll', return_value=MagicMock()), \
             patch.object(reposting, '_safe_click', return_value=True), \
             patch.object(reposting, '_model_from_visible_listing_heading', return_value='Focus'), \
             patch.object(reposting, '_find_edit_listing_link', return_value=link), \
             patch.object(reposting, 'WebDriverWait'), \
             patch.object(reposting, 'wait_for_edit_form_ready', return_value=snapshot), \
             patch.object(reposting, 'read_edit_form_snapshot', return_value=snapshot), \
             patch.object(reposting, '_scroll_down_page'), \
             patch.object(reposting, '_safe_image_urls', return_value=['edit-photo']), \
             patch.object(reposting, 'highest_resolution_listing_images') as gallery:
            result = reposting.read_listing_from_manage('account',
                {'year': '2020', 'make': 'Ford', 'model': 'Focus'}, driver=driver)
        self.assertEqual(result['posts'][0]['price'], '100')
        self.assertEqual(result['posts'][0]['image_urls'], ['edit-photo'])
        self.assertEqual(manage.call_count, 2)
        gallery.assert_not_called()
        driver.get.assert_not_called()
        driver.switch_to.new_window.assert_not_called()

    def test_full_details_are_saved_using_same_browser(self):
        driver = MagicMock()
        driver.execute_script.return_value = 100
        candidate = {'href': 'https://www.facebook.com/marketplace/item/123/',
                     'title': '2020 Ford Focus', 'text': '$100\n5 clicks on listing'}
        details = {'title': '2020 Ford Focus', 'description': 'Full description', 'image_urls': ['photo']}
        with patch.object(importer, '_open_prelogged_facebook', return_value=driver), \
             patch.object(importer, 'load_listings', return_value={'listings': []}), \
             patch.object(importer, 'WebDriverWait'), \
             patch.object(importer, '_click_manage_listings'), \
             patch.object(importer, '_extract_visible_listing_candidates', return_value=[candidate]), \
             patch.object(importer, 'read_listing_from_manage', return_value={'posts': [details], 'edit_url': 'edit'}) as reader, \
             patch.object(importer, 'download_listing_images', return_value=([{'name': 'photo', 'path': 'local'}], [])), \
             patch.object(importer, 'save_listings') as save, \
             patch.object(importer.time, 'sleep'):
            result = importer.import_listings_from_facebook('account', replace_existing=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['details']['description'], 'Full description')
        self.assertEqual(result[0]['downloaded_images'][0]['path'], 'local')
        self.assertIs(reader.call_args.kwargs['driver'], driver)
        self.assertTrue(save.called)
        driver.quit.assert_called_once()

    def test_download_retains_exact_bytes_dimensions_and_order(self):
        stream = BytesIO()
        Image.new('RGB', (1000, 800), 'blue').save(stream, format='JPEG')
        content = stream.getvalue()
        response = MagicMock(content=content)
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        driver = MagicMock()
        driver.execute_script.return_value = 'test-agent'
        driver.get_cookies.return_value = []
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(importer, 'IMPORT_DIR', Path(folder)), \
             patch.object(importer.requests, 'Session', return_value=session):
            images, errors = importer.download_listing_images(driver, 'account', 'id',
                ['https://scontent.example.fbcdn.net/photo.jpg'], threading.Event())
            self.assertEqual(errors, [])
            self.assertEqual(Path(images[0]['path']).read_bytes(), content)
            self.assertEqual((images[0]['width'], images[0]['height']), (1000, 800))

    def test_distinct_listing_urls_do_not_merge_same_vehicle_titles(self):
        first = {'year': '2020', 'make': 'Ford', 'model': 'Focus', 'url': 'https://www.facebook.com/marketplace/item/1/'}
        second = dict(first, url='https://www.facebook.com/marketplace/item/2/')
        key = importer._matching_record_key({importer._record_key(first): first}, second)
        self.assertNotEqual(key, importer._record_key(first))

    def test_photo_saves_under_long_windows_project_path(self):
        stream = BytesIO()
        Image.new('RGB', (1000, 800), 'green').save(stream, format='JPEG')
        content = stream.getvalue()
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = MagicMock(content=content)
        driver = MagicMock()
        driver.execute_script.return_value = 'test-agent'
        driver.get_cookies.return_value = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ('x' * max(1, 165 - len(temporary) - 1))
            with patch.object(importer, 'IMPORT_DIR', root), patch.object(importer.requests, 'Session', return_value=session):
                images, errors = importer.download_listing_images(driver, 'account', 'x' * 20,
                    ['https://scontent.example.fbcdn.net/photo.jpg'], threading.Event())
            self.assertEqual(errors, [])
            self.assertLess(len(images[0]['path'] + '.tmp'), 260)
            self.assertEqual(Path(images[0]['path']).read_bytes(), content)

    def test_original_source_id_must_match_before_repost_deletion(self):
        link = MagicMock()
        link.get_attribute.return_value = 'https://www.facebook.com/marketplace/edit/?listing_id=999'
        with patch.object(importer, '_find_edit_listing_link', return_value=link):
            self.assertFalse(importer._verify_repost_source(MagicMock(), {'_source_listing_id': '123'}))
            self.assertTrue(importer._verify_repost_source(MagicMock(), {'_source_listing_id': '999'}))

    def test_daily_import_keeps_backups_and_reads_only_new_live_listing(self):
        driver = MagicMock()
        driver.execute_script.return_value = 100
        candidate = {'href': '', 'title': '2020 Ford Focus', 'text': '$100\n5 clicks on listing'}
        old = {'id': importer._listing_from_candidate(candidate)['id'], 'year': '2020', 'make': 'Ford',
               'model': 'Focus', 'source_deleted': True, 'details': {'title': 'Saved original'},
               'downloaded_images': [{'name': 'old.jpg', 'path': 'old.jpg'}]}
        absent = {'id': 'absent', 'year': '2018', 'make': 'Ford', 'model': 'Transit', 'details': {'title': 'Absent but saved'}}
        details = {'title': '2020 Ford Focus', 'price': '100', 'image_urls': ['photo']}
        with patch.object(importer, 'load_listings', return_value={'listings': [old, absent]}), \
             patch.object(importer, '_open_prelogged_facebook', return_value=driver) as opener, \
             patch.object(importer, 'WebDriverWait'), patch.object(importer, '_click_manage_listings'), \
             patch.object(importer, '_extract_visible_listing_candidates', return_value=[candidate]), \
             patch.object(importer, 'read_listing_from_manage', return_value={'posts': [details], 'edit_url': 'edit'}) as reader, \
             patch.object(importer, 'download_listing_images', return_value=([{'name': 'new.jpg', 'path': 'new.jpg'}], [])), \
             patch.object(importer, 'save_listings'), patch.object(importer.time, 'sleep'):
            result = importer.import_listings_from_facebook('account', replace_existing=True, only_new=True)
        self.assertEqual(len(result), 3)
        self.assertEqual(len({item['id'] for item in result}), 3)
        self.assertEqual(result[0]['details']['title'], 'Saved original')
        self.assertEqual(result[1]['details']['title'], 'Absent but saved')
        reader.assert_called_once()
        self.assertNotEqual(reader.call_args.args[1]['id'], old['id'])
        opener.assert_called_once_with('account', force_headless=True)

    def test_daily_import_skips_complete_existing_details(self):
        driver = MagicMock()
        driver.execute_script.return_value = 100
        candidate = {'href': 'https://www.facebook.com/marketplace/item/123/', 'title': '2020 Ford Focus'}
        record = dict(importer._listing_from_candidate(candidate), details={'title': 'Saved'}, downloaded_images=[{'path': 'photo.jpg'}])
        with patch.object(importer, 'load_listings', return_value={'listings': [record]}), \
             patch.object(importer, '_open_prelogged_facebook', return_value=driver), \
             patch.object(importer, 'WebDriverWait'), patch.object(importer, '_click_manage_listings'), \
             patch.object(importer, '_extract_visible_listing_candidates', return_value=[candidate]), \
             patch.object(importer, 'read_listing_from_manage') as reader, \
             patch.object(importer.os.path, 'isfile', return_value=True), \
             patch.object(importer, 'save_listings'), patch.object(importer.time, 'sleep'):
            result = importer.import_listings_from_facebook('account', only_new=True)
        reader.assert_not_called()
        self.assertEqual(result[0]['details']['title'], 'Saved')

    def test_delete_skips_same_title_with_wrong_facebook_id(self):
        driver = MagicMock()
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        first, second = MagicMock(), MagicMock()
        candidates = [dict(title='2020 Ford Focus', text='$100', element=element, source='full_row_dom')
                      for element in (first, second)]
        stored = {'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus', 'price': '$100', '_source_listing_id': '123'}
        with patch.object(importer, '_open_prelogged_facebook', return_value=driver), \
             patch.object(importer, 'WebDriverWait'), patch.object(importer, '_click_manage_listings'), \
             patch.object(importer, '_extract_visible_listing_candidates', return_value=candidates), \
             patch.object(importer, '_verify_repost_source', side_effect=[False, True]) as verify, \
             patch.object(importer, '_complete_facebook_delete_dialog') as confirm:
            result = importer.delete_selected_listings_from_facebook('account', [stored], stop_event=stop)
        first.click.assert_called_once()
        second.click.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        confirm.assert_called_once()
        self.assertEqual(result['deleted_ids'], ['one'])

    def test_empty_scan_keeps_all_same_title_saved_backups(self):
        driver = MagicMock()
        driver.execute_script.return_value = 100
        first = {'id': 'one', 'year': '2019', 'make': 'Ford', 'model': 'Transit', 'details': {'title': 'First saved listing'}}
        second = dict(first, id='two', details={'title': 'Second saved listing'})
        with patch.object(importer, 'load_listings', return_value={'listings': [first, second]}), \
             patch.object(importer, '_open_prelogged_facebook', return_value=driver), \
             patch.object(importer, 'WebDriverWait'), patch.object(importer, '_click_manage_listings'), \
             patch.object(importer, '_extract_visible_listing_candidates', return_value=[]), \
             patch.object(importer, 'read_listing_from_manage') as reader, \
             patch.object(importer, 'save_listings'), patch.object(importer.time, 'sleep'):
            result = importer.import_listings_from_facebook('account', only_new=True)
        self.assertEqual([item['id'] for item in result], ['one', 'two'])
        reader.assert_not_called()


    def mock_inventory_import(self, existing, candidates, *, empty=False, url=importer.SELLING_URL, scroll_error=False):
        driver = MagicMock()
        driver.current_url = url
        def script(code):
            if 'you haven' in code:
                return empty
            if scroll_error:
                raise RuntimeError('Scroll failed')
            return 100
        driver.execute_script.side_effect = script
        with patch.object(importer, 'load_listings', return_value={'listings': existing}), \
             patch.object(importer, '_open_prelogged_facebook', return_value=driver), \
             patch.object(importer, 'WebDriverWait'), patch.object(importer, '_click_manage_listings'), \
             patch.object(importer, '_extract_visible_listing_candidates', return_value=candidates), \
             patch.object(importer, 'read_listing_from_manage', side_effect=RuntimeError('Keep original backup')), \
             patch.object(importer.os.path, 'isfile', return_value=True), \
             patch.object(importer, 'save_listings'), patch.object(importer.time, 'sleep'):
            return importer.import_listings_from_facebook('account', only_new=True)

    def inventory_candidate(self, facebook_id):
        return {'href': f'https://www.facebook.com/marketplace/item/{facebook_id}/',
                'title': '2020 Ford Focus', 'text': '$100\n5 clicks on listing'}

    def test_reimport_deduplicates_facebook_id_across_url_variants(self):
        candidate = self.inventory_candidate('123')
        record = dict(importer._listing_from_candidate(candidate), id='saved',
                      url='https://www.facebook.com/marketplace/item/123/?ref=saved',
                      details={'title': 'Saved'}, downloaded_images=[{'path': 'saved.jpg'}])
        duplicate = dict(record, id='duplicate')
        results = self.mock_inventory_import([record, duplicate], [candidate, candidate])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 'saved')
        self.assertTrue(results[0]['facebook_present'])

    def test_cross_post_local_record_is_enriched_without_duplicate(self):
        candidate = self.inventory_candidate('123')
        record = dict(importer._listing_from_candidate(candidate), id='cross-local', url='',
                      details={'title': 'Saved cross post'}, downloaded_images=[{'path': 'saved.jpg'}])
        first = self.mock_inventory_import([record], [candidate])
        second = self.mock_inventory_import(first, [candidate])
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['id'], 'cross-local')
        self.assertIn('/123/', second[0]['url'])

    def test_confirmed_repost_matches_new_facebook_listing_without_duplicate(self):
        candidate = self.inventory_candidate('456')
        record = dict(importer._listing_from_candidate(self.inventory_candidate('123')),
                      id='saved-repost', source_deleted=True, facebook_reposted=True,
                      details={'title': 'Reposted'}, downloaded_images=[{'path': 'saved.jpg'}])
        results = self.mock_inventory_import([record], [candidate])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], 'saved-repost')
        self.assertIn('/456/', results[0]['url'])
        self.assertFalse(results[0]['source_deleted'])

    def test_absent_posted_listing_becomes_backup_and_keeps_details(self):
        present = importer._listing_from_candidate(self.inventory_candidate('123'))
        absent = dict(importer._listing_from_candidate(self.inventory_candidate('456')),
                      details={'title': 'Saved backup'}, downloaded_images=[{'path': 'saved.jpg'}],
                      facebook_reposted=True)
        results = self.mock_inventory_import([present, absent], [self.inventory_candidate('123')])
        saved = next(record for record in results if record['id'] == absent['id'])
        self.assertFalse(saved['facebook_present'])
        self.assertTrue(saved['source_deleted'])
        self.assertFalse(saved['facebook_reposted'])
        self.assertEqual(saved['details'], {'title': 'Saved backup'})
        self.assertEqual(saved['downloaded_images'], [{'path': 'saved.jpg'}])

    def test_empty_inventory_only_marks_missing_when_facebook_confirms_empty(self):
        for empty in (False, True):
            record = importer._listing_from_candidate(self.inventory_candidate('123'))
            result = self.mock_inventory_import([record], [], empty=empty)[0]
            self.assertEqual(result.get('source_deleted', False), empty)

    def test_failed_or_blocked_scan_does_not_mark_missing(self):
        for kwargs in ({'scroll_error': True}, {'url': 'https://www.facebook.com/checkpoint/'}):
            record = importer._listing_from_candidate(self.inventory_candidate('123'))
            with self.assertRaises(RuntimeError):
                self.mock_inventory_import([record], [], **kwargs)
            self.assertFalse(record.get('source_deleted', False))


if __name__ == '__main__':
    unittest.main()
