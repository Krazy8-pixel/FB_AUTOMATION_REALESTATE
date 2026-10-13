import threading
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch
from types import SimpleNamespace
from automation import task_control


class TaskControlTests(unittest.TestCase):
    def test_close_cancels_schedules_but_keeps_maintenance_jobs(self):
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = [SimpleNamespace(id=value) for value in ['post-one', 'maintenance', 'desktop-state-checkpoint']]
        routes = SimpleNamespace(scheduled_posts={'one': {'status': 'pending'}},
            repost_jobs={'two': {'status': 'posting'}}, scheduled_posts_lock=threading.RLock(),
            repost_jobs_lock=threading.RLock(), scheduler=scheduler, LISTING_AUTO_REFRESH_CHECK_JOB_ID='maintenance')
        with patch('automation.desktop_state.checkpoint'):
            task_control.stop_foreground_work(routes)
        self.assertEqual(routes.scheduled_posts['one']['status'], 'cancelled')
        self.assertEqual(routes.repost_jobs['two']['status'], 'cancelled')
        scheduler.remove_job.assert_called_once_with('post-one')

    def tearDown(self):
        task_control.set_window_open(True)

    def test_closed_window_rejects_posting_but_allows_maintenance(self):
        @task_control.cancellable
        def posting(account):
            return 'posted'
        @task_control.cancellable
        def import_groups_from_facebook(account):
            return 'imported'
        with patch.dict('os.environ', FB_AUTOMATION_DESKTOP='1'):
            task_control.set_window_open(False)
            with self.assertRaisesRegex(RuntimeError, 'window is closed'):
                posting('account')
            self.assertEqual(import_groups_from_facebook('account'), 'imported')
            task_control.set_window_open(True)
            self.assertEqual(posting('account'), 'posted')
    def test_cancel_blocks_browser_actions_but_allows_cleanup(self):
        ready, done = threading.Event(), threading.Event()
        driver = MagicMock()
        failures = []

        @task_control.cancellable
        def work(account):
            guarded = task_control.guard_driver(driver)
            ready.set()
            done.wait(2)
            try:
                guarded.execute('clickElement', {})
            except RuntimeError:
                failures.append('cancelled')
            guarded.execute('quit', {})

        def run():
            try:
                work('account')
            except RuntimeError:
                pass
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(2))
        task = task_control.active_tasks('account')[0]
        self.assertFalse(task_control.cancel(task['id'], 'another-account'))
        self.assertTrue(task_control.cancel(task['id'], 'account'))
        done.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ['cancelled'])
        self.assertEqual(task_control.active_tasks('account'), [])

    def test_nested_work_has_one_task(self):
        @task_control.cancellable
        def child(account):
            return len(task_control.active_tasks(account))

        @task_control.cancellable
        def parent(account, job_id):
            self.assertEqual(task_control.active_tasks(account)[0]['job_id'], job_id)
            return child(account)
        self.assertEqual(parent('account', 'job'), 1)
