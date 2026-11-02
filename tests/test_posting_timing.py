import unittest
from automation.posting_timing import request_origin, complete_posting, duration_fields


class PostingTimingTests(unittest.TestCase):
    def test_click_time_includes_upload_queue_and_execution(self):
        meta = {'posting_requested_at': request_origin(1000000, 1005), 'status': 'posted'}
        complete_posting(meta, True, 1123.5)
        self.assertEqual(duration_fields(meta), {
            'posting_duration_seconds': 123.5, 'posting_duration_text': '2m 3s'})

    def test_invalid_client_time_uses_request_arrival(self):
        for value in (None, '', 'nan', 'inf', True, -1, 9000000):
            self.assertEqual(request_origin(value, 1005), 1005)

    def test_failure_clears_duration_and_legacy_success_stays_empty(self):
        meta = {'posting_requested_at': 1000, 'status': 'posted'}
        complete_posting(meta, True, 1010)
        self.assertEqual(duration_fields(meta, 'failed'), {})
        complete_posting(meta, False, 1011)
        self.assertEqual(duration_fields(meta), {})
        complete_posting({}, True, 1010)
        self.assertEqual(duration_fields({'status': 'posted'}), {})


if __name__ == '__main__':
    unittest.main()
