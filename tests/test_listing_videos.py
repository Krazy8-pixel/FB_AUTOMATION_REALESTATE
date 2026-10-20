import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from automation import listing_videos


class ListingVideoTests(unittest.TestCase):
    def test_discovery_filters_blobs_and_deduplicates(self):
        driver = MagicMock()
        url = 'https://video.fbcdn.net/clip.mp4'
        driver.execute_script.return_value = [url, url, 'blob:https://www.facebook.com/123']
        urls, errors = listing_videos.discover(driver)
        self.assertEqual(urls, [url])
        self.assertEqual(len(errors), 1)
        self.assertFalse(listing_videos.allowed_source('https://fbcdn.net.evil.test/a.mp4'))

    def test_discovery_failure_is_optional(self):
        driver = MagicMock()
        driver.execute_script.side_effect = RuntimeError('no video DOM')
        urls, errors = listing_videos.discover(driver)
        self.assertEqual(urls, [])
        self.assertTrue(errors)

    def test_download_preserves_bytes_and_skips_failed_video(self):
        with tempfile.TemporaryDirectory() as folder:
            response = MagicMock()
            response.__enter__.return_value = response
            response.is_redirect = False
            response.headers = {'Content-Type': 'video/mp4'}
            response.iter_content.return_value = [b'original-', b'video-bytes']
            session = MagicMock()
            session.__enter__.return_value = session
            session.get.side_effect = [RuntimeError('expired'), response]
            driver = MagicMock()
            driver.get_cookies.return_value = []
            driver.execute_script.return_value = 'test-agent'
            with patch.object(listing_videos.requests, 'Session', return_value=session):
                videos, errors = listing_videos.download(driver, 'account', '123',
                    ['https://video.fbcdn.net/old.mp4', 'https://video.fbcdn.net/new.mp4'],
                    folder, threading.Event())
            self.assertEqual(len(videos), 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(Path(videos[0]['path']).read_bytes(), b'original-video-bytes')
            self.assertEqual(listing_videos.saved_paths({'downloaded_videos': videos}), [videos[0]['path']])
            self.assertEqual(list(Path(folder).rglob('*.part')), [])

    def test_non_video_response_is_not_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            response = MagicMock()
            response.__enter__.return_value = response
            response.is_redirect = False
            response.headers = {'Content-Type': 'text/html'}
            session = MagicMock()
            session.__enter__.return_value = session
            session.get.return_value = response
            with patch.object(listing_videos.requests, 'Session', return_value=session):
                videos, errors = listing_videos.download(MagicMock(), 'a', '1',
                    ['https://video.fbcdn.net/clip.mp4'], folder, threading.Event())
            self.assertEqual(videos, [])
            self.assertTrue(errors)
            self.assertEqual(list(Path(folder).rglob('*.mp4')), [])
