"""Persistent logging: tail helper, /api/logs endpoint and gateway 503 diagnostics."""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import server


def _start(handler):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, 'http://127.0.0.1:%s' % httpd.server_port


class QuietHandler(server.DashboardHandler):
    def log_message(self, *args):
        pass


class TailLogTest(unittest.TestCase):
    def test_tail_returns_last_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dashboard.log'
            path.write_text('\n'.join('line%d' % i for i in range(10)) + '\n', encoding='utf-8')
            self.assertEqual(server.tail_log_file(path, 3), 'line7\nline8\nline9')
            self.assertEqual(server.tail_log_file(path, 100).splitlines()[0], 'line0')

    def test_tail_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(server.tail_log_file(Path(tmp) / 'absent.log', 5), '')


class LogsEndpointTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.log_path = Path(self.tmp) / 'dashboard.log'
        self.log_path.write_text('hello-log-line\nsecond-line\n', encoding='utf-8')
        self.stack.enter_context(patch.object(server, 'LOG_PATH', self.log_path))
        self.httpd, self.base = _start(QuietHandler)
        # ExitStack unwinds LIFO: shutdown must run before server_close.
        self.stack.callback(self.httpd.server_close)
        self.stack.callback(self.httpd.shutdown)

    def test_tail_via_api(self):
        with urlopen(self.base + '/api/logs?lines=1', timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read().decode('utf-8'), 'second-line')

    def test_download_serves_full_file(self):
        with urlopen(self.base + '/api/logs?download=1', timeout=5) as response:
            self.assertEqual(response.headers.get('Content-Disposition'),
                             'attachment; filename="dashboard.log"')
            self.assertIn('hello-log-line', response.read().decode('utf-8'))

    def test_missing_log_file_returns_empty_200(self):
        self.log_path.unlink()
        with urlopen(self.base + '/api/logs', timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read().decode('utf-8'), '')


class Gateway503LogTest(unittest.TestCase):
    def test_unavailable_log_lists_account_reasons_without_leaking_keys(self):
        accounts = {
            'acc-a': {'apiKey': 'secret-key-value', 'gateway': {'enabled': True}},
            'acc-b': {'apiKey': 'secret-key-value', 'gateway': {'enabled': False}},
        }
        handler = object.__new__(server.DashboardHandler)
        with self.assertLogs(server.log, level='WARNING') as captured:
            handler.log_gateway_unavailable(accounts, {}, 'upstream boom')
        joined = '\n'.join(captured.output)
        self.assertIn('gw 503 no available accounts', joined)
        self.assertIn('acc-a: available', joined)
        self.assertIn('acc-b: gateway disabled', joined)
        self.assertIn('upstream boom', joined)
        self.assertNotIn('secret-key-value', joined)

    def test_cooldown_logs_reason(self):
        self.addCleanup(server._gateway_cooldowns.pop, 'acc-log-test', None)
        with self.assertLogs(server.log, level='WARNING') as captured:
            server.cooldown_account('acc-log-test', 30, 'test reason')
        self.assertIn('acc-log-test cooldown 30s: test reason', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
