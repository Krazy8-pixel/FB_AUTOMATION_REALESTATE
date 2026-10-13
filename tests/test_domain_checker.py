"""Offline regression checks for access failures and resumable domain batches."""
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, call, patch


SPEC = importlib.util.spec_from_file_location(
    'standalone_domain_checker', Path(__file__).resolve().parents[1] / 'automation' / 'bbc.py'
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class FakePlaywrightError(Exception):
    pass


class DomainCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.pending = Path(self.temporary.name) / 'pending.json'
        self.workbook_path = Path(self.temporary.name) / 'domains.xlsx'
        self.context = MagicMock()
        self.output = patch('sys.stdout', new=io.StringIO())
        self.output.start()
        self.addCleanup(self.output.stop)
        api = types.ModuleType('playwright.sync_api')
        api.Error = FakePlaywrightError
        self.modules = patch.dict(sys.modules, {'playwright.sync_api': api})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.sleep_patch = patch.object(checker.time, 'sleep')
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.capture_patch = patch.object(checker, 'capture_result_screenshot', return_value=None)
        self.capture_patch.start()
        self.addCleanup(self.capture_patch.stop)

    def page(self, title='Domain search', body='Find your perfect domain'):
        page = MagicMock()
        page.is_closed.return_value = False
        page.title.return_value = title
        page.locator.return_value.inner_text.return_value = body
        return page

    def test_http_access_failure_is_not_domain_data(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), self.assertRaises(checker.SiteBlocked):
                checker.assert_site_access(self.page(), types.SimpleNamespace(status=status))

    def test_http_server_failure_is_unavailable(self):
        with self.assertRaises(checker.SiteUnavailable):
            checker.assert_site_access(self.page(), types.SimpleNamespace(status=503))

    def test_access_denied_and_human_verification_are_detected(self):
        cases = [
            ('Access Denied', 'Reference #123'),
            ('Domain search', "You don't have permission to access this server."),
            ('Domain search', 'Please verify that you are human'),
        ]
        for title, body in cases:
            with self.subTest(title=title, body=body), self.assertRaises(checker.SiteBlocked):
                checker.assert_site_access(self.page(title, body))

    def test_domain_name_is_not_mistaken_for_denial(self):
        checker.assert_site_access(self.page(body='access-denied.com\nGREAT NAME\n$10'))

    def test_single_domain_starts_after_one_prompt_submission(self):
        terminal = types.ModuleType('prompt_toolkit')
        terminal.prompt = MagicMock(return_value='ChatGPT.COM')
        with patch.dict(sys.modules, {'prompt_toolkit': terminal}), \
                patch('builtins.input', side_effect=AssertionError('Must not wait for a second input')):
            domains = checker.read_domains()
        self.assertEqual(domains, ['chatgpt.com'])
        terminal.prompt.assert_called_once_with('Domains > ', multiline=False)

    def test_hundred_pasted_lines_start_after_one_prompt_submission(self):
        expected = [f'domain{index}.com' for index in range(100)]
        terminal = types.ModuleType('prompt_toolkit')
        terminal.prompt = MagicMock(return_value='\n'.join(expected))
        with patch.dict(sys.modules, {'prompt_toolkit': terminal}), \
                patch('builtins.input', side_effect=AssertionError('Must not wait for a blank line')):
            domains = checker.read_domains()
        self.assertEqual(domains, expected)
        self.assertEqual(len(domains), 100)
        terminal.prompt.assert_called_once_with('Domains > ', multiline=False)

    def test_blocked_search_stops_batch_and_keeps_unprocessed_input(self):
        domains = ['first.com', 'second.com', 'third.com']
        with patch.object(checker, 'check_domain', side_effect=[('Taken', None), checker.SiteBlocked('Denied')]) as check, \
                patch.object(checker, 'append_result') as append, patch.object(checker.time, 'sleep'):
            completed = checker.run_batch(self.context, domains, None, None, self.workbook_path, self.pending, 5)
        self.assertFalse(completed)
        self.assertEqual(check.call_count, 2)
        append.assert_called_once_with(None, None, self.workbook_path, 'first.com', 'Taken', None, None)
        self.assertEqual(checker.load_pending(self.pending), ['second.com', 'third.com'])

    def test_resume_checks_only_pending_domains_and_clears_completed_queue(self):
        checker.save_pending(self.pending, ['second.com', 'third.com'])
        with patch('builtins.input', return_value=''):
            domains = checker.read_or_resume_domains(self.pending)
        with patch.object(checker, 'check_domain', return_value=('Taken', None)) as check, \
                patch.object(checker, 'append_result') as append, patch.object(checker.time, 'sleep'):
            completed = checker.run_batch(self.context, domains, None, None, self.workbook_path, self.pending, 5)
        self.assertTrue(completed)
        self.assertEqual([call.args[1] for call in check.call_args_list], ['second.com', 'third.com'])
        self.assertEqual(append.call_count, 2)
        self.assertFalse(self.pending.exists())

    def test_declining_resume_retains_queue(self):
        checker.save_pending(self.pending, ['second.com'])
        with patch('builtins.input', return_value='n'):
            self.assertEqual(checker.read_or_resume_domains(self.pending), [])
        self.assertEqual(checker.load_pending(self.pending), ['second.com'])

    def test_excel_save_failure_does_not_drop_domain_from_pending_queue(self):
        domains = ['first.com', 'second.com']
        with patch.object(checker, 'check_domain', return_value=('Taken', None)), \
                patch.object(checker, 'append_result', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                checker.run_batch(self.context, domains, None, None, self.workbook_path, self.pending, 5)
        self.assertEqual(checker.load_pending(self.pending), domains)

    def test_unrecognized_accessible_search_can_still_write_no_data(self):
        with patch.object(checker, 'check_domain', return_value=('No Data', None)), \
                patch.object(checker, 'append_result') as append:
            completed = checker.run_batch(self.context, ['example.com'], None, None, self.workbook_path, self.pending, 5)
        self.assertTrue(completed)
        append.assert_called_once_with(None, None, self.workbook_path, 'example.com', 'No Data', None, None)

    def test_block_on_navigation_does_not_wait_for_missing_search_box(self):
        page = self.page()
        page.goto.return_value = types.SimpleNamespace(status=403)
        context = MagicMock()
        context.new_page.return_value = page
        with self.assertRaises(checker.SiteBlocked):
            checker.check_domain(context, 'example.com', 5)
        page.locator.assert_not_called()
        page.close.assert_called_once()

    def test_duplicate_domain_refreshes_before_reading_a_result(self):
        page = self.page()

        def read_after_refresh(current_page, domain, timeout):
            page.reload.assert_called_once_with(wait_until='domcontentloaded', timeout=45000)
            self.assertIs(current_page, page)
            self.assertEqual(domain, 'example.com')
            return 'Taken', None

        with patch.object(checker, 'assert_site_access') as access, \
                patch.object(checker, 'wait_result', side_effect=read_after_refresh):
            result = checker.check_domain(
                self.context, 'example.com', 5, page=page, previous_domain='example.com'
            )
        self.assertEqual(result, ('Taken', None))
        access.assert_called_once_with(page, page.reload.return_value)
        page.goto.assert_not_called()
        page.locator.assert_not_called()
        page.close.assert_not_called()
        self.context.new_page.assert_not_called()

    def test_search_page_closes_without_extra_sleep_for_all_outcomes(self):
        for outcome in [('Taken', None), None, checker.SiteBlocked('Denied'), FakePlaywrightError('Failed')]:
            with self.subTest(outcome=outcome):
                page = self.page()
                self.context.new_page.return_value = page
                self.sleep.reset_mock()

                with patch.object(checker, 'assert_site_access'), \
                        patch.object(checker, 'wait_result') as wait:
                    if isinstance(outcome, Exception):
                        wait.side_effect = outcome
                        with self.assertRaises(checker.SiteUnavailable):
                            checker.check_domain(self.context, 'example.com', 5)
                    else:
                        wait.return_value = outcome
                        checker.check_domain(self.context, 'example.com', 5)
                self.sleep.assert_not_called()
                page.close.assert_called_once_with()

    def test_batch_reuses_one_page_and_waits_ten_seconds_between_domains(self):
        domains = ['first.com', 'second.com', 'third.com']
        with patch.object(checker, 'check_domain', return_value=('Taken', None)) as check, \
                patch.object(checker, 'append_result'), patch.object(checker.time, 'sleep') as sleep:
            completed = checker.run_batch(
                self.context, domains, None, None, self.workbook_path, self.pending, 5
            )
        self.assertTrue(completed)
        self.context.new_page.assert_called_once_with()
        page = self.context.new_page.return_value
        self.assertEqual(check.call_args_list, [
            call(self.context, 'first.com', 5, page=page, previous_domain=None),
            call(self.context, 'second.com', 5, page=page, previous_domain='first.com'),
            call(self.context, 'third.com', 5, page=page, previous_domain='second.com'),
        ])
        self.assertEqual(sleep.call_args_list, [call(10), call(10)])
        page.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
