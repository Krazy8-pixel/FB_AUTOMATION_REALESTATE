import threading
import unittest
from unittest.mock import MagicMock
from automation import task_control


class TaskControlTests(unittest.TestCase):
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
