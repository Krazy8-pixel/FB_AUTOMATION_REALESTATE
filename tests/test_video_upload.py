import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from automation.video_upload import upload_optional_videos


class OptionalVideoTests(unittest.TestCase):
    def test_failed_video_does_not_prevent_next_video(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / name for name in ('first.mp4', 'second.mp4')]
            for path in paths:
                path.write_bytes(b'video')
            driver = MagicMock()
            element = MagicMock()
            element.get_attribute.return_value = 'image/*,video/*'
            element.send_keys.side_effect = [RuntimeError('upload rejected'), None]
            driver.find_elements.side_effect = lambda by, selector: [element] if selector.startswith("input[type=") else []
            self.assertEqual(upload_optional_videos(driver, paths, log=MagicMock()), 1)
            self.assertEqual(element.send_keys.call_count, 2)

    def test_missing_video_is_skipped_without_touching_composer(self):
        driver = MagicMock()
        self.assertEqual(upload_optional_videos(driver, ['missing-video.mp4'], log=MagicMock()), 0)
        driver.find_elements.assert_not_called()
