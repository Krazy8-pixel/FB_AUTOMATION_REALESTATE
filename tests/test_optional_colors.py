import unittest
from unittest.mock import MagicMock, patch
from automation import auto_listing_excel_poster as poster


class OptionalColorsTests(unittest.TestCase):
    def test_repost_color_failure_is_not_silently_ignored(self):
        with patch.object(poster, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = TimeoutError('missing')
            with self.assertRaisesRegex(RuntimeError, 'could not be retained'):
                poster._fill_optional_color(MagicMock(), 'Exterior colour', 'White', strict=True)

    def test_missing_field_skips_without_aborting(self):
        with patch.object(poster, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = TimeoutError('missing')
            self.assertIsNone(poster._fill_optional_color(MagicMock(), 'Exterior colour', 'Blue'))
            self.assertLessEqual(wait.call_args.args[1], 25)

    def test_selects_mapped_color_with_shared_budget(self):
        driver, combo, option = MagicMock(), MagicMock(), MagicMock()
        with patch.object(poster, 'WebDriverWait') as wait, patch.object(poster, '_marketplace_combo_value', return_value=''):
            wait.return_value.until.side_effect = [combo, [(option, 'Blue')], True]
            self.assertEqual(poster._fill_optional_color(driver, 'Interior colour', 'Navy'), 'Blue')
            self.assertTrue(all(call.args[1] <= 25 for call in wait.call_args_list))


if __name__ == '__main__':
    unittest.main()
