import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from automation import desktop_state, responsiveness
from automation import auto_listing_excel_poster as poster


class ResponsivenessTests(unittest.TestCase):
    def test_twelve_ui_requests_do_not_wait_indefinitely_for_a_worker_lock(self):
        app = Flask(__name__)
        lock = responsiveness.RequestLock('busy-worker', request_timeout=0.02)
        lock.acquire()
        outcomes = []
        def request():
            with app.test_request_context('/'):
                try:
                    with lock:
                        outcomes.append('unexpected acquisition')
                except responsiveness.AppBusyError:
                    outcomes.append('busy')
        threads = [threading.Thread(target=request) for _ in range(12)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(outcomes, ['busy'] * 12)
            self.assertTrue(lock.locked())
        finally:
            lock.release()
            for thread in threads:
                thread.join(timeout=2)

    def test_background_worker_can_wait_until_lock_is_released(self):
        lock = responsiveness.RequestLock('worker')
        lock.acquire()
        finished = threading.Event()
        def worker():
            with lock:
                finished.set()
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertFalse(finished.wait(0.03))
        lock.release()
        thread.join(timeout=2)
        self.assertTrue(finished.is_set())

    def test_nonblocking_acquire_remains_nonblocking(self):
        lock = responsiveness.RequestLock('worker')
        with lock:
            self.assertFalse(lock.acquire(blocking=False))
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_state_disk_write_does_not_hold_page_registry_locks(self):
        routes = SimpleNamespace(scheduled_posts={'one': {'status': 'pending'}}, repost_jobs={},
                                 scheduled_posts_lock=threading.Lock(), repost_jobs_lock=threading.Lock())
        writing, finish = threading.Event(), threading.Event()
        original = Path.write_text
        errors = []
        def write(path, text, **kwargs):
            writing.set()
            if not finish.wait(3):
                raise RuntimeError('Test disk wait expired')
            return original(path, text, **kwargs)
        def save():
            try:
                desktop_state.save_state(routes)
            except Exception as error:
                errors.append(error)
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict('os.environ', FB_AUTOMATION_DESKTOP='1'), \
             patch.object(desktop_state, 'data_dir', return_value=Path(folder)), \
             patch.object(desktop_state, '_last_saved', None), patch.object(Path, 'write_text', write):
            thread = threading.Thread(target=save)
            thread.start()
            try:
                self.assertTrue(writing.wait(2))
                for lock in (routes.scheduled_posts_lock, routes.repost_jobs_lock):
                    acquired = lock.acquire(blocking=False)
                    if acquired:
                        lock.release()
                    self.assertTrue(acquired)
            finally:
                finish.set()
                thread.join(timeout=3)
            self.assertEqual(errors, [])
            self.assertEqual(json.loads((Path(folder) / 'desktop_jobs.json').read_text())['posts']['one']['status'], 'pending')

    def test_slow_request_capture_is_throttled_and_ends_at_teardown(self):
        clock = [0]
        with tempfile.TemporaryDirectory() as folder:
            monitor = responsiveness.RequestMonitor(Path(folder), clock=lambda: clock[0])
            with responsiveness._wait_lock:
                responsiveness._wait_events.clear()
            try:
                key = monitor.begin('GET', '/auto_listing')
                monitor.inspect()
                clock[0] = 11
                monitor.inspect()
                path = Path(folder) / 'freeze-diagnostics.log'
                first = path.read_text(encoding='utf-8')
                self.assertIn('GET /auto_listing age=11.0s', first)
                self.assertIn('Thread ', first)
                monitor.inspect()
                self.assertEqual(path.read_text(encoding='utf-8'), first)
                monitor.end(key)
                clock[0] = 80
                monitor.inspect()
                self.assertEqual(path.read_text(encoding='utf-8'), first)
            finally:
                monitor.close()

    def test_posting_initialization_failure_releases_the_account(self):
        driver, lock = MagicMock(), threading.Lock()
        lock.acquire()
        driver.set_page_load_timeout.side_effect = RuntimeError('Chrome disconnected')
        def close(*args):
            lock.release()
        data = {'vehicle_type': 'Car', 'make': 'Ford', 'model': 'Focus', 'mileage': '5000',
                'price': '1000', 'body_style': 'Sedan', 'image_path1': 'fake.jpg'}
        with patch.object(poster, 'open_posting_tab', return_value=(driver, 'posting', set(), lock)), \
             patch.object(poster, 'close_posting_tab', side_effect=close) as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'Chrome disconnected'):
                poster.post_to_fb_marketplace(data, 'test-account')
        cleanup.assert_called_once()
        self.assertFalse(lock.locked())

    def test_short_ui_lock_timeout_also_captures_worker_stacks(self):
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as folder:
            monitor = responsiveness.RequestMonitor(Path(folder))
            lock = responsiveness.RequestLock('listing-registry', request_timeout=0.01)
            lock.acquire()
            try:
                with app.test_request_context('/auto_listing'), self.assertRaises(responsiveness.AppBusyError):
                    with lock:
                        pass
                monitor.inspect()
                text = (Path(folder) / 'freeze-diagnostics.log').read_text(encoding='utf-8')
                self.assertIn('UI lock wait timed out: listing-registry', text)
                self.assertIn('Thread ', text)
            finally:
                lock.release()
                monitor.close()


if __name__ == '__main__':
    unittest.main()
