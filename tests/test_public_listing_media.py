from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from selenium.common.exceptions import TimeoutException
from PIL import Image

from automation.public_listing_media import full_photo_url, listing_hd_videos, read_public_listing_media
from automation import reposting


def photo(name, transform='dst-jpg_s960x960_tt6'):
    return f'https://scontent.example.fbcdn.net/{name}.jpg?stp={transform}&oh=signed'


def navigation_driver(url):
    driver = MagicMock()
    driver.current_url = url
    driver.current_window_handle = 'inventory'
    driver.window_handles = ['inventory']
    driver.timeouts.page_load = 45
    driver.get.side_effect = lambda value: setattr(driver, 'current_url', value)
    def new_window(kind):
        driver.current_window_handle = 'public'
        driver.window_handles.append('public')
    driver.switch_to.new_window.side_effect = new_window
    driver.switch_to.window.side_effect = lambda handle: setattr(driver, 'current_window_handle', handle)
    driver.close.side_effect = lambda: driver.window_handles.remove(driver.current_window_handle)
    return driver


def fallback_until(driver, condition, message=''):
    if 'Selected popup did not expose' in message:
        raise TimeoutException('No public link in fallback fixture')
    result = condition(driver)
    if not result:
        raise TimeoutException(message)
    return result


class PublicMediaTests(unittest.TestCase):
    def test_confirmed_empty_description_skips_unnecessary_ocr(self):
        fallback = MagicMock()
        post = {'description': '', 'field_states': {'description': 'empty'}}
        reposting.recover_unreadable_fields(MagicMock(), post, fallback,
            fields=('description',), skip_confirmed_empty=True)
        fallback.assert_not_called()
        self.assertEqual(post['field_states']['description'], 'empty')

    def test_unavailable_description_still_uses_recovery(self):
        fallback = MagicMock(return_value={'posts': [{'description': 'Recovered description',
            'field_states': {'description': 'read'}}]})
        post = {'description': '', 'field_states': {'description': 'unavailable'}}
        reposting.recover_unreadable_fields(MagicMock(), post, fallback,
            fields=('description',), skip_confirmed_empty=True)
        fallback.assert_called_once()
        self.assertEqual(post['description'], 'Recovered description')

    def test_download_preserves_public_photo_bytes_without_resolution_rejection(self):
        from automation import delete_listing_import as importer
        content = BytesIO()
        Image.new('RGB', (261, 260)).save(content, format='JPEG')
        driver = MagicMock()
        driver.execute_script.return_value = 'Test browser'
        driver.get_cookies.return_value = []
        response = MagicMock()
        response.iter_content.return_value = [content.getvalue()]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(importer, 'IMPORT_DIR', Path(folder)), \
             patch.object(importer.requests, 'Session') as session:
            session.return_value.__enter__.return_value.get.return_value = response
            photos, errors = importer.download_listing_images(driver, 'account', '123',
                [photo('looks-large')], threading.Event())
            self.assertEqual(Path(photos[0]['path']).read_bytes(), content.getvalue())
        self.assertEqual(len(photos), 1)
        self.assertEqual(errors, [])

    def test_uncropped_portrait_and_largest_advertised_source(self):
        self.assertEqual(full_photo_url({'url': photo('portrait')}), photo('portrait'))
        self.assertEqual(full_photo_url({'url': photo('small', 'dst-jpg_s260x260'),
            'srcset': f"{photo('large', 'dst-jpg_s1600x1600')} 1600w"}),
            photo('large', 'dst-jpg_s1600x1600'))

    def test_public_sources_are_not_rejected_by_crop_or_size_parameters(self):
        for url in [photo('crop', 'c0.101.261.261a_dst-jpg_p261x260_tt6'),
                    photo('small', 'dst-jpg_p261x260')]:
            self.assertEqual(full_photo_url({'url': url}), url)
        for url in ['https://example.com/a.jpg', 'blob:photo']:
            self.assertIsNone(full_photo_url({'url': url}))

    def test_order_deduplication_and_optional_video_absence(self):
        driver = MagicMock()
        driver.page_source = ''
        driver.execute_script.return_value = {'images': [{'url': photo('one')},
            {'url': photo('two')}, {'url': photo('one')}], 'videos': []}
        result = read_public_listing_media(driver, '123')
        self.assertEqual(result['image_urls'], [photo('one'), photo('two')])
        self.assertEqual(result['video_urls'], [])
        self.assertEqual(result['video_errors'], [])

    def test_missing_gallery_source_reported_instead_of_silently_losing_photo(self):
        driver = MagicMock()
        driver.execute_script.return_value = {'images': [{'url': photo('one')},
            {'url': ''}], 'videos': []}
        with self.assertRaisesRegex(RuntimeError, 'URL unavailable'):
            read_public_listing_media(driver, '123')

    def test_hd_metadata_scoped_to_listing_and_ignores_recommendations(self):
        html = '''<script type="application/json">{"results":[
          {"id":"123","listing_videos":[{"browser_native_hd_url":"https://video.example.fbcdn.net/hd.mp4",
            "playable_url":"https://video.example.fbcdn.net/sd.mp4"}]},
          {"id":"999","videos":[{"browser_native_hd_url":"https://video.example.fbcdn.net/other.mp4"}]}]}
          </script>'''
        self.assertEqual(listing_hd_videos(html, '123'), ['https://video.example.fbcdn.net/hd.mp4'])

    def test_streaming_video_skipped_without_blocking_photos(self):
        driver = MagicMock()
        driver.page_source = ''
        driver.execute_script.return_value = {'images': [{'url': photo('one')}],
            'videos': [{'urls': ['blob:video'], 'width': 1920, 'height': 1080}]}
        result = read_public_listing_media(driver, '123')
        self.assertEqual(result['image_urls'], [photo('one')])
        self.assertFalse(result['video_urls'])
        self.assertTrue(result['video_errors'])

    def test_import_reads_public_media_before_edit_and_never_reads_edit_media(self):
        driver, link = MagicMock(), MagicMock()
        driver.current_url = 'https://www.facebook.com/marketplace/item/123/'
        link.get_attribute.return_value = 'https://www.facebook.com/marketplace/edit/?listing_id=123'
        sources = {'image_urls': [photo('full')], 'video_urls': [], 'video_errors': []}
        calls = []
        with patch.object(reposting, '_safe_click', return_value=True), \
             patch.object(reposting, 'WebDriverWait'), \
             patch.object(reposting, '_model_from_visible_listing_heading', return_value='Focus'), \
             patch.object(reposting, '_find_edit_listing_link', side_effect=lambda *a, **k: calls.append('edit') or link), \
             patch.object(reposting, '_public_media_in_new_tab', side_effect=lambda *a: calls.append('public') or sources), \
             patch.object(reposting, '_safe_image_urls') as edit_images, \
             patch('automation.listing_videos.discover') as edit_videos:
            result = reposting.read_listing_from_manage('account',
                {'year': '2020', 'make': 'Ford', 'model': 'Focus', 'url': driver.current_url},
                driver=driver, listing_card=MagicMock(), return_to_inventory=MagicMock(),
                field_reader=lambda current: {'posts': [{'price': '100', 'image_urls': ['bad-edit-image']}]},
                public_media=True)
        self.assertEqual(calls, ['public', 'edit'])
        self.assertEqual(result['posts'][0]['image_urls'], sources['image_urls'])
        self.assertEqual(result['posts'][0]['price'], '100')
        edit_images.assert_not_called()
        edit_videos.assert_not_called()

    def test_dialog_edit_link_resolves_missing_card_url_and_restores_tab(self):
        driver = navigation_driver('https://www.facebook.com/marketplace/you/selling')
        edit = MagicMock()
        edit.get_attribute.return_value = 'https://www.facebook.com/marketplace/edit/?listing_id=123'
        edit.is_displayed.return_value = True
        driver.find_elements.side_effect = lambda by, selector: [] if 'dialog' in selector else [edit]
        sources = {'image_urls': [photo('full')], 'video_urls': [], 'video_errors': []}
        with patch.object(reposting, 'WebDriverWait') as wait, \
             patch('automation.public_listing_media.read_public_listing_media', return_value=sources):
            wait.return_value.until.side_effect = lambda condition, **kwargs: fallback_until(driver, condition, **kwargs)
            self.assertEqual(reposting._public_media_in_new_tab(driver,
                {'url': 'https://www.facebook.com/marketplace/item/999/'}), sources)
        driver.get.assert_called_once_with('https://www.facebook.com/marketplace/item/123/')
        driver.switch_to.new_window.assert_called_once_with('tab')
        driver.close.assert_called_once()
        self.assertEqual(driver.switch_to.window.call_args.args, ('inventory',))
        self.assertEqual(driver.set_page_load_timeout.call_args_list[-1].args, (45,))
        self.assertEqual([call.args[1] for call in wait.call_args_list], [20, 25, 25])

    def test_gallery_timeout_closes_only_public_tab_and_restores_timeout(self):
        driver = navigation_driver('https://www.facebook.com/marketplace/item/123/')
        with patch.object(reposting, 'WebDriverWait') as wait, \
             patch('automation.public_listing_media.read_public_listing_media', side_effect=TimeoutException('gallery')):
            wait.return_value.until.side_effect = lambda condition, **kwargs: fallback_until(driver, condition, **kwargs)
            with self.assertRaisesRegex(RuntimeError, 'fallback tab: TimeoutException: Message: gallery'):
                reposting._public_media_in_new_tab(driver, {})
        driver.close.assert_called_once()
        self.assertEqual(driver.window_handles, ['inventory'])
        self.assertEqual(driver.switch_to.window.call_args.args, ('inventory',))
        self.assertEqual(driver.set_page_load_timeout.call_args_list[-1].args, (45,))

    def test_media_timeout_does_not_prevent_detail_extraction(self):
        driver, link = MagicMock(), MagicMock()
        driver.current_url = 'https://www.facebook.com/marketplace/edit/?listing_id=123'
        link.get_attribute.return_value = driver.current_url
        with patch.object(reposting, '_safe_click', return_value=True), \
             patch.object(reposting, 'WebDriverWait'), \
             patch.object(reposting, '_model_from_visible_listing_heading', return_value='Focus'), \
             patch.object(reposting, '_find_edit_listing_link', return_value=link), \
             patch.object(reposting, '_public_media_in_new_tab', side_effect=TimeoutException('public gallery timed out')):
            result = reposting.read_listing_from_manage('account',
                {'year': '2020', 'make': 'Ford', 'model': 'Focus'},
                driver=driver, listing_card=MagicMock(), return_to_inventory=MagicMock(),
                field_reader=lambda current: {'posts': [{'price': '100', 'description': 'fresh details'}]},
                public_media=True)
        post = result['posts'][0]
        self.assertEqual(post['price'], '100')
        self.assertEqual(post['description'], 'fresh details')
        self.assertEqual(post['image_urls'], [])
        self.assertIn('timed out', post['photo_errors'][0])


if __name__ == '__main__':
    unittest.main()
