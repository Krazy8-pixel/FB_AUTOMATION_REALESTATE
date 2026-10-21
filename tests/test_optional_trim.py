import unittest
from unittest.mock import MagicMock, patch

from automation import auto_listing_excel_poster as poster


class OptionalTrimTests(unittest.TestCase):
    def setUp(self):
        self.driver = MagicMock()
        self.driver.find_elements.return_value = []
        self.model = MagicMock()
        self.model.send_keys.side_effect = lambda value: setattr(self.model.get_attribute, 'return_value', value)

    def test_empty_trim_does_not_inspect_form(self):
        self.assertEqual(poster._fill_optional_trim(self.driver, self.model, 'Range Rover Sport', ''), 'Range Rover Sport')
        self.driver.find_elements.assert_not_called()
        self.model.clear.assert_not_called()

    def test_missing_trim_keeps_model_without_waiting_or_menu_retries(self):
        with patch.object(poster, '_marketplace_combo', return_value=False), \
             patch.object(poster, 'WebDriverWait') as wait, \
             patch.object(poster, '_select_marketplace_combo') as select:
            result = poster._fill_optional_trim(self.driver, self.model, 'Range Rover Sport', 'Supercharged')
        self.assertEqual(result, 'Range Rover Sport')
        self.model.clear.assert_not_called()
        self.model.send_keys.assert_not_called()
        wait.assert_not_called()
        select.assert_not_called()

    def test_existing_trim_in_model_is_not_duplicated(self):
        original = 'Range Rover Sport · Supercharged Limited Edition Sport Utility 4D'
        with patch.object(poster, '_marketplace_combo', return_value=False):
            result = poster._fill_optional_trim(self.driver, self.model, original, 'supercharged limited edition sport utility 4d')
        self.assertEqual(result, original)
        self.model.clear.assert_not_called()

    def test_visible_text_field_is_filled_without_changing_model(self):
        trim = MagicMock()
        trim.get_attribute.return_value = 'Supercharged'
        self.driver.find_elements.return_value = [trim]
        with patch.object(poster, '_marketplace_combo', return_value=False):
            result = poster._fill_optional_trim(self.driver, self.model, 'Range Rover Sport', 'Supercharged')
        trim.send_keys.assert_called_once_with('Supercharged')
        self.assertEqual(result, 'Range Rover Sport')
        self.model.clear.assert_not_called()

    def test_short_trim_is_also_skipped_when_field_is_missing(self):
        with patch.object(poster, '_marketplace_combo', return_value=False):
            result = poster._fill_optional_trim(self.driver, self.model, 'Discovery Diesel', 'SE')
        self.assertEqual(result, 'Discovery Diesel')
        self.model.send_keys.assert_not_called()

    def test_visible_dropdown_is_selected_once(self):
        combo, option = MagicMock(), MagicMock()
        with patch.object(poster, '_marketplace_combo', return_value=combo), \
             patch.object(poster, '_marketplace_combo_value', return_value=''), \
             patch.object(poster, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = [[(option, 'Supercharged')], True]
            result = poster._fill_optional_trim(self.driver, self.model, 'Range Rover Sport', 'Supercharged')
        self.assertEqual(result, 'Range Rover Sport')
        self.assertEqual(self.driver.execute_script.call_count, 2)
        self.model.clear.assert_not_called()
        self.assertTrue(all(call.args[1] == 25 for call in wait.call_args_list))

    def test_unusable_dropdown_falls_back_to_model_and_closes_menu(self):
        combo = MagicMock()
        with patch.object(poster, '_marketplace_combo', return_value=combo), \
             patch.object(poster, '_marketplace_combo_value', return_value=''), \
             patch.object(poster, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = TimeoutError('No choices')
            result = poster._fill_optional_trim(self.driver, self.model, 'Range Rover Sport', 'Supercharged')
        self.assertEqual(result, 'Range Rover Sport')
        self.model.send_keys.assert_not_called()
        self.assertEqual(wait.return_value.until.call_count, 1)
        combo.send_keys.assert_called_once_with(poster.Keys.ESCAPE)

    def test_cancellation_is_not_swallowed_as_optional_failure(self):
        with patch('automation.task_control.check_cancelled', side_effect=RuntimeError('Cancelled by user')):
            with self.assertRaisesRegex(RuntimeError, 'Cancelled by user'):
                poster._fill_optional_trim(self.driver, self.model, 'Range Rover Sport', 'Supercharged')
        self.model.send_keys.assert_not_called()


if __name__ == '__main__':
    unittest.main()
