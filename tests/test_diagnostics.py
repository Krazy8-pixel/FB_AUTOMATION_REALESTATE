import io
import unittest
from contextlib import redirect_stdout

from automation.diagnostics import traced


class DiagnosticTests(unittest.TestCase):
    def test_failed_action_is_recorded_and_exception_preserved(self):
        @traced('IMPORT')
        def import_listings(account):
            raise ValueError('missing field')

        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, 'missing field'):
                import_listings('test-account')
        self.assertIn('IMPORT START', output.getvalue())
        self.assertIn('IMPORT STOPPED', output.getvalue())
        self.assertIn('ValueError', output.getvalue())

    def test_false_post_result_is_not_reported_as_success(self):
        @traced('POST')
        def post(data, account):
            return False

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertFalse(post({}, 'target'))
        self.assertIn('POST RETURNED_FALSE', output.getvalue())
        self.assertIn('"account": "target"', output.getvalue())
