import io
import threading
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch
from collections import deque

from automation import terminal_log


class TerminalLogTests(unittest.TestCase):
    def test_report_includes_freeze_thread_captures(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch('automation.runtime_paths.data_dir', return_value=Path(folder)), \
             patch.object(terminal_log, '_disk_handler', None):
            (Path(folder) / 'freeze-diagnostics.log').write_text('[FREEZE] UI lock wait timed out: posting registry', encoding='utf-8')
            self.assertIn('[FREEZE] UI lock wait timed out: posting registry', terminal_log.export_text())

    def setUp(self):
        for name, value in [('_chunks', deque()), ('_sequence', 0), ('_size', 0)]:
            item = patch.object(terminal_log, name, value)
            item.start()
            self.addCleanup(item.stop)

    def test_file_history_survives_reopening_and_rotates(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(terminal_log, '_disk_handler', None):
            terminal_log.start_disk_capture(folder)
            terminal_log._disk_handler.maxBytes = 60
            try:
                terminal_log._record('first session import complete\n')
                terminal_log._disk_handler.close()
                terminal_log._disk_handler = None
                terminal_log.start_disk_capture(folder)
                terminal_log._disk_handler.maxBytes = 60
                terminal_log._record('second session post confirmed\n')
                terminal_log._record('third event cross post failed\n')
                report = terminal_log.export_text()
                self.assertIn('first session import complete', report)
                self.assertIn('second session post confirmed', report)
                self.assertIn('third event cross post failed', report)
                self.assertTrue(Path(folder, 'diagnostics.log.1').exists())
            finally:
                terminal_log._disk_handler.close()

    def test_disk_failure_keeps_live_output_working(self):
        handler = MagicMock()
        handler.handle.side_effect = OSError('disk full')
        with patch.object(terminal_log, '_disk_handler', handler):
            terminal_log._record('import continues\n')
        self.assertIn('import continues', terminal_log.snapshot()['text'])

    def test_mirrors_output_and_preserves_partial_lines(self):
        original = io.StringIO()
        stream = terminal_log.TerminalStream(original)
        stream.write('progress ')
        cursor = terminal_log.snapshot()['cursor']
        stream.write('done\nerror\n')
        self.assertEqual(original.getvalue(), 'progress done\nerror\n')
        self.assertEqual(terminal_log.snapshot(cursor)['text'], 'done\nerror\n')

    def test_slow_viewer_recovers_after_buffer_eviction(self):
        with patch.object(terminal_log, 'MAX_CHARS', 10):
            terminal_log._record('first write')
            terminal_log._record('last write')
        result = terminal_log.snapshot()
        self.assertTrue(result['reset'])
        self.assertEqual(result['text'], 'last write')

    def test_concurrent_writes_keep_unique_ordered_cursors(self):
        threads = [threading.Thread(target=terminal_log._record, args=('line\n',)) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(terminal_log.snapshot()['text'], 'line\n' * 30)
        self.assertEqual(terminal_log.snapshot()['cursor'], 30)

    def test_console_free_mode(self):
        stream = terminal_log.TerminalStream(None)
        self.assertEqual(stream.write('message'), 7)
        stream.flush()
        self.assertEqual(terminal_log.snapshot()['text'], 'message')
