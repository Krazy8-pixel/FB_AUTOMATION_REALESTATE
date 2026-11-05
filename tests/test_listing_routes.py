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
    def test_account_browser_is_desktop_sidebar_page(self):
        with patch.dict('os.environ', {'FB_AUTOMATION_DESKTOP': '1'}), \
             patch.object(self.routes.account_browser, 'status', return_value={'account': None, 'open': False}):
            response = self.client.get('/account/browser')
            dashboard = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('href="/account/browser">Account Browser</a>', html)
        self.assertIn('id="browser-surface"', html)
        self.assertIn('class="account-browser-header"', html)
        self.assertIn('id="browser-menu-toggle"', html)
        self.assertNotIn('account-browser-heading', html)
        self.assertNotIn('class="account-browser-header"', dashboard.get_data(as_text=True))
        self.assertIn('class="header-account-box"', dashboard.get_data(as_text=True))

    def test_clear_posting_results_keeps_active_jobs_and_saved_listings(self):
        for job_id, state, account in [('posted', 'posted', 'test-account'), ('failed', 'failed', 'test-account'),
                                       ('cancelled', 'cancelled', 'test-account'), ('pending', 'pending', 'test-account'),
                                       ('waiting', 'waiting', 'test-account'), ('running', 'posting', 'test-account'),
                                       ('other', 'posted', 'other-account')]:
            self.routes.scheduled_posts[job_id] = {'status': state, 'account': account}
        saved = listing_store.load('test-account')
        with patch.object(self.routes.scheduler, 'remove_job') as cancel, \
             patch.object(self.routes, 'post_to_fb_marketplace') as post, \
             patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete:
            response = self.client.post('/post_schedular/clear_results', json={'account': 'test-account'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cleared_count'], 3)
        self.assertEqual(set(self.routes.scheduled_posts), {'pending', 'waiting', 'running', 'other'})
        self.assertEqual(listing_store.load('test-account'), saved)
        cancel.assert_not_called()
        post.assert_not_called()
        delete.assert_not_called()

    def test_cleared_cross_post_results_are_account_scoped_and_survive_restart(self):
        from automation import desktop_state
        now = datetime.now()
        self.routes.scheduled_posts['cross'] = {
            'job_id': 'cross', 'status': 'posted', 'source': 'Cross posting',
            'account': 'target-account', 'source_account': 'test-account',
            'title': 'Cross-post result', 'run_at': now, 'posted_at': now,
        }
        with patch.object(self.routes, 'DESKTOP_MODE', True), \
             patch.dict('os.environ', FB_AUTOMATION_DESKTOP='1'), \
             patch.object(desktop_state, 'data_dir', return_value=self.root), \
             patch.object(desktop_state, '_last_saved', None), \
             patch.object(self.routes.scheduler, 'get_jobs', return_value=[]):
            response = self.client.post('/post_schedular/clear_results', json={'account': 'test-account'})
            self.assertEqual(response.json['cleared_count'], 1)
            self.routes.scheduled_posts.clear()
            desktop_state.restore_state(self.routes)
            self.assertEqual(self.client.get('/post_schedular_data').json['items'], [])
            self.assertEqual(self.client.get('/post_schedular_data').json['posted_count'], 0)
            with patch.object(self.routes, 'get_active_account', return_value='target-account'):
                self.assertEqual(len(self.client.get('/post_schedular_data').json['items']), 1)
                self.assertEqual(self.client.get('/post_schedular_data').json['posted_count'], 1)
                response = self.client.post('/post_schedular/clear_results', json={'account': 'target-account'})
                self.assertEqual(response.json['cleared_count'], 1)
            self.assertNotIn('cross', self.routes.scheduled_posts)

    def test_clear_posting_results_rejects_changed_account(self):
        self.routes.scheduled_posts['posted'] = {'account': 'test-account', 'status': 'posted'}
        response = self.client.post('/post_schedular/clear_results', json={'account': 'another-account'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('posted', self.routes.scheduled_posts)

    def test_posting_results_show_latest_completion_first_with_clear_times(self):
        now = datetime.now().replace(microsecond=0)
        self.routes.scheduled_posts.update({
            'older': {'job_id': 'older', 'account': 'test-account', 'title': 'Older result',
                      'status': 'posted', 'run_at': now - timedelta(hours=1),
                      'posted_at': now - timedelta(minutes=2)},
            'latest': {'job_id': 'latest', 'account': 'test-account', 'title': 'Latest result',
                       'source': 'Cross posting', 'source_account': 'other',
                       'status': 'posted', 'run_at': now - timedelta(hours=2),
                       'posted_at': now - timedelta(minutes=1)},
            'future': {'job_id': 'future', 'account': 'test-account', 'title': 'Future post',
                       'status': 'pending', 'run_at': now + timedelta(days=5),
                       'created_at': now - timedelta(days=1)},
            'sooner': {'job_id': 'sooner', 'account': 'test-account', 'title': 'Sooner post',
                       'status': 'pending', 'run_at': now + timedelta(days=1),
                       'created_at': now - timedelta(days=2)},
        })
        with patch.object(self.routes.scheduler, 'get_jobs', return_value=[]):
            data = self.client.get('/post_schedular_data').json
            html = self.client.get('/post_schedular').get_data(as_text=True)
        self.assertEqual([item['job_id'] for item in data['items']], ['latest', 'older', 'future', 'sooner'])
        latest = data['items'][0]
        local = (now - timedelta(minutes=1)).astimezone()
        self.assertEqual(latest['time_label'], 'Posted')
        self.assertEqual(latest['time_clock'], local.strftime('%I:%M:%S %p').lstrip('0'))
        self.assertEqual(latest['time_date'], local.strftime('%d %b %Y'))
        self.assertTrue(latest['time_zone'])
        self.assertEqual(data['pending_count'], 2)
        self.assertEqual(data['posted_count'], 2)
        self.assertEqual(data['next_run_label'], (now + timedelta(days=1)).astimezone().strftime('%d %b, %I:%M %p') + ' ' + local.tzname())
        self.assertLess(html.index('Latest result'), html.index('Older result'))
        self.assertIn('posting-time-clock', html)

    def test_busy_post_registry_returns_promptly_and_dashboard_stays_accessible(self):
        lock = self.routes.scheduled_posts_lock
        lock.acquire()
        try:
            with patch.object(lock, 'request_timeout', 0.02):
                def blocked_context():
                    raise AssertionError('Recovery page ran normal context processors')
                with patch.dict(self.app.template_context_processors, {None: [blocked_context]}):
                    response = self.client.get('/auto_listing')
                self.assertEqual(response.status_code, 503)
                self.assertIn(b'Posting Results', response.data)
                self.assertEqual(self.client.get('/').status_code, 200)
        finally:
            lock.release()

    def test_create_page_does_not_query_scheduler_to_render_navigation(self):
        with patch.object(self.routes.scheduler, 'get_jobs', side_effect=AssertionError('Navigation queried scheduler')):
            self.assertEqual(self.client.get('/auto_listing').status_code, 200)

    def test_busy_listing_database_returns_retryable_json(self):
        import sqlite3
        with patch.object(self.routes, 'load_listings', side_effect=sqlite3.OperationalError('database is locked')):
            response = self.client.get('/delete_listings/data')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json['ok'])

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
                      'fuel_type', 'description'):
            tag = re.search(r'<(?:input|select|textarea)\b[^>]*name="' + field + r'"[^>]*>', html).group()
            self.assertIn('required', tag)
        for field in ('exterior_color', 'interior_color', 'vehicle_condition'):
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
        self.assertIn('disabled', message)

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
        with patch('automation.maintenance.start_scheduler_without_maintenance'):
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

    def test_backup_edit_persists_description_and_uploaded_photo(self):
        import io
        from PIL import Image
        listing_store.update_fields('test-account', 'one', {'facebook_present': False})
        listing_store.save_preferences('test-account', 'one', {'fields': {'description': 'stale', 'price': '200'}, 'image_order': ['second.jpg', 'first.jpg']})
        image = io.BytesIO()
        Image.new('RGB', (4, 3), 'blue').save(image, 'PNG')
        image.seek(0)
        def folder(*parts):
            path = self.root.joinpath(*parts)
            path.mkdir(parents=True, exist_ok=True)
            return path
        with patch.object(self.routes, 'data_dir', side_effect=folder):
            response = self.client.post('/backup_listings/one/edit', data={'description': 'Updated', 'photos': (image, 'new.png')})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        record = listing_store.load('test-account')['listings'][0]
        self.assertEqual(record['details']['description'], 'Updated')
        self.assertEqual(len(record['downloaded_images']), 3)
        self.assertTrue(Path(record['downloaded_images'][-1]['path']).exists())
        self.assertNotIn('description', record['repost_preferences']['fields'])
        self.assertEqual(record['repost_preferences']['fields']['price'], '200')
        self.assertEqual(record['repost_preferences']['image_order'][-1], record['downloaded_images'][-1]['name'])
        self.assertIn('Edit backup', self.client.get('/delete_listings/detail/one').get_data(as_text=True))

    def test_backup_edit_rejects_invalid_photos_without_changing_backup(self):
        import io
        listing_store.update_fields('test-account', 'one', {'facebook_present': False})
        before = listing_store.load('test-account')['listings'][0]
        def folder(*parts):
            path = self.root.joinpath(*parts)
            path.mkdir(parents=True, exist_ok=True)
            return path
        with patch.object(self.routes, 'data_dir', side_effect=folder):
            response = self.client.post('/backup_listings/one/edit', data={'description': 'Changed', 'photos': (io.BytesIO(b'bad'), 'bad.png')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(listing_store.load('test-account')['listings'][0], before)

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

    def test_saved_details_edit_persists_empty_and_populated_fields(self):
        listing = listing_store.load('test-account')['listings'][0]
        fields = {key: str(listing['details'].get(key) or '') for key in self.routes.REPOST_EDIT_FIELDS}
        fields.update(exterior_color='Blue', interior_color='', vin='12345', price='250')
        response = self.client.post('/delete_listings/detail/one', json={'action': 'save_details', 'fields': fields})
        self.assertEqual(response.status_code, 200)
        saved = listing_store.load('test-account')['listings'][0]
        self.assertEqual(saved['details']['exterior_color'], 'Blue')
        self.assertEqual(saved['details']['interior_color'], '')
        self.assertEqual(saved['details']['vin'], '12345')
        self.assertEqual(saved['price'], '250')
        html = self.client.get('/delete_listings/detail/one').get_data(as_text=True)
        self.assertIn('Interior Color', html)
        self.assertIn('Empty on Facebook', html)

    def test_missing_saved_colors_block_repost_before_scheduling(self):
        listing = listing_store.load('test-account')['listings'][0]
        listing['details']['interior_color'] = ''
        listing_store.save('test-account', [listing])
        response = self.client.post('/delete_listings/repost', json={'account': 'test-account', 'listing_id': 'one', 'post_mode': 'now'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('colors', response.json['message'])
        self.assertFalse(self.routes.repost_jobs)

    def test_missing_backup_colors_block_cross_post_before_scheduling(self):
        listing = listing_store.load('test-account')['listings'][0]
        listing['facebook_present'] = False
        listing['details']['exterior_color'] = ''
        listing_store.save('test-account', [listing])
        response = self.client.post('/delete_listings/cross_post', json={'account': 'test-account', 'listing_id': 'one',
                                    'backup_post': True, 'target_accounts': ['test-account'], 'post_mode': 'now'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('colors', response.json['message'])
        self.assertFalse(self.routes.scheduled_posts)

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

    def test_legacy_daily_refresh_never_starts_imports(self):
        now = datetime.now(timezone.utc)
        for hours, expected in ((23, False), (24, True)):
            with patch.object(self.routes, 'load_listings', return_value={'last_import_at': (now-timedelta(hours=hours)).isoformat(), 'updated_at': now.isoformat()}):
                self.assertEqual(self.routes._listing_snapshot_is_due('test-account', now), expected)
        with patch.object(self.routes, 'get_all_accounts', return_value=['a', 'b']), \
             patch.object(self.routes, '_listing_snapshot_is_due', return_value=True), \
             patch.object(self.routes, '_start_listing_import_job', return_value=(True, 'Started')) as start, \
             patch.object(self.routes, '_runtime_log'):
            self.routes._refresh_due_listing_snapshots()
        start.assert_not_called()

    def test_manual_import_merges_and_marks_its_own_clock(self):
        def thread(*args, **kwargs):
            from unittest.mock import MagicMock
            instance = MagicMock()
            instance.start.side_effect = kwargs['target']
            return instance
        original_load = self.routes.load_listings
        def read_without_registry_lock(account):
            acquired = self.routes.listing_import_jobs_lock.acquire(blocking=False)
            if acquired:
                self.routes.listing_import_jobs_lock.release()
            self.assertTrue(acquired, 'Import held the page-facing lock while reading SQLite')
            return original_load(account)
        with patch.object(self.routes.threading, 'Thread', side_effect=thread), \
             patch.object(self.routes, 'load_listings', side_effect=read_without_registry_lock), \
             patch.object(self.routes, 'import_listings_from_facebook') as importer:
            ok, _ = self.routes._start_listing_import_job('test-account')
        self.assertTrue(ok)
        self.assertFalse(importer.call_args.kwargs['replace_existing'])
        self.assertFalse(importer.call_args.kwargs['only_new'])
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
                    self.assertEqual(self.routes.scheduled_posts[ids[1]]['status'], 'waiting')
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
            failed_item = next(item for item in data['items'] if item['job_id'] == ids[0])
            self.assertIn('First account failed', failed_item['error'])
            with patch.object(self.routes, 'get_active_account', return_value='third'):
                target = self.client.get('/post_schedular_data').json['items']
                self.assertEqual(len(target), 1)
                self.assertEqual(target[0]['source_account'], 'test-account')
            with patch.object(self.routes, 'get_active_account', return_value='unrelated'):
                self.assertEqual(self.client.get('/post_schedular_data').json['items'], [])

    def test_clear_facebook_records_is_bulk_local_only_and_account_scoped(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [original, dict(original, id='two'), dict(original, id='backup', source_deleted=True, facebook_present=False)])
        listing_store.save('other', [dict(original, id='other-live')])
        with patch.object(self.routes, 'delete_selected_listings_from_facebook') as delete:
            response = self.client.post('/delete_listings/storage/clear', json={'account':'test-account','scope':'facebook'})
            self.assertEqual(response.status_code, 200, response.json)
            delete.assert_not_called()
        self.assertEqual([item['id'] for item in listing_store.load('test-account')['listings']], ['backup'])
        self.assertEqual({item['id'] for item in listing_store.load('other')['listings']}, {'other-live','backup'})
        self.assertTrue((self.root / 'photo.jpg').exists())
        self.assertEqual(self.client.post('/delete_listings/storage/clear', json={'account':'other','scope':'facebook'}).status_code, 409)

    def test_manage_bulk_controls_are_inline_with_one_bulk_facebook_clear_button(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(self.client.get('/delete_listings').data, 'html.parser')
        self.assertIsNone(soup.select_one('dialog'))
        self.assertEqual(len(soup.select('#facebook-panel [data-clear-facebook]')), 1)
        self.assertFalse(soup.select('#backup-panel [data-clear-facebook], .delete-card [data-clear-facebook]'))
        self.assertEqual(len(soup.select('[data-posting-controls-slot]')), 1)
        form = soup.select_one('#manage-cross-form')
        self.assertLess(str(form).index('manage-target-accounts'), str(form).index('manage-cross-delay'))

    def test_mass_account_posting_staggers_each_listing_on_all_ten_accounts(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [dict(original, id=str(index), details=dict(original['details'], model='Model ' + str(index)), source_deleted=True, facebook_present=False) for index in range(4)])
        accounts = ['test-account'] + ['account-' + str(index) for index in range(9)]
        with patch.object(self.routes, 'get_all_accounts', return_value=accounts), patch.object(self.routes.scheduler, 'add_job') as schedule:
            for index in range(4):
                response = self.cross_request(accounts, listing_id=str(index), backup_post=True, mass_post=True, post_mode='schedule', delay_value=5, delay_unit='minutes', schedule_batch='test-series', schedule_position=index)
                self.assertEqual(response.status_code, 200, response.json)
                jobs = [self.routes.scheduled_posts[job_id] for job_id in response.json['job_ids']]
                self.assertEqual(len(jobs), 10)
                self.assertEqual({job['account'] for job in jobs}, set(accounts))
                self.assertEqual(len({job['run_at'] for job in jobs}), 1)
                if index == 0:
                    anchor = jobs[0]['schedule_anchor']
                expected = anchor + timedelta(minutes=5 * (index + 1))
                self.assertTrue(all(job['run_at'] == expected for job in jobs))
                self.assertEqual(schedule.call_args.kwargs['run_date'], expected)
                self.assertEqual(response.json['run_at'], expected.strftime('%Y-%m-%d %H:%M:%S'))
            rows = self.client.get('/post_schedular_data').json['items']
            self.assertEqual(len(rows), 40)
            for row in rows:
                meta = self.routes.scheduled_posts[row['job_id']]
                self.assertEqual(row['time_label'], 'Scheduled')
                self.assertEqual(row['time_iso'], meta['run_at'].astimezone().isoformat(timespec='seconds'))
                self.assertAlmostEqual(row['remaining_seconds'], (meta['run_at'] - datetime.now()).total_seconds(), delta=2)

    def test_mass_schedule_requires_consistent_interval_order_and_targets(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [original, dict(original, id='two')])
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account','other','third']), patch.object(self.routes.scheduler, 'add_job'):
            args = dict(mass_post=True, post_mode='schedule', delay_value=5, delay_unit='minutes', schedule_batch='series', schedule_position=0)
            self.assertEqual(self.cross_request(['other'], **args).status_code, 200)
            args.update(listing_id='two', schedule_position=1)
            self.assertEqual(self.cross_request(['other'], **dict(args, delay_value=6)).status_code, 409)
            self.assertEqual(self.cross_request(['third'], **args).status_code, 409)
            self.assertEqual(self.cross_request(['other'], **dict(args, schedule_position=3)).status_code, 409)
            self.assertEqual(self.cross_request(['other'], **dict(args, schedule_position=True)).status_code, 400)
            self.assertEqual(self.cross_request(['other'], **args).status_code, 200)
            jobs = list(self.routes.scheduled_posts.values())
            self.assertEqual(jobs[1]['run_at'] - jobs[0]['run_at'], timedelta(minutes=5))

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
                    self.assertEqual(self.routes.scheduled_posts[second_ids[0]]['status'], 'waiting')
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
        for url in ('/backup_listings', '/delete_listings/detail/one'):
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

    def manual_form(self, **changes):
        from io import BytesIO
        from PIL import Image
        image = BytesIO()
        Image.new('RGB', (900, 600), 'blue').save(image, format='PNG')
        image.seek(0)
        return dict({'account': 'test-account', 'vehicle_type': 'Car', 'year': '2026',
                     'make': 'Honda', 'model': 'City', 'mileage': '1000', 'price': '5000',
                     'body_style': 'Sedan', 'fuel_type': 'Gasoline', 'transmission': 'Automatic transmission',
                     'description': '', 'images': (image, 'car.png')}, **changes)

    def test_save_manual_template_requires_fields_and_is_shared(self):
        with patch.object(self.routes, '_manual_images_directory', return_value=str(self.root / 'uploads')), \
             patch('automation.manual_backups.data_dir', return_value=self.root / 'durable'), \
             patch.object(self.routes.scheduler, 'add_job') as schedule, \
             patch.object(self.routes, 'post_to_fb_marketplace') as publish:
            response = self.client.post('/auto_listing_manual', data=self.manual_form(submit_action='save_template'))
        self.assertEqual(response.status_code, 302)
        records = listing_store.load('test-account')['listings']
        template = next(item for item in records if item.get('manual_template'))
        self.assertFalse(template['facebook_present'])
        self.assertTrue(template['source_deleted'])
        self.assertEqual(template['details']['make'], 'Honda')
        self.assertTrue(Path(template['downloaded_images'][0]['path']).is_file())
        self.assertIn('durable', template['downloaded_images'][0]['path'])
        for source in (self.root / 'uploads').iterdir():
            source.unlink()
        self.assertTrue(Path(template['downloaded_images'][0]['path']).is_file())
        self.assertEqual(listing_store.load('other')['listings'], [template])
        schedule.assert_not_called()
        publish.assert_not_called()

    def test_invalid_manual_template_is_not_saved_or_scheduled(self):
        for changes in ({'make': ''}, {'mileage': '100'}, {'year': '1800'}, {'price': '-1'}, {'body_style': ''}):
            with patch.object(self.routes.scheduler, 'add_job') as schedule:
                response = self.client.post('/auto_listing_manual',
                    data=self.manual_form(submit_action='save_template', **changes))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(listing_store.load('test-account')['listings']), 1)
            schedule.assert_not_called()

    def test_manual_template_rejects_account_change(self):
        response = self.client.post('/auto_listing_manual',
            data=self.manual_form(submit_action='save_template', account='other'))
        self.assertIn(b'active account changed', response.data)
        self.assertEqual(len(listing_store.load('test-account')['listings']), 1)
        self.assertEqual(listing_store.load('other')['listings'], [])

    def test_manual_post_is_saved_only_after_existing_confirmation_succeeds(self):
        with patch.object(self.routes, '_manual_images_directory', return_value=str(self.root / 'uploads')), \
             patch('automation.manual_backups.data_dir', return_value=self.root / 'durable'), \
             patch.object(self.routes.scheduler, 'add_job') as schedule, \
             patch.object(self.routes, '_runtime_log'), patch.object(self.routes, '_log_event'):
            response = self.client.post('/auto_listing_manual', data=self.manual_form(manual_timing='now'))
            self.assertEqual(response.status_code, 200)
            payload, account, job_id = schedule.call_args.kwargs['args']
            self.assertTrue(payload['_require_publish_confirmation'])
            self.assertEqual(len(listing_store.load(account)['listings']), 1)
            with patch.object(self.routes, 'post_to_fb_marketplace', return_value=False):
                self.assertFalse(self.routes._post_single_listing(payload, account, job_id))
            self.assertEqual(len(listing_store.load(account)['listings']), 1)
            self.routes.scheduled_posts[job_id]['status'] = 'pending'
            with patch.object(self.routes, 'post_to_fb_marketplace', return_value=True):
                self.assertTrue(self.routes._post_single_listing(payload, account, job_id))
        record = listing_store.load(account)['listings'][0]
        self.assertTrue(record['facebook_present'])
        self.assertFalse(record['source_deleted'])
        self.assertFalse(record['manual_template'])
        self.assertEqual(record['details']['title'], '2026 Honda City')
        self.assertTrue(Path(record['downloaded_images'][0]['path']).is_file())
        self.assertEqual(listing_store.load('other')['listings'], [])

    def test_manual_delay_remains_scheduled_with_confirmation(self):
        with patch.object(self.routes, '_manual_images_directory', return_value=str(self.root / 'uploads')), \
             patch.object(self.routes.scheduler, 'add_job') as schedule:
            response = self.client.post('/auto_listing_manual', data=self.manual_form(
                manual_timing='schedule', delay_unit='minutes', delay_value='5'))
        self.assertEqual(response.status_code, 200)
        self.assertGreater(schedule.call_args.kwargs['run_date'], datetime.now() + timedelta(minutes=4))
        self.assertTrue(schedule.call_args.kwargs['args'][0]['_require_publish_confirmation'])
        self.assertEqual(len(listing_store.load('test-account')['listings']), 1)

    def test_clear_backups_preserves_facebook_and_other_account_records(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [original, dict(original, id='backup', source_deleted=True, facebook_present=False)])
        listing_store.save('other', [dict(original, id='other-backup', source_deleted=True)])
        response = self.client.post('/delete_listings/storage/clear', json={'scope': 'backups', 'account': 'test-account'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in listing_store.load('test-account')['listings']], ['one'])
        self.assertEqual(len(listing_store.load('other')['listings']), 0)
        self.assertEqual(self.client.post('/delete_listings/storage/clear',
            json={'scope': 'backups', 'account': 'other'}).status_code, 409)

    def test_manage_sections_sort_latest_facebook_first_and_backups_last(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [dict(original, id='backup', source_deleted=True, facebook_present=False, created_at='2026-12-01'),
            dict(original, id='older', listed_date='2026-01-01'), dict(original, id='newer', listed_date='2026-10-21')])
        ids = [item['id'] for item in self.client.get('/delete_listings/data').json['listings']]
        self.assertEqual(ids, ['newer', 'older', 'backup'])
        html = self.client.get('/delete_listings').get_data(as_text=True)
        self.assertLess(html.index('data-id="newer"'), html.index('data-id="older"'))
        self.assertNotIn('data-id="backup"', html)
        backup_html = self.client.get('/backup_listings').data.decode()
        self.assertIn('data-id="backup"', backup_html)
        self.assertNotIn('data-id="older"', backup_html)
        self.assertIn('Clear all backups', backup_html)
        self.assertIn('Post to other accounts', html)

    def test_manual_page_exposes_template_and_now_delay_controls(self):
        html = self.client.get('/auto_listing_manual').get_data(as_text=True)
        self.assertIn('Save template', html)
        self.assertIn('manual_timing', html)
        self.assertIn('submit_action', html)

    def test_facebook_delete_moves_reposted_listing_to_backups_without_losing_media(self):
        listing_store.update_fields('test-account', 'one', {'facebook_present': True, 'facebook_reposted': True})
        workers = []
        from unittest.mock import MagicMock
        def make_worker(*args, **kwargs):
            workers.append(kwargs['target'])
            return MagicMock()
        with patch.object(self.routes.threading, 'Thread', side_effect=make_worker), \
             patch.object(self.routes, 'delete_selected_listings_from_facebook',
                          return_value={'deleted_ids': ['one'], 'deleted': 1}), \
             patch.object(self.routes, '_runtime_log'):
            response = self.client.post('/delete_listings/delete', json={'ids': ['one'], 'account': 'test-account'})
            self.assertEqual(response.status_code, 200)
            workers[0]()
        listing = listing_store.load('test-account')['listings'][0]
        self.assertTrue(listing['source_deleted'])
        self.assertFalse(listing['facebook_present'])
        self.assertFalse(listing['facebook_reposted'])
        self.assertEqual(len(listing['downloaded_images']), 2)

    def test_confirmed_deletion_is_saved_before_batch_ends_even_if_next_fails(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [original, dict(original, id='two')])
        workers = []
        from unittest.mock import MagicMock
        def make_worker(*args, **kwargs):
            workers.append(kwargs['target'])
            return MagicMock()
        def delete(account, items, **kwargs):
            kwargs['progress_callback']({'listing_id': 'one', 'confirmed_deleted': True,
                'clicked': 1, 'deleted': 1, 'message': 'Listing deleted.'})
            records = {item['id']: item for item in listing_store.load(account)['listings']}
            self.assertTrue(records['one']['source_deleted'])
            self.assertFalse(records['one']['facebook_present'])
            self.assertFalse(records['two'].get('source_deleted', False))
            self.assertTrue(self.routes.facebook_delete_jobs[account]['running'])
            raise RuntimeError('Second listing failed')
        with patch.object(self.routes.threading, 'Thread', side_effect=make_worker), \
             patch.object(self.routes, 'delete_selected_listings_from_facebook', side_effect=delete), \
             patch.object(self.routes, '_runtime_log'):
            self.assertEqual(self.client.post('/delete_listings/delete', json={'ids': ['one', 'two']}).status_code, 200)
            workers[0]()
        records = {item['id']: item for item in listing_store.load('test-account')['listings']}
        self.assertTrue(records['one']['source_deleted'])
        self.assertFalse(records['two'].get('source_deleted', False))
        self.assertEqual(self.routes.facebook_delete_jobs['test-account']['deleted'], 1)

    def test_clear_one_backup_preserves_live_and_other_backups(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [original, dict(original, id='backup', source_deleted=True, facebook_present=False),
            dict(original, id='backup-two', details=dict(original['details'], price='200'), source_deleted=True)])
        listing_store.save('other', [dict(original, id='backup', source_deleted=True)])
        endpoint = '/delete_listings/backup/backup/clear'
        self.assertEqual(self.client.post(endpoint, json={'account': 'other'}).status_code, 409)
        self.assertEqual(self.client.post('/delete_listings/backup/one/clear', json={'account': 'test-account'}).status_code, 409)
        self.assertEqual(self.client.post(endpoint, json={'account': 'test-account'}).status_code, 200)
        self.assertEqual({item['id'] for item in listing_store.load('test-account')['listings']}, {'one', 'backup-two'})
        self.assertEqual(len(listing_store.load('other')['listings']), 1)
        self.assertTrue((self.root / 'photo.jpg').is_file())

    def test_backup_cards_offer_posting_actions_without_global_clear(self):
        from bs4 import BeautifulSoup
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [original, dict(original, id='backup', source_deleted=True, facebook_present=False)])
        soup = BeautifulSoup(self.client.get('/delete_listings').data, 'html.parser')
        self.assertIsNone(soup.select_one('#listing-clear-storage-btn'))
        live = soup.select_one('.delete-card[data-id="one"]')
        backup_soup = BeautifulSoup(self.client.get('/backup_listings').data, 'html.parser')
        backup = backup_soup.select_one('.delete-card[data-id="backup"]')
        self.assertIsNone(live.select_one('[data-clear-backup]'))
        self.assertIsNone(live.select_one('.listing-backup-action'))
        self.assertEqual(backup.select('.listing-backup-action'), [])
        self.assertEqual([button.text.strip() for button in backup_soup.select('[data-backup-action]')], ['Post on accounts'])
        self.assertEqual([el.text.strip() for el in backup.select('.listing-actions a,.listing-actions button')], ['View', 'Clear backup'])
        for card in (live, backup):
            self.assertEqual(card.select_one('.listing-view-btn').text, 'View')

    def test_backup_batch_can_queue_multiple_reposts_at_the_same_time(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [dict(original, id=name, source_deleted=True, facebook_present=False) for name in ('backup-one', 'backup-two')])
        with patch.object(self.routes.scheduler, 'add_job') as queue:
            for name in ('backup-one', 'backup-two'):
                response = self.client.post('/delete_listings/repost', json={'listing_id': name,
                    'account': 'test-account', 'post_mode': 'schedule', 'delay_value': 1, 'delay_unit': 'minutes', 'backup_batch': True})
                self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(queue.call_count, 2)

    def test_mass_actions_require_delay_but_individual_now_stays_available(self):
        with patch.object(self.routes.scheduler, 'add_job') as queue:
            for path, flags in [('/delete_listings/repost', {'backup_batch': True}),
                                ('/delete_listings/cross_post', {'mass_post': True})]:
                response = self.client.post(path, json={'account': 'test-account', 'post_mode': 'now', **flags})
                self.assertEqual(response.status_code, 400)
                self.assertIn('delay', response.json['message'])
            queue.assert_not_called()
        self.assertIn(b'value="now"', self.client.get('/delete_listings/detail/one').data)

    def test_identical_backups_show_once_and_clear_all_matching_copies(self):
        from bs4 import BeautifulSoup
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [original, dict(original, id='backup-one', facebook_present=False, source_deleted=True), dict(original, id='backup-two', facebook_present=False, source_deleted=True)])
        soup = BeautifulSoup(self.client.get('/backup_listings').data, 'html.parser')
        cards = soup.select('.delete-card')
        self.assertEqual(len(cards), 1)
        rows = self.client.get('/delete_listings/data').json['listings']
        self.assertEqual(len(rows), 2)
        backup_id = cards[0]['data-id']
        self.routes.scheduled_posts['pending'] = {'source':'Cross posting', 'source_account':'other', 'source_listing_id':'backup-two' if backup_id=='backup-one' else 'backup-one', 'status':'pending', 'account':'other'}
        self.assertEqual(self.client.post('/delete_listings/backup/' + backup_id + '/clear', json={'account':'test-account'}).status_code, 409)
        self.routes.scheduled_posts.clear()
        self.assertEqual(self.client.post('/delete_listings/backup/' + backup_id + '/clear', json={'account':'test-account'}).status_code, 200)
        self.assertEqual([item['id'] for item in listing_store.load('test-account')['listings']], ['one'])
        self.assertTrue((self.root / 'photo.jpg').is_file())

    def test_backups_have_own_sidebar_page_and_keep_account_switch_navigation(self):
        from bs4 import BeautifulSoup
        original = listing_store.load('test-account')['listings'][0]
        listing_store.add_at_top('test-account', dict(original, id='shared-backup', facebook_present=False, source_deleted=True))
        soup = BeautifulSoup(self.client.get('/backup_listings').data, 'html.parser')
        self.assertIsNotNone(soup.select_one('#backup-panel'))
        self.assertIsNone(soup.select_one('#facebook-panel'))
        self.assertIsNone(soup.select_one('#listing-import-form'))
        self.assertIsNotNone(soup.select_one('li.active a[href="/backup_listings"]'))
        self.assertIsNotNone(soup.select_one('.delete-card[data-id="shared-backup"]'))
        with patch.object(self.routes, 'get_active_account', return_value='other'):
            other = BeautifulSoup(self.client.get('/backup_listings').data, 'html.parser')
            self.assertIsNotNone(other.select_one('.delete-card[data-id="shared-backup"]'))
        detail = BeautifulSoup(self.client.get('/delete_listings/detail/shared-backup').data, 'html.parser')
        self.assertEqual(detail.select_one('.ld-back')['href'], '/backup_listings')
        self.assertIsNotNone(detail.select_one('li.active a[href="/backup_listings"]'))
        response = self.client.post('/set_active_account', data={'account':'other'}, headers={'Referer':'http://localhost/backup_listings'})
        self.assertTrue(response.location.endswith('/backup_listings'))

    def test_manage_panels_own_their_buttons(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(self.client.get('/delete_listings').data, 'html.parser')
        posted = soup.select_one('#facebook-panel')
        backups = BeautifulSoup(self.client.get('/backup_listings').data, 'html.parser').select_one('#backup-panel')
        self.assertIsNone(soup.select_one('#backup-panel'))
        self.assertIsNotNone(posted)
        self.assertIsNotNone(backups)
        for selector in ('#listing-refresh-clicks-btn', '#listing-cross-post-btn', '#listing-facebook-delete-btn'):
            self.assertIsNotNone(posted.select_one(selector))
        self.assertEqual(len(backups.select('[data-backup-action]')), 1)
        self.assertIsNotNone(backups.select_one('[data-clear-backups]'))
        self.assertFalse(soup.select('.delete-secondary-row button'))

    def test_post_dialog_uses_card_selection_and_includes_active_account(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(self.client.get('/delete_listings').data, 'html.parser')
        dialog = soup.select_one('#manage-cross-dialog')
        self.assertIsNone(dialog.select_one('#backup-popup-listings'))
        self.assertIsNotNone(dialog.select_one('#manage-cross-value'))
        self.assertIsNotNone(dialog.select_one('[name=cross_account][value="test-account"]'))
        self.assertIsNotNone(soup.select_one('#facebook-panel [data-facebook-action="repost"]'))
        self.assertIsNone(soup.select_one('#backup-panel [data-backup-action="repost"]'))

    def test_backup_post_allows_active_account_only_for_real_backups(self):
        with patch.object(self.routes, 'get_all_accounts', return_value=['test-account', 'other']), patch.object(self.routes.scheduler, 'add_job'):
            self.assertEqual(self.cross_request(['test-account'], backup_post=True).status_code, 409)
            original = listing_store.load('test-account')['listings'][0]
            listing_store.save('test-account', [dict(original, source_deleted=True, facebook_present=False)])
            self.assertEqual(self.cross_request(['test-account']).status_code, 400)
            response = self.cross_request(['test-account', 'other'], backup_post=True, mass_post=True, post_mode='schedule', delay_value=1, delay_unit='minutes')
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual({meta['account'] for meta in self.routes.scheduled_posts.values()}, {'test-account', 'other'})

    def test_shared_backup_visible_and_postable_after_switching_accounts(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.add_at_top('test-account', dict(original, id='shared', facebook_present=False, source_deleted=True))
        with patch.object(self.routes, 'get_active_account', return_value='other'), patch.object(self.routes, 'get_all_accounts', return_value=['test-account','other']), patch.object(self.routes.scheduler, 'add_job'):
            records = self.client.get('/delete_listings/data').json['listings']
            self.assertEqual([item['id'] for item in records], ['shared'])
            self.assertEqual(self.client.get('/delete_listings/photo/shared/first.jpg').status_code, 200)
            response = self.cross_request(['other'], account='other', listing_id='shared', backup_post=True, mass_post=True, post_mode='schedule', delay_value=1, delay_unit='minutes')
            self.assertEqual(response.status_code, 200, response.json)
        self.assertIn('shared', self.routes._active_repost_listing_ids('test-account'))
        self.assertEqual(self.client.post('/delete_listings/backup/shared/clear', json={'account':'test-account'}).status_code, 409)
        self.assertEqual(self.client.post('/delete_listings/storage/clear', json={'account':'test-account','scope':'backups'}).status_code, 409)

    def test_reposted_source_requires_current_facebook_id_before_reposting_again(self):
        listing_store.update_fields('test-account', 'one', {'source_deleted':True, 'facebook_reposted':True, 'facebook_present':True})
        with patch.object(self.routes.scheduler, 'add_job') as queue:
            response = self.client.post('/delete_listings/repost', json={'account':'test-account', 'listing_id':'one', 'mass_post':True, 'post_mode':'schedule', 'delay_value':1, 'delay_unit':'minutes'})
            self.assertEqual(response.status_code, 400)
            self.assertIn('current Facebook listing ID', response.json['message'])
            queue.assert_not_called()

    def test_failed_repost_after_confirmed_deletion_is_shared_backup(self):
        listing_store.update_fields('test-account', 'one', {'source_deleted':False, 'facebook_reposted':True, 'facebook_present':True})
        listing = listing_store.load('test-account')['listings'][0]
        with patch.object(self.routes, 'delete_selected_listings_from_facebook', return_value={'deleted':1}), patch.object(self.routes, 'post_to_fb_marketplace', return_value=False), patch.object(self.routes, '_runtime_log'), patch.object(self.routes, '_record_repost_history'):
            self.routes._run_saved_listing_repost(listing, 'test-account')
        backup = listing_store.load('other')['listings'][0]
        self.assertEqual(backup['id'], 'one')
        self.assertFalse(backup['facebook_present'])
        self.assertFalse(backup['facebook_reposted'])
        self.assertEqual(backup['repost_status'], 'failed')

    def test_bulk_repost_rejects_backups(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [dict(original, source_deleted=True, facebook_present=False)])
        response = self.client.post('/delete_listings/repost', json={'account':'test-account', 'listing_id':'one', 'mass_post':True, 'post_mode':'schedule', 'delay_value':1, 'delay_unit':'minutes'})
        self.assertEqual(response.status_code, 409)

    def test_bulk_live_reposts_allow_same_delay(self):
        original = listing_store.load('test-account')['listings'][0]
        listing_store.save('test-account', [dict(original, id=name) for name in ('live-one', 'live-two')])
        with patch.object(self.routes.scheduler, 'add_job') as queue:
            for name in ('live-one', 'live-two'):
                response = self.client.post('/delete_listings/repost', json={'account':'test-account', 'listing_id':name, 'mass_post':True, 'post_mode':'schedule', 'delay_value':1, 'delay_unit':'minutes'})
                self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(queue.call_count, 2)


if __name__ == '__main__':
    unittest.main()
