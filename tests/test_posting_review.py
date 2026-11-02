import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import TimeoutException

from automation import posting_review as review


class ImmediateWait:
    """Exercise the readiness predicate without waiting on a mocked browser."""

    def __init__(self, driver, timeout, **kwargs):
        self.driver = driver

    def until(self, condition):
        result = condition(self.driver)
        if not result:
            raise TimeoutException('Simulated form value has not been retained')
        return result


class PostingReviewTests(unittest.TestCase):
    def setUp(self):
        self.driver = object()
        self.labels = {'model': 'Model', 'mileage': 'Mileage', 'price': 'Price',
                       'description': 'Description', 'exterior_color': 'Exterior color',
                       'interior_color': 'Interior color'}
        self.snapshot = {}
        self.log = MagicMock()
        self.events = MagicMock()
        self.verified = set()

    def read(self, driver):
        self.assertIs(driver, self.driver)
        return copy.deepcopy(self.snapshot)

    def run_review(self, supplied, refill, **kwargs):
        reader = kwargs.pop('snapshot_reader', self.read)
        with patch.object(review, 'WebDriverWait', ImmediateWait), redirect_stdout(io.StringIO()):
            return review.review_posting_fields(
                self.driver, supplied, refill, stage=kwargs.pop('stage', 'before Publish'),
                labels=self.labels, verified_fields=self.verified,
                log=self.log, event=self.events, account='test-account',
                snapshot_reader=reader, settle_for=0, **kwargs)

    def test_numeric_formatting_matches_and_colors_are_optional(self):
        self.snapshot = {'mileage': {'value': '123,456', 'state': 'read'},
                         'price': {'value': '$15,500.00', 'state': 'read'},
                         'exterior_color': {'value': '', 'state': 'empty'},
                         'interior_color': {'value': 'Red', 'state': 'read'}}
        refill = MagicMock()
        result = self.run_review({'mileage': '123456', 'price': '15500',
                                  'exterior_color': 'Blue', 'interior_color': 'Black'}, refill)
        self.assertTrue(result.ok)
        refill.assert_not_called()
        self.assertTrue({'mileage', 'price'}.issubset(self.verified))
        self.assertTrue(any(call.args[:3] == ('Exterior color', 'review: before Publish', 'SKIPPED')
                            for call in self.log.call_args_list))

    def test_mismatched_model_is_refilled_with_requested_value(self):
        self.snapshot = {'model': {'value': 'Civic', 'state': 'read'}}

        def refill(key, label, expected):
            self.snapshot[key] = {'value': expected, 'state': 'read'}

        filler = MagicMock(side_effect=refill)
        result = self.run_review({'model': 'Accord'}, filler)
        self.assertTrue(result.ok)
        filler.assert_called_once_with('model', 'Model', 'Accord')
        self.assertEqual(self.snapshot['model']['value'], 'Accord')

    def test_description_cleared_after_first_snapshot_is_restored(self):
        self.snapshot = {'description': {'value': 'Original supplied description.', 'state': 'read'}}
        reads = 0

        def read(driver):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.snapshot['description'] = {'value': '', 'state': 'empty'}
            return self.read(driver)

        def refill(key, label, expected):
            self.snapshot[key] = {'value': expected, 'state': 'read'}

        filler = MagicMock(side_effect=refill)
        result = self.run_review({'description': 'Original supplied description.'}, filler,
                                 snapshot_reader=read)
        self.assertTrue(result.ok)
        filler.assert_called_once_with('description', 'Description', 'Original supplied description.')
        self.assertIn('description', self.verified)
        self.assertTrue(any('value changed during the render check' in call.kwargs.get('detail', '')
                            for call in self.log.call_args_list))

    def test_never_verified_absent_description_blocks_publish(self):
        self.snapshot = {'description': {'value': '', 'state': 'unavailable'}}
        filler = MagicMock(return_value=False)
        result = self.run_review({'description': 'The supplied listing text'}, filler,
                                 visible_only=True)
        self.assertFalse(result.ok)
        self.assertEqual(filler.call_count, 2)
        self.assertIn('Description', result.message)
        self.assertIn('field state=unavailable', result.message)
        self.assertNotIn('description', self.verified)
        self.assertEqual(self.events.call_args.args[:2], ('POST_REVIEW', 'FAILED'))
        self.assertEqual(self.events.call_args.kwargs['fields'], ['description'])

    def test_already_verified_description_may_be_hidden_after_next(self):
        self.snapshot = {'description': {'value': '', 'state': 'unavailable'}}
        self.verified.add('description')
        filler = MagicMock()
        result = self.run_review({'description': 'The supplied listing text'}, filler,
                                 stage='after Next', visible_only=True)
        self.assertTrue(result.ok)
        filler.assert_not_called()
        self.assertTrue(any('verified before Next' in call.kwargs.get('detail', '')
                            for call in self.log.call_args_list))

    def test_unrecoverable_model_reports_field_expected_actual_and_state(self):
        self.snapshot = {'model': {'value': 'Wrong model', 'state': 'read'}}
        filler = MagicMock(return_value=False)
        result = self.run_review({'model': 'Requested model'}, filler)
        self.assertFalse(result.ok)
        self.assertIn("Model: expected='Requested model'; actual='Wrong model'; field state=read",
                      result.message)
        self.assertIn('before Publish form review failed after refill', result.message)
        self.assertEqual(filler.call_count, 2)
        self.assertEqual(self.events.call_args.kwargs['fields'], ['model'])

    def test_correct_dirham_price_is_not_retyped_during_review(self):
        self.snapshot = {'price': {'value': '567,000 \u062f.\u0645.', 'state': 'read'}}
        filler = MagicMock()
        result = self.run_review({'price': 'MAD567,000'}, filler)
        self.assertTrue(result.ok, result.message)
        filler.assert_not_called()

    def test_numeric_zero_is_a_supplied_value_and_is_reviewed(self):
        self.snapshot = {'price': {'value': 0, 'state': 'read'}}
        filler = MagicMock()
        result = self.run_review({'price': 0}, filler)
        self.assertTrue(result.ok, result.message)
        self.assertIn('price', self.verified)
        filler.assert_not_called()

    def test_wrong_visible_value_is_retried_without_waiting_for_timeout(self):
        self.snapshot = {'model': {'value': 'Wrong', 'state': 'read'}}
        filler = MagicMock(return_value=True)
        with patch.object(review, 'WebDriverWait', side_effect=AssertionError('Must not wait on a known wrong value')):
            result = review.review_posting_fields(
                self.driver, {'model': 'Requested'}, filler, stage='before Publish',
                labels=self.labels, snapshot_reader=self.read, log=self.log, settle_for=0)
        self.assertFalse(result.ok)
        self.assertEqual(filler.call_count, 2)

    def test_parent_repair_also_fixes_a_newly_cleared_child_in_same_pass(self):
        self.labels['make'] = 'Make'
        self.snapshot = {'make': {'value': 'Wrong make', 'state': 'read'},
                         'model': {'value': 'Civic', 'state': 'read'}}

        def refill(key, label, expected):
            self.snapshot[key] = {'value': expected, 'state': 'read'}
            if key == 'make':
                self.snapshot['model'] = {'value': '', 'state': 'empty'}

        filler = MagicMock(side_effect=refill)
        result = self.run_review({'make': 'Honda', 'model': 'Civic'}, filler)
        self.assertTrue(result.ok, result.message)
        self.assertEqual([call.args[0] for call in filler.call_args_list], ['make', 'model'])
        self.assertTrue(any('refill attempt 1/2' in call.kwargs.get('detail', '')
                            and call.args[0] == 'Model' for call in self.log.call_args_list))


if __name__ == '__main__':
    unittest.main()
