import importlib
from pathlib import Path
import tempfile
import unittest
import threading
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from flask import Flask
from automation import listing_store


class ListingRouteTests(unittest.TestCase):
    def test_background_toggle_saves_without_redirecting(self):
        with patch.object(self.routes, 'set_run_in_background_enabled') as save:
            response = self.client.post('/set_background_mode', data={},
                headers={'X-Requested-With': 'XMLHttpRequest',
                         'Referer': 'http://localhost/delete_listings/import/stop'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True, 'enabled': False})
        save.assert_called_once_with(False)

    def test_background_toggle_get_recovers_without_changing_setting(self):
        with patch.object(self.routes, 'set_run_in_background_enabled') as save:
            response = self.client.get('/set_background_mode')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/delete_listings'))
        save.assert_not_called()

    def test_listing_import_waits_for_group_import(self):
        with patch.dict(self.routes.groups_jobs, {'test-account': {'running': True}}, clear=True):
            ok, message = self.routes._start_listing_import_job('test-account')
        self.assertFalse(ok)
        self.assertIn('Group import', message)
        self.assertNotIn('test-account', self.routes.listing_import_jobs)

    def test_absent_optional_fields_are_valid_and_hidden(self):
        from automation.reposting import validate_repost_fields
        listing = listing_store.load('test-account')['listings'][0]
        for field in ('exterior_color', 'interior_color', 'vin', 'trim'):
            listing['details'][field] = ''
            listing['details'].setdefault('field_states', {})[field] = 'unreadable'
        validate_repost_fields(listing['details'])
        listing_store.save('test-account', [listing])
        html = self.client.get('/delete_listings/detail/one').data.decode()
        for field in ('exterior_color', 'interior_color', 'vin', 'trim'):
            self.assertNotIn('name="' + field + '"', html)
        self.assertNotIn('<dt>Exterior Color</dt>', html)
        self.assertNotIn('<dt>Interior Color</dt>', html)

    def test_repost_form_requires_core_fields_and_shows_optional_colors(self):
        import re
        html = self.client.get('/delete_listings/detail/one').data.decode()
        for field in ('year', 'make', 'model', 'price', 'body_style',
                      'fuel_type', 'vehicle_condition', 'description'):
            tag = re.search(r'<(?:input|select|textarea)\b[^>]*name="' + field + r'"[^>]*>', html).group()
            self.assertIn('required', tag)
        for field in ('exterior_color', 'interior_color'):
            tag = re.search(r'<select\b[^>]*name="' + field + r'"[^>]*>', html).group()
            self.assertNotIn('required', tag)
        for field in ('vin', 'trim'):
            self.assertNotIn('name="' + field + '"', html)

    def test_repost_payload_keeps_twenty_photos_vin_trim_and_colors(self):
        post = {'title': '2015 Lexus RC 350', 'price': '18500',
                'description': 'Full\nmultiline description', 'vin': 'TESTVIN1234567890',
                'trim': 'F Sport', 'exterior_color': 'White', 'interior_color': 'Black',
                'image_urls': [f'photo-{i}.jpg' for i in range(20)]}
        payload = self.routes._build_repost_payloads({'posts': [post]})[0]
        for field in ('description', 'vin', 'trim', 'exterior_color', 'interior_color'):
            self.assertEqual(payload[field], post[field])
        self.assertEqual(payload['image_path20'], 'photo-19.jpg')

    def test_repost_blocks_missing_core_and_unreadable_colors_before_delete(self):
        for field in ('mileage', 'fuel_type', 'transmission', 'location', 'body_style', 'year', 'exterior_color'):
            with self.subTest(field=field):
                listing = listing_store.load('test-account')['listings'][0]
                if field == 'exterior_color':
                    listing['details']['field_states'] = {'exterior_color': 'unreadable'}
                else:
                    listing['details'][field] = ''
                with patch.object(self.routes, 'post_to_fb_marketplace') as poster, \
                     patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete, \
                     patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
                    self.routes._run_saved_listing_repost(listing, 'test-account')
                delete.assert_not_called()
                poster.assert_not_called()

    def test_item_format_missing_vehicle_fields_is_not_deleted_for_reposting(self):
        listing = listing_store.load('test-account')['listings'][0]
        listing['details']['listing_format'] = 'item'
        listing['details']['mileage'] = ''
        with patch.object(self.routes, 'post_to_fb_marketplace') as poster, \
             patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete, \
             patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
            self.routes._run_saved_listing_repost(listing, 'test-account')
        delete.assert_not_called()
        poster.assert_not_called()
        saved = listing_store.load('test-account')['listings'][0]
        self.assertIn('details are incomplete', saved['repost_error'])

    def test_stopping_import_interrupts_wait_for_account_lock(self):
        lock = threading.Lock()
        lock.acquire()
        stop = threading.Event()
        stop.set()
        try:
            with patch.object(self.routes, '_get_post_lock', return_value=lock):
                with self.assertRaises(self.routes.ListingImportCancelled):
                    with self.routes._listing_import_account_lock('test-account', stop):
                        self.fail('Cancelled import acquired the account lock')
        finally:
            lock.release()

    def test_automatic_import_does_not_restart_user_stopped_job(self):
        self.routes.listing_import_jobs['test-account'] = {'running': False, 'stop_requested': True}
        ok, message = self.routes._start_listing_import_job('test-account', automatic=True)
        self.assertFalse(ok)
        self.assertIn('paused', message)

    def test_stop_import_sets_account_event_and_closes_its_browser(self):
        stop = threading.Event()
        self.routes.listing_import_jobs['test-account'] = {'running': True}
        with patch.dict(self.routes.listing_import_cancel_events, {'test-account': stop}, clear=True), \
             patch.object(self.routes, 'stop_listing_import_driver') as close:
            response = self.client.post('/delete_listings/import/stop',
                headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(stop.is_set())
        self.assertTrue(self.routes.listing_import_jobs['test-account']['stop_requested'])
        close.assert_called_once_with('test-account')

    def test_cancel_pending_repost_is_account_scoped(self):
        self.routes.repost_jobs['cancel-me'] = {'job_id': 'cancel-me', 'account': 'test-account', 'status': 'pending'}
        response = self.client.post('/reposting/cancel', json={'job_id': 'cancel-me'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.routes.repost_jobs['cancel-me']['status'], 'cancelled')
        self.routes.repost_jobs['other'] = {'account': 'other-account', 'status': 'pending'}
        response = self.client.post('/reposting/cancel', json={'job_id': 'other'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.routes.repost_jobs['other']['status'], 'pending')

    def test_listing_cards_use_imported_detail_price(self):
        response = self.client.get('/delete_listings')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<span class="listing-price">100</span>', response.data)
        self.assertIn(b'(item.details || {}).price || item.price', response.data)

    @classmethod
    def setUpClass(cls):
        # Never start background account work while exercising HTTP handlers.
        with patch('apscheduler.schedulers.background.BackgroundScheduler.start'):
            cls.routes = importlib.import_module('web.routes')
        cls.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'web/templates'),
                        static_folder=str(Path(__file__).resolve().parents[1] / 'web/static'))
        cls.app.secret_key = 'test'
        cls.app.register_blueprint(cls.routes.web_bp)
        cls.app.testing = True

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.patches = [patch.object(listing_store, 'data_dir', return_value=self.root),
                        patch.object(self.routes, '_repost_history_path', return_value=self.root / 'repost_history.json'),
                        patch.object(self.routes, 'get_active_account', return_value='test-account'),
                        patch.object(self.routes, 'get_all_accounts', return_value=['test-account']),
                        patch.object(self.routes, '_portal_auth_is_configured', return_value=True)]
        for item in self.patches:
            item.start()
        self.client = self.app.test_client()
        self.routes.repost_jobs.clear()
        self.routes.scheduled_posts.clear()
        self.routes.facebook_delete_jobs.clear()
        self.routes.listing_import_jobs.clear()
        self.routes.listing_click_jobs.clear()
        with self.client.session_transaction() as session:
            session['portal_logged_in'] = True
        photo = self.root / 'photo.jpg'
        photo.write_bytes(b'test-photo')
        listing_store.save('test-account', [{'id': 'one', 'year': '2020', 'make': 'Ford', 'model': 'Focus',
                    'clicks': 5, 'edit_url': 'https://www.facebook.com/marketplace/edit/?listing_id=123', 'details': {
                        'title': '2020 Ford Focus', 'price': '100', 'description': '<original>',
                        'vehicle_type': 'Car', 'year': '2020', 'make': 'Ford', 'model': 'Focus',
                        'mileage': '50000', 'body_style': 'Sedan', 'vehicle_condition': 'Excellent',
                        'exterior_color': 'White', 'interior_color': 'Black',
                        'fuel_type': 'Petrol', 'transmission': 'Automatic', 'location': 'Garland'},
                    'downloaded_images': [{'name': 'first.jpg', 'path': str(photo), 'width': 1000, 'height': 800},
                                          {'name': 'second.jpg', 'path': str(photo), 'width': 1000, 'height': 800}]}])

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def test_cleanup_includes_video_uploads_and_keeps_session_data(self):
        def folder(*parts):
            path = self.root.joinpath(*parts)
            path.mkdir(parents=True, exist_ok=True)
            return path
        video = folder('manual_listing_videos') / 'clip.mp4'
        photo = folder('manual_listing_images') / 'photo.jpg'
        session_file = folder('sessions') / 'saved.pkl'
        for path in [video, photo, session_file]:
            path.write_bytes(b'saved')
        with patch.object(self.routes, 'data_dir', side_effect=folder), patch.object(self.routes, 'screenshots_dir', return_value=str(folder('screenshots'))), patch.object(self.routes, 'project_root', return_value=self.root / 'legacy'), patch.object(self.routes, '_is_auto_listing_running_for_account', return_value=False):
            self.assertEqual(self.client.post('/auto_listing_clear_junk').status_code, 302)
        self.assertFalse(video.exists())
        self.assertFalse(photo.exists())
        self.assertTrue(session_file.exists())

    def test_cleanup_waits_for_posts_on_another_account(self):
        self.routes.scheduled_posts['other-job'] = {'account': 'other', 'status': 'pending'}
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other']), patch.object(self.routes.os, 'remove') as remove:
            self.assertEqual(self.client.post('/auto_listing_clear_junk').status_code, 302)
            remove.assert_not_called()

    def test_detail_renders_and_preferences_leave_source_untouched(self):
        response = self.client.get('/delete_listings/detail/one')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'&lt;original&gt;', response.data)
        response = self.client.post('/delete_listings/detail/one', json={
            'fields': {'price': '250'}, 'image_order': ['second.jpg', 'first.jpg']})
        self.assertEqual(response.status_code, 200)
        listing = listing_store.load('test-account')['listings'][0]
        self.assertEqual(listing['details']['price'], '100')
        self.assertEqual(listing['repost_preferences']['fields']['price'], '250')
        self.assertEqual(listing['repost_preferences']['image_order'][0], 'second.jpg')

    def test_repost_updates_date_clicks_and_edited_data_only_after_success(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [{'id': 'other', 'year': '2019', 'make': 'Honda', 'model': 'Civic'}, original])
        for success in (False, True):
            with self.subTest(success=success):
                listing_store.update_fields('test-account', 'one', {'listed_date': '2020-01-01', 'clicks': 5})
                listing = next(item for item in listing_store.load('test-account')['listings'] if item['id'] == 'one')
                before = dict(listing['details'])
                listing['repost_preferences'] = {'fields': {
                    'year': '2026', 'make': 'Toyota', 'model': 'Corolla',
                    'price': '250', 'description': 'Edited description'},
                    'image_order': ['second.jpg', 'first.jpg']}

                def publish(payload, account):
                    self.assertTrue(payload['_require_publish_confirmation'])
                    current = next(item for item in listing_store.load(account)['listings'] if item['id'] == 'one')
                    self.assertEqual(current['listed_date'], '2020-01-01')
                    self.assertEqual(current['clicks'], 5)
                    self.assertEqual(current['details'], before)
                    return success

                with patch.object(self.routes, 'post_to_fb_marketplace', side_effect=publish), \
                     patch.object(self.routes, 'delete_selected_listings_from_facebook', return_value={'deleted': 1}), \
                     patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
                    self.routes._run_saved_listing_repost(listing, 'test-account')
                records = listing_store.load('test-account')['listings']
                saved = next(item for item in records if item['id'] == 'one')
                self.assertEqual([item['id'] for item in records], ['one', 'other'] if success else ['other', 'one'])
                if success:
                    expected_date = datetime.fromisoformat(saved['facebook_reposted_at']).astimezone(timezone(timedelta(hours=5))).date().isoformat()
                    self.assertEqual(saved['listed_date'], expected_date)
                    self.assertEqual(saved['clicks'], 0)
                    self.assertTrue(saved['clicks_confirmed'])
                    self.assertEqual(saved['details']['title'], '2026 Toyota Corolla')
                    self.assertEqual(saved['details']['description'], 'Edited description')
                    self.assertEqual(saved['price'], '250')
                    self.assertEqual(saved['model'], 'Corolla')
                    self.assertEqual(saved['downloaded_images'][0]['name'], 'second.jpg')
                else:
                    self.assertEqual(saved['listed_date'], '2020-01-01')
                    self.assertEqual(saved['clicks'], 5)
                    self.assertEqual(saved['details'], before)

    def test_photo_access_is_account_scoped_and_invalid_order_rejected(self):
        response = self.client.get('/delete_listings/photo/one/first.jpg')
        self.assertEqual(response.data, b'test-photo')
        response.close()
        with patch.object(self.routes, 'get_active_account', return_value='other-account'):
            self.assertEqual(self.client.get('/delete_listings/photo/one/first.jpg').status_code, 404)
        response = self.client.post('/delete_listings/detail/one', json={
            'fields': {}, 'image_order': ['first.jpg', 'first.jpg']})
        self.assertEqual(response.status_code, 400)

    def test_preferences_locked_during_import(self):
        with patch.object(self.routes, '_delete_import_snapshot', return_value={'running': True}):
            response = self.client.post('/delete_listings/detail/one', json={
                'fields': {}, 'image_order': ['first.jpg', 'second.jpg']})
        self.assertEqual(response.status_code, 409)

    def test_repost_uses_saved_fields_and_order_without_rereading_facebook(self):
        listing = listing_store.load('test-account')['listings'][0]
        second = self.root / 'second.jpg'
        second.write_bytes(b'second-photo')
        listing['downloaded_images'][1]['path'] = str(second)
        listing['repost_preferences'] = {'fields': {'title': 'Updated title', 'price': '250'},
                                         'image_order': ['second.jpg', 'first.jpg']}
        calls = []
        self.routes.repost_jobs['test-repost'] = {'status': 'pending'}

        def delete_source(*args):
            calls.append('delete')
            self.assertEqual(self.routes.repost_jobs['test-repost']['status'], 'deleting')
            return {'deleted': 1}

        def publish(*args):
            calls.append('post')
            saved = listing_store.load('test-account')['listings'][0]
            self.assertTrue(saved['source_deleted'])
            self.assertTrue(saved['facebook_deleted_at'])
            self.assertEqual(saved['repost_status'], 'posting')
            self.assertEqual(self.routes.repost_jobs['test-repost']['status'], 'posting')
            return True

        with patch('automation.delete_listing_import.read_listing_from_manage') as reader, \
             patch.object(self.routes, 'post_to_fb_marketplace', side_effect=publish) as poster, \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', side_effect=delete_source) as delete, \
             patch.object(self.routes, '_record_repost_history'), \
             patch.object(self.routes, '_runtime_log'):
            self.routes._run_saved_listing_repost(listing, 'test-account', job_id='test-repost')
        reader.assert_not_called()
        self.assertEqual(calls, ['delete', 'post'])
        payload = poster.call_args.args[0]
        self.assertEqual(payload['price'], '250')
        self.assertEqual(payload['title'], 'Updated title')
        self.assertEqual(payload['image_path1'], str(second))
        self.assertEqual(delete.call_args.args[1][0]['model'], 'Focus')
        self.assertEqual(delete.call_args.args[1][0]['_source_listing_id'], '123')
        saved = listing_store.load('test-account')['listings'][0]
        self.assertTrue(saved['source_deleted'])
        self.assertEqual(saved['details']['price'], '250')
        self.assertEqual(len(saved['downloaded_images']), 2)

    def test_schedule_freezes_preferences_and_shows_main_list_status(self):
        with patch.object(self.routes.scheduler, 'add_job') as schedule, patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/repost', json={
                'listing_id': 'one', 'post_mode': 'schedule', 'delay_value': 2, 'delay_unit': 'hours',
                'fields': {'price': '250', 'clean_title': 'No'}, 'image_order': ['second.jpg', 'first.jpg']})
        self.assertEqual(response.status_code, 200)
        listing = schedule.call_args.kwargs['args'][0]
        self.assertEqual(listing['repost_preferences']['fields']['price'], '250')
        self.assertEqual(listing['repost_preferences']['image_order'][0], 'second.jpg')
        self.assertEqual(self.client.get('/delete_listings/data').json['listings'][0]['display_status'], 'pending')
        response = self.client.get('/delete_listings')
        self.assertIn(b'listing-view-btn', response.data)
        self.assertIn(b'Posting Results', response.data)
        self.assertNotIn(b'listing-repost-btn', response.data)

    def test_immediate_repost_is_queued_without_publishing_in_request(self):
        with patch.object(self.routes.scheduler, 'add_job') as schedule, \
             patch.object(self.routes, 'post_to_fb_marketplace') as poster, patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/repost', json={
                'listing_id': 'one', 'post_mode': 'now', 'fields': {}, 'image_order': ['first.jpg', 'second.jpg']})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(schedule.called)
        poster.assert_not_called()

    def test_incomplete_photo_import_does_not_schedule(self):
        listing_store.update_fields('test-account', 'one', {'image_errors': ['download failed']})
        with patch.object(self.routes.scheduler, 'add_job') as schedule:
            response = self.client.post('/delete_listings/repost', json={
                'listing_id': 'one', 'post_mode': 'now', 'fields': {}, 'image_order': ['first.jpg', 'second.jpg']})
        self.assertEqual(response.status_code, 400)
        schedule.assert_not_called()

    def _delete_synchronously(self, target, result=None):
        def thread(*args, **kwargs):
            from unittest.mock import MagicMock
            instance = MagicMock()
            instance.start.side_effect = kwargs['target']
            return instance
        with patch.object(self.routes.threading, 'Thread', side_effect=thread), \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', return_value=result or
                          {'deleted': 1, 'deleted_ids': ['one'], 'clicked': 1}) as delete, \
             patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/delete', json={'ids': ['one'], 'target': target})
        return response, delete

    def test_facebook_delete_keeps_saved_backup(self):
        before = listing_store.load('test-account')['listings'][0]
        response, delete = self._delete_synchronously('facebook')
        self.assertEqual(response.status_code, 200)
        saved = listing_store.load('test-account')['listings'][0]
        self.assertTrue(saved['source_deleted'])
        self.assertEqual(saved['details'], before['details'])
        self.assertEqual(saved['downloaded_images'], before['downloaded_images'])
        self.assertEqual(delete.call_args.args[1][0]['_source_listing_id'], '123')
        self.assertEqual(self.client.get('/delete_listings/data').json['listings'][0]['display_status'], 'backup')

    def test_database_delete_leaves_facebook_untouched(self):
        response, delete = self._delete_synchronously('database')
        self.assertTrue(response.json['completed'])
        self.assertEqual(listing_store.load('test-account')['listings'], [])
        delete.assert_not_called()

    def test_delete_both_removes_backup_after_success(self):
        response, delete = self._delete_synchronously('both')
        self.assertEqual(response.status_code, 200)
        delete.assert_called_once()
        self.assertEqual(listing_store.load('test-account')['listings'], [])

    def test_delete_both_failure_keeps_backup(self):
        self._delete_synchronously('both', {'deleted': 0, 'deleted_ids': [], 'not_found': 1})
        saved = listing_store.load('test-account')['listings'][0]
        self.assertFalse(saved.get('source_deleted', False))
        self.assertTrue(self.routes._facebook_delete_snapshot('test-account')['error'])

    def test_delete_both_archived_listing_does_not_contact_facebook(self):
        listing_store.update_fields('test-account', 'one', {'source_deleted': True})
        response, delete = self._delete_synchronously('both')
        self.assertEqual(response.status_code, 200)
        delete.assert_not_called()
        self.assertEqual(listing_store.load('test-account')['listings'], [])

    def test_delete_is_blocked_during_refresh_or_scheduled_repost(self):
        for status in ('import', 'schedule'):
            self.routes.listing_import_jobs.clear()
            self.routes.repost_jobs.clear()
            if status == 'import':
                self.routes.listing_import_jobs['test-account'] = {'running': True}
            else:
                self.routes.repost_jobs['job'] = {'account': 'test-account', 'listing_id': 'one', 'status': 'pending'}
            response = self.client.post('/delete_listings/delete', json={'ids': ['one'], 'target': 'database'})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(len(listing_store.load('test-account')['listings']), 1)

    def test_delete_invalid_target_or_wrong_account_is_rejected(self):
        for body, expected in [({'ids': ['one'], 'target': 'invalid'}, 400),
                               ({'ids': ['missing'], 'target': 'database'}, 404),
                               ({'ids': ['one'], 'target': 'database', 'account': 'another'}, 409)]:
            self.assertEqual(self.client.post('/delete_listings/delete', json=body).status_code, expected)

    def test_archived_backup_can_be_reposted_without_deleting_again(self):
        listing_store.update_fields('test-account', 'one', {'source_deleted': True})
        with patch.object(self.routes.scheduler, 'add_job'), patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/repost', json={'listing_id': 'one', 'post_mode': 'now'})
        self.assertEqual(response.status_code, 200)
        listing = listing_store.load('test-account')['listings'][0]
        with patch.object(self.routes, 'post_to_fb_marketplace', return_value=True) as poster, \
             patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete, \
             patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
            self.routes._run_saved_listing_repost(listing, 'test-account')
        poster.assert_called_once()
        delete.assert_not_called()
        self.assertEqual(len(listing_store.load('test-account')['listings']), 1)
        saved = listing_store.load('test-account')['listings'][0]
        self.assertTrue(saved['facebook_reposted'])
        self.assertTrue(saved['facebook_reposted_at'])
        self.assertTrue(saved['source_deleted'])
        response = self.client.get('/delete_listings/detail/one')
        self.assertIn(b'Posted on Facebook', response.data)
        self.assertNotIn(b'Removed from Facebook', response.data)

    def test_relogin_keeps_saved_listing_data(self):
        before = listing_store.load('test-account')['listings']
        with patch.object(self.routes, 'start_cdp_login') as login:
            response = self.client.post('/account/relogin', data={'account': 'test-account'})
        self.assertEqual(response.status_code, 302)
        login.assert_called_once_with('test-account', relogin=True)
        self.assertEqual(listing_store.load('test-account')['listings'], before)

    def test_relogin_rejects_unknown_account(self):
        with patch.object(self.routes, 'start_cdp_login') as login:
            response = self.client.post('/account/relogin', data={'account': 'unknown'})
        self.assertEqual(response.status_code, 404)
        login.assert_not_called()

    def test_failed_post_keeps_backup_and_retry_skips_deleted_source(self):
        listing = listing_store.load('test-account')['listings'][0]
        with patch.object(self.routes, 'post_to_fb_marketplace', return_value=False) as poster, \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', return_value={'deleted': 1}) as delete, \
             patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
            self.routes._run_saved_listing_repost(listing, 'test-account')
        delete.assert_called_once()
        poster.assert_called_once()
        saved = listing_store.load('test-account')['listings'][0]
        self.assertTrue(saved['source_deleted'])
        self.assertEqual(saved['repost_status'], 'failed')
        self.assertFalse(saved.get('facebook_reposted', False))
        self.assertIn('saved backup was kept', saved['repost_error'])
        self.assertEqual(saved['details'], listing['details'])
        self.assertEqual(saved['downloaded_images'], listing['downloaded_images'])
        with patch.object(self.routes, 'post_to_fb_marketplace', return_value=True) as poster, \
             patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete, \
             patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
            self.routes._run_saved_listing_repost(saved, 'test-account')
        delete.assert_not_called()
        poster.assert_called_once()
        self.assertEqual(listing_store.load('test-account')['listings'][0]['repost_status'], 'done')
        self.assertEqual(len(listing_store.load('test-account')['listings']), 1)

    def test_delete_failure_stops_reposting_and_keeps_backup(self):
        for result in ({'deleted': 0}, RuntimeError('Delete browser failed')):
            with self.subTest(result=result):
                listing = listing_store.load('test-account')['listings'][0]
                kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
                with patch.object(self.routes, 'post_to_fb_marketplace') as poster, \
                     patch.object(self.routes, 'delete_selected_listings_from_facebook', **kwargs) as delete, \
                     patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
                    self.routes._run_saved_listing_repost(listing, 'test-account')
                delete.assert_called_once()
                poster.assert_not_called()
                saved = listing_store.load('test-account')['listings'][0]
                self.assertFalse(saved.get('source_deleted', False))
                self.assertEqual(saved['repost_status'], 'failed')
                self.assertEqual(saved['details'], listing['details'])

    def test_saved_data_is_validated_before_deletion(self):
        for invalid in ('missing-photo', 'missing-price', 'missing-id'):
            with self.subTest(invalid=invalid):
                listing = listing_store.load('test-account')['listings'][0]
                if invalid == 'missing-photo':
                    listing['downloaded_images'][0]['path'] = str(self.root / 'missing.jpg')
                elif invalid == 'missing-price':
                    listing['details']['price'] = ''
                else:
                    listing['edit_url'] = ''
                with patch.object(self.routes, 'post_to_fb_marketplace') as poster, \
                     patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete, \
                     patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
                    self.routes._run_saved_listing_repost(listing, 'test-account')
                delete.assert_not_called()
                poster.assert_not_called()
                saved = listing_store.load('test-account')['listings'][0]
                self.assertFalse(saved.get('source_deleted', False))
                self.assertEqual(saved['repost_status'], 'failed')

    def test_post_exception_after_deletion_retains_backup_and_removed_status(self):
        listing = listing_store.load('test-account')['listings'][0]
        with patch.object(self.routes, 'post_to_fb_marketplace', side_effect=RuntimeError('Browser failed')), \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', return_value={'deleted': 1}), \
             patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
            self.routes._run_saved_listing_repost(listing, 'test-account')
        saved = listing_store.load('test-account')['listings'][0]
        self.assertTrue(saved['source_deleted'])
        self.assertEqual(saved['repost_status'], 'failed')
        self.assertIn('original Facebook listing has been removed', saved['repost_error'])
        self.assertEqual(saved['downloaded_images'], listing['downloaded_images'])

    def test_missing_details_never_falls_back_to_facebook_read(self):
        listing = listing_store.load('test-account')['listings'][0]
        listing['details'] = {}
        with patch('automation.delete_listing_import.read_listing_from_manage') as reader, \
             patch.object(self.routes, 'post_to_fb_marketplace') as poster, \
             patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
            self.routes._run_saved_listing_repost(listing, 'test-account')
        reader.assert_not_called()
        poster.assert_not_called()

    def test_daily_refresh_uses_import_time_and_runs_only_one_due_account(self):
        now = datetime.now(timezone.utc)
        for hours, expected in ((23, False), (24, True)):
            with patch.object(self.routes, 'load_listings', return_value={'last_import_at': (now-timedelta(hours=hours)).isoformat(), 'updated_at': now.isoformat()}):
                self.assertEqual(self.routes._listing_snapshot_is_due('test-account', now), expected)
        with patch.object(self.routes, 'get_all_accounts', return_value=['a', 'b']), \
             patch.object(self.routes, '_listing_snapshot_is_due', return_value=True), \
             patch.object(self.routes, '_start_listing_import_job', return_value=(True, 'Started')) as start, \
             patch.object(self.routes, '_runtime_log'):
            self.routes._refresh_due_listing_snapshots()
        start.assert_called_once_with('a', automatic=True)

    def test_automatic_import_merges_and_marks_its_own_clock(self):
        def thread(*args, **kwargs):
            from unittest.mock import MagicMock
            instance = MagicMock()
            instance.start.side_effect = kwargs['target']
            return instance
        with patch.object(self.routes.threading, 'Thread', side_effect=thread), \
             patch.object(self.routes, 'import_listings_from_facebook') as importer:
            ok, _ = self.routes._start_listing_import_job('test-account', automatic=True)
        self.assertTrue(ok)
        self.assertFalse(importer.call_args.kwargs['replace_existing'])
        self.assertTrue(importer.call_args.kwargs['only_new'])
        self.assertFalse(self.routes._listing_snapshot_is_due('test-account'))

    def test_repost_status_page_shows_schedules_and_history_for_active_account(self):
        now = datetime.now()
        for job_id, account, state in [('scheduled', 'test-account', 'pending'), ('running', 'test-account', 'posting'),
                                        ('deleting', 'test-account', 'deleting'), ('other', 'other-account', 'pending')]:
            self.routes.repost_jobs[job_id] = {'job_id': job_id, 'listing_id': 'one', 'account': account,
                'title': job_id + ' vehicle', 'status': state, 'run_at': now + timedelta(hours=2),
                'delay_value': 2, 'delay_unit': 'hours'}
        listing = listing_store.load('test-account')['listings'][0]
        self.routes._record_repost_history('test-account', listing, 'failed', 'Body style: no matching option')
        self.routes._record_repost_history('test-account', listing, 'reposted')
        self.routes._record_repost_history('other-account', listing, 'failed', 'Private other account error')
        response = self.client.get('/reposting')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Reposting Results', response.data)
        self.assertIn(b'Posting Results', response.data)
        self.assertIn(b'Reposting', response.data)
        self.assertIn(b'Deleting original listing', response.data)
        self.assertIn(b'Body style: no matching option', response.data)
        self.assertIn(b'reposted', response.data)
        self.assertNotIn(b'Private other account error', response.data)
        self.assertNotIn(b'other vehicle', response.data)
        data = self.client.get('/reposting_data').json
        self.assertEqual(data['pending_count'], 1)
        self.assertEqual(data['running_count'], 2)
        self.assertEqual(len(data['history']), 2)
        self.client.post('/reposting_history/clear')
        self.assertEqual(self.client.get('/reposting_data').json['history'], [])
        self.assertEqual(len(listing_store.load('test-account')['listings']), 1)

    def test_diagnostic_report_download_requires_login_and_is_attachment(self):
        with patch('automation.terminal_log.export_text', return_value='[CHECK] IMPORT FINISHED'):
            response = self.client.get('/settings/diagnostics/download')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn(b'IMPORT FINISHED', response.data)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/settings/diagnostics/download').status_code, 302)

    def add_saved_video(self):
        video = self.root / 'clip.mp4'
        video.write_bytes(b'original-video')
        listing = listing_store.load('test-account')['listings'][0]
        listing['downloaded_videos'] = [{'name': 'clip.mp4', 'path': str(video), 'mime_type': 'video/mp4'}]
        listing_store.save('test-account', [listing])
        return listing, video

    def test_saved_video_player_and_account_scoped_range_delivery(self):
        listing, video = self.add_saved_video()
        page = self.client.get('/delete_listings/detail/one')
        self.assertIn(b'<video class="ld-saved-video"', page.data)
        response = self.client.get('/delete_listings/video/one/clip.mp4', headers={'Range': 'bytes=0-7'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'original')
        response.close()
        with patch.object(self.routes, 'get_active_account', return_value='other'):
            self.assertEqual(self.client.get('/delete_listings/video/one/clip.mp4').status_code, 404)

    def test_repost_passes_saved_video_and_skips_missing_optional_file(self):
        listing, video = self.add_saved_video()
        for exists in (True, False):
            if not exists:
                video.unlink()
            with patch.object(self.routes, 'post_to_fb_marketplace', return_value=True) as poster, \
                 patch.object(self.routes, 'delete_selected_listings_from_facebook', return_value={'deleted': 1}), \
                 patch.object(self.routes, '_record_repost_history'), patch.object(self.routes, '_runtime_log'):
                self.routes._run_saved_listing_repost(listing, 'test-account')
            poster.assert_called_once()
            self.assertEqual(poster.call_args.args[0]['video_paths'], [str(video)] if exists else [])

    def test_cross_post_copies_video_backup_and_passes_upload_path(self):
        listing, video = self.add_saved_video()
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other']), \
             patch.object(self.routes.scheduler, 'add_job'):
            response = self.cross_request(['other'])
            self.assertEqual(response.status_code, 200)
            ids = response.json['job_ids']
            with patch.object(self.routes, 'post_to_fb_marketplace', return_value=True) as poster:
                self.routes._run_cross_post_batch(ids)
            self.assertEqual(poster.call_args.args[0]['video_paths'], [str(video)])
        target = listing_store.load('other')['listings'][0]
        copied = target['downloaded_videos'][0]['path']
        self.assertNotEqual(copied, str(video))
        self.assertEqual(Path(copied).read_bytes(), b'original-video')
        self.assertEqual(target['details']['video_paths'], [copied])

    def cross_request(self, targets=None, **extra):
        payload = {'account': 'test-account', 'listing_id': 'one',
                   'target_accounts': targets if targets is not None else ['other', 'third'],
                   'fields': {'title': 'Cross title', 'price': '250', 'description': 'Cross description'},
                   'image_order': ['second.jpg', 'first.jpg'], 'post_mode': 'now'}
        payload.update(extra)
        return self.client.post('/delete_listings/cross_post', json=payload)

    def test_cross_post_selection_validation_and_account_scope(self):
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other', 'third']), \
             patch.object(self.routes.scheduler, 'add_job') as schedule:
            for targets in ([], ['test-account'], ['unknown'], ['other', 'other'], ['other'] * 20):
                self.assertEqual(self.cross_request(targets).status_code, 400)
            self.assertEqual(self.cross_request(account='third').status_code, 409)
            self.assertEqual(self.cross_request(listing_id='missing').status_code, 404)
            schedule.assert_not_called()

    def test_cross_post_serial_failure_continues_and_source_stays_unchanged(self):
        original = listing_store.load('test-account')['listings'][0]
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other', 'third']), \
             patch.object(self.routes.scheduler, 'add_job') as schedule:
            response = self.cross_request()
            self.assertEqual(response.status_code, 200)
            ids = response.json['job_ids']
            self.assertEqual(schedule.call_args.kwargs['args'], [ids])
            calls = []
            def publish(payload, account):
                calls.append(account)
                self.assertFalse(payload['_require_publish_confirmation'])
                self.assertEqual(payload['price'], '250')
                self.assertEqual(payload['description'], 'Cross description')
                if account == 'other':
                    self.assertEqual(self.routes.scheduled_posts[ids[1]]['status'], 'pending')
                    raise RuntimeError('First account failed')
                self.assertEqual(self.routes.scheduled_posts[ids[0]]['status'], 'failed')
                return True
            with patch.object(self.routes, 'post_to_fb_marketplace', side_effect=publish), \
                 patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete, \
                 patch.object(self.routes, '_runtime_log'):
                self.routes._run_cross_post_batch(ids)
            self.assertEqual(calls, ['other', 'third'])
            delete.assert_not_called()
            self.assertEqual(self.routes.scheduled_posts[ids[1]]['status'], 'posted')
            self.assertEqual(listing_store.load('other')['listings'], [])
            target_listing = listing_store.load('third')['listings'][0]
            self.assertEqual(target_listing['id'], self.routes.scheduled_posts[ids[1]]['target_listing_id'])
            self.assertEqual(target_listing['details']['title'], 'Cross title')
            self.assertEqual(target_listing['price'], '250')
            self.assertEqual(target_listing['clicks'], 0)
            self.assertEqual(target_listing['listed_date'], datetime.fromisoformat(target_listing['cross_posted_at']).astimezone(timezone(timedelta(hours=5))).date().isoformat())
            self.assertEqual(target_listing['downloaded_images'][0]['name'], 'second.jpg')
            self.assertNotEqual(target_listing['downloaded_images'][0]['path'], original['downloaded_images'][0]['path'])
            self.assertEqual(Path(target_listing['downloaded_images'][0]['path']).read_bytes(), b'test-photo')
            self.assertEqual(listing_store.load('test-account')['listings'][0], original)
            data = self.client.get('/post_schedular_data').json
            self.assertEqual(len(data['items']), 2)
            self.assertEqual(data['posted_count'], 1)
            self.assertIn('First account failed', data['items'][0]['error'])
            with patch.object(self.routes, 'get_active_account', return_value='third'):
                target = self.client.get('/post_schedular_data').json['items']
                self.assertEqual(len(target), 1)
                self.assertEqual(target[0]['source_account'], 'test-account')
            with patch.object(self.routes, 'get_active_account', return_value='unrelated'):
                self.assertEqual(self.client.get('/post_schedular_data').json['items'], [])

    def test_cross_post_delay_and_pending_cancellation(self):
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other', 'third']), \
             patch.object(self.routes.scheduler, 'add_job') as schedule:
            self.assertEqual(self.cross_request(post_mode='schedule', delay_value=0, delay_unit='minutes').status_code, 400)
            response = self.cross_request(post_mode='schedule', delay_value=2, delay_unit='hours')
            self.assertEqual(response.status_code, 200)
            self.assertGreater((schedule.call_args.kwargs['run_date'] - datetime.now()).total_seconds(), 7190)
            ids = response.json['job_ids']
            self.assertEqual(self.client.post('/post_schedular_delete', json={'job_id': ids[0]}).status_code, 200)
            with patch.object(self.routes, 'post_to_fb_marketplace', return_value=True) as poster, \
                 patch.object(self.routes, '_runtime_log'):
                self.routes._run_cross_post_batch(ids)
            poster.assert_called_once()
            self.assertEqual(poster.call_args.args[1], 'third')

    def test_cross_post_selection_excludes_source_and_supports_reverse_direction(self):
        accounts = ['test-account', 'other', 'third']
        with patch.object(self.routes, 'get_all_accounts', return_value=accounts):
            html = self.client.get('/delete_listings/detail/one').data
            self.assertIn(b'Post on Other accounts', html)
            self.assertIn(b'data-cross-account value="other"', html)
            self.assertNotIn(b'data-cross-account value="test-account"', html)
            listing_store.save('other', listing_store.load('test-account')['listings'])
            with patch.object(self.routes, 'get_active_account', return_value='other'), \
                 patch.object(self.routes.scheduler, 'add_job'):
                self.assertEqual(self.cross_request(['test-account'], account='other').status_code, 200)

    def test_two_cross_post_batches_never_overlap(self):
        first_started, second_started, release = threading.Event(), threading.Event(), threading.Event()
        accounts = ['test-account', 'other', 'third']
        with patch.object(self.routes, 'get_all_accounts', return_value=accounts), \
             patch.object(self.routes.scheduler, 'add_job'), patch.object(self.routes, '_runtime_log'):
            first_ids = self.cross_request(['other']).json['job_ids']
            original = listing_store.load('test-account')['listings'][0]
            other = dict(original, id='two')
            listing_store.save('test-account', [original, other])
            second_ids = self.cross_request(['third'], listing_id='two').json['job_ids']
            def publish(payload, account):
                if account == 'other':
                    first_started.set()
                    if not release.wait(3):
                        raise RuntimeError('Test timed out waiting for release')
                else:
                    second_started.set()
                return True
            with patch.object(self.routes, 'post_to_fb_marketplace', side_effect=publish):
                first = threading.Thread(target=self.routes._run_cross_post_batch, args=(first_ids,))
                second = threading.Thread(target=self.routes._run_cross_post_batch, args=(second_ids,))
                first.start()
                try:
                    self.assertTrue(first_started.wait(2))
                    second.start()
                    self.assertFalse(second_started.wait(0.2))
                    self.assertEqual(self.routes.scheduled_posts[second_ids[0]]['status'], 'pending')
                finally:
                    release.set()
                    first.join(3)
                    if second.ident is not None:
                        second.join(3)
                self.assertTrue(second_started.is_set())
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())

    def test_cross_post_scheduler_registration_failure_leaves_no_jobs(self):
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other']), \
             patch.object(self.routes.scheduler, 'add_job', side_effect=RuntimeError('Queue unavailable')):
            self.assertEqual(self.cross_request(['other']).status_code, 500)
        self.assertEqual(self.routes.scheduled_posts, {})

    def test_confirmed_cross_post_appears_at_top_of_target_portal(self):
        listing_store.save('other', [{'id': 'existing', 'details': {'title': 'Existing'}}])
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other']), \
             patch.object(self.routes.scheduler, 'add_job'), \
             patch.object(self.routes, 'post_to_fb_marketplace', return_value=True), \
             patch.object(self.routes, '_runtime_log'):
            response = self.cross_request(['other'])
            self.routes._run_cross_post_batch(response.json['job_ids'])
            with patch.object(self.routes, 'get_active_account', return_value='other'):
                records = self.client.get('/delete_listings/data').json['listings']
                self.assertEqual(len(records), 2)
                self.assertEqual(records[0]['details']['title'], 'Cross title')
                self.assertEqual(records[1]['id'], 'existing')
                detail = self.client.get('/delete_listings/detail/' + records[0]['id'])
                self.assertEqual(detail.status_code, 200)
                self.assertIn(b'Cross title', detail.data)
                self.assertEqual(records[0]['edit_url'], '')

    def test_missing_facebook_listing_shows_backup_only(self):
        import re
        listing_store.update_fields('test-account', 'one', {
            'facebook_present': False, 'source_deleted': True, 'facebook_reposted': False})
        for url in ('/delete_listings', '/delete_listings/detail/one'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            badge = re.search(r'<span\b[^>]*class="listing-source-badge[^>]*>', response.get_data(as_text=True))
            self.assertIsNotNone(badge)
            self.assertIn('hidden', badge.group(0))
            self.assertIn(b'class="listing-backup-badge">Saved as backup', response.data)

    def test_repost_status_page_does_not_start_old_read_first_workflow(self):
        response = self.client.post('/reposting')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/delete_listings'))


if __name__ == '__main__':
    unittest.main()
