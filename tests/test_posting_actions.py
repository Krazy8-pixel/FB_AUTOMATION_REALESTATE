import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import ElementClickInterceptedException, TimeoutException

from automation import posting_recovery as recovery


class ImmediateWait:
    def __init__(self, driver, timeout, **kwargs):
        self.driver = driver

    def until(self, condition):
        result = condition(self.driver)
        if not result:
            raise TimeoutException('Simulated button has not become ready')
        return result


class PostingActionTests(unittest.TestCase):
    def button(self, enabled=True):
        button = MagicMock()
        button.is_enabled.return_value = enabled
        button.get_attribute.side_effect = lambda key: ('false' if enabled else 'true') if key == 'aria-disabled' else None
        return button

    def test_form_is_verified_after_readiness_wait_and_before_click(self):
        driver, button, order = MagicMock(), self.button(), []
        button.click.side_effect = lambda: order.append('click')

        def wait(*args, **kwargs):
            order.append('wait')
            return button

        def verify():
            order.append('verify')
            return True

        with patch.object(recovery, 'wait_for_action', side_effect=wait), \
             patch.object(recovery, 'action_button', return_value=button):
            self.assertTrue(recovery.click_action(driver, 'Publish', MagicMock(), verify=verify,
                                                  log=MagicMock()))
        self.assertEqual(order, ['wait', 'verify', 'click'])
        driver.execute_script.assert_not_called()

    def test_failed_form_review_sends_no_click(self):
        driver, button, log = MagicMock(), self.button(), MagicMock()
        with patch.object(recovery, 'wait_for_action', return_value=button), \
             patch.object(recovery, 'action_button', return_value=button):
            with self.assertRaisesRegex(RuntimeError, 'Form review failed; Publish was not clicked'):
                recovery.click_action(driver, 'Publish', MagicMock(), verify=lambda: False, log=log)
        button.click.assert_not_called()
        driver.execute_script.assert_not_called()
        self.assertTrue(any(call.args[:3] == ('Publish', 'verify form before click', 'FAILED')
                            for call in log.call_args_list))

    def test_native_failure_uses_fresh_button_for_javascript_fallback(self):
        driver, first, fresh, log = MagicMock(), self.button(), self.button(), MagicMock()
        error = ElementClickInterceptedException('overlay intercepted native click')
        first.click.side_effect = error
        verify = MagicMock(return_value=True)
        with patch.object(recovery, 'wait_for_action', return_value=first), \
             patch.object(recovery, 'action_button', side_effect=[first, fresh]):
            self.assertTrue(recovery.click_action(driver, 'Publish', MagicMock(), verify=verify, log=log))
        first.click.assert_called_once()
        fresh.click.assert_not_called()
        driver.execute_script.assert_called_once_with('arguments[0].click();', fresh)
        self.assertEqual(verify.call_count, 2)
        failed = [call for call in log.call_args_list
                  if call.args[:3] == ('Publish', 'native click', 'FAILED')]
        self.assertEqual(len(failed), 1)
        self.assertIs(failed[0].kwargs['error'], error)
        self.assertTrue(any(call.args[:3] == ('Publish', 'javascript click fallback', 'SUCCESS')
                            for call in log.call_args_list))

    def test_direct_publish_page_skips_absent_next(self):
        driver, publish, verify = MagicMock(), self.button(), MagicMock()
        with patch.object(recovery, 'WebDriverWait', ImmediateWait), \
             patch.object(recovery, 'action_button', side_effect=lambda d, label: publish if label == 'Publish' else None):
            self.assertFalse(recovery.click_action(driver, 'Next', MagicMock(), verify=verify,
                                                   alternative='Publish', log=MagicMock()))
        verify.assert_not_called()
        publish.click.assert_not_called()
        driver.execute_script.assert_not_called()

    def test_disabled_publish_is_never_forced(self):
        driver, button, repair = MagicMock(), self.button(False), MagicMock(return_value=0)
        driver.execute_script.return_value = True  # Page idle; the button itself is still disabled.
        with patch.object(recovery, 'WebDriverWait', ImmediateWait), \
             patch.object(recovery, 'action_button', return_value=button):
            with self.assertRaises(TimeoutException):
                recovery.click_action(driver, 'Publish', repair, verify=lambda: True, log=MagicMock())
        button.click.assert_not_called()
        self.assertFalse(any(call.args and call.args[0] == 'arguments[0].click();'
                             for call in driver.execute_script.call_args_list))
        repair.assert_called_once()

    def test_form_is_reverified_if_button_waits_again_after_review(self):
        driver, initial, disabled, fresh = MagicMock(), self.button(), self.button(False), self.button()
        order = []

        def wait(*args, **kwargs):
            order.append('wait')
            return initial if order.count('wait') == 1 else fresh

        def verify():
            order.append('verify')
            return True

        fresh.click.side_effect = lambda: order.append('click')
        with patch.object(recovery, 'wait_for_action', side_effect=wait), \
             patch.object(recovery, 'action_button', side_effect=[disabled, fresh]):
            self.assertTrue(recovery.click_action(driver, 'Publish', MagicMock(), verify=verify,
                                                  log=MagicMock()))
        self.assertEqual(order, ['wait', 'verify', 'wait', 'verify', 'click'])
        initial.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
