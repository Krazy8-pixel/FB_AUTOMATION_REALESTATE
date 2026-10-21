import unittest
from unittest.mock import MagicMock, patch
from selenium.common.exceptions import TimeoutException
from automation import posting_recovery as recovery
from automation.auto_listing_excel_poster import _normalize_marketplace_vehicle_settings
from automation.reposting import REPOST_REQUIRED_FIELDS, validate_repost_fields


class PostingRecoveryTests(unittest.TestCase):
    def button(self, active):
        button = MagicMock()
        button.is_enabled.return_value = active
        button.get_attribute.side_effect = lambda key: ('false' if button.is_enabled() else 'true') if key == 'aria-disabled' else None
        return button

    def test_condition_is_optional_and_has_no_invented_default(self):
        self.assertNotIn('vehicle_condition', REPOST_REQUIRED_FIELDS)
        post = {key: 'value' for key in REPOST_REQUIRED_FIELDS}
        post['mileage'] = '1000'
        validate_repost_fields(post)
        self.assertEqual(_normalize_marketplace_vehicle_settings({})['vehicle_condition'], '')
        self.assertEqual(_normalize_marketplace_vehicle_settings({'vehicle_condition': 'Good'})['vehicle_condition'], 'Good')

    def test_normal_button_rendering_does_not_repair_fields(self):
        driver, repair = MagicMock(), MagicMock()
        button = self.button(True)
        with patch.object(recovery, 'action_button', return_value=button), \
             patch.object(recovery, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = lambda condition: condition(driver)
            self.assertIs(recovery.wait_for_action(driver, 'Publish', repair), button)
        repair.assert_not_called()
        self.assertEqual(wait.call_args.args[1], 25)

    def test_disabled_rendered_button_repairs_once_after_normal_wait(self):
        driver, button = MagicMock(), self.button(False)
        driver.execute_script.return_value = True
        def repair():
            button.is_enabled.return_value = True
            return 1
        repair_mock = MagicMock(side_effect=repair)
        calls = 0
        def until(condition):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutException('initial rendering wait elapsed')
            return condition(driver)
        with patch.object(recovery, 'action_button', return_value=button), \
             patch.object(recovery, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = until
            self.assertIs(recovery.wait_for_action(driver, 'Publish', repair_mock), button)
        repair_mock.assert_called_once()
        button.click.assert_not_called()
        self.assertTrue(all(call.args[1] == 25 for call in wait.call_args_list))

    def test_loading_or_missing_button_does_not_trigger_repair(self):
        repair = MagicMock()
        with patch.object(recovery, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = TimeoutException('still rendering')
            with self.assertRaises(TimeoutException):
                recovery.wait_for_action(MagicMock(), 'Publish', repair)
        repair.assert_not_called()

    def test_aria_disabled_div_is_not_clickable(self):
        button = self.button(True)
        button.get_attribute.side_effect = lambda key: 'true' if key == 'aria-disabled' else None
        self.assertFalse(recovery.enabled(button))

    def test_only_empty_supplied_fields_restored(self):
        snapshot = {'year': {'value': '2020', 'state': 'read'},
                    'make': {'value': '', 'state': 'empty'},
                    'model': {'value': '', 'state': 'unavailable'},
                    'vehicle_condition': {'value': '', 'state': 'empty'}}
        def fill(key, label, value):
            snapshot[key] = {'value': value, 'state': 'read'}
        fill_mock = MagicMock(side_effect=fill)
        with patch('automation.reposting.read_edit_form_snapshot', return_value=snapshot), \
             patch.object(recovery, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = lambda condition: condition(MagicMock())
            count = recovery.restore_empty_fields(MagicMock(),
                {'year': '2020', 'make': 'Honda', 'model': 'City', 'vehicle_condition': ''}, fill_mock)
        self.assertEqual(count, 1)
        fill_mock.assert_called_once_with('make', 'Make', 'Honda')

    def test_disabled_button_without_cleared_fields_is_never_forced(self):
        button, driver, repair = self.button(False), MagicMock(), MagicMock(return_value=0)
        driver.execute_script.return_value = True
        with patch.object(recovery, 'action_button', return_value=button), \
             patch.object(recovery, 'WebDriverWait') as wait:
            wait.return_value.until.side_effect = [TimeoutException(), button]
            with self.assertRaises(TimeoutException):
                recovery.wait_for_action(driver, 'Next', repair)
        button.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
