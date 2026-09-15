"""Portable configuration round trips use generated, non-production secrets."""
import copy
import json
import tempfile
import threading
import unittest
import uuid
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class ConfigTransferTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.paths = {name: root / (name + '.json') for name in
                      ('requests', 'order', 'sms', 'results', 'snapshot')}
        handler = type('TransferHandler', (server.DashboardHandler,), {
            name + '_path': path for name, path in self.paths.items() if name != 'sms'})
        self.stack.enter_context(patch.object(server, 'SMS_CONFIG_PATH', self.paths['sms']))
        self.execute = self.stack.enter_context(patch.object(server, 'execute_curl'))
        self.authenticate = self.stack.enter_context(patch.object(
            server.DashboardHandler, '_gateway_authenticate', side_effect=AssertionError('auth called')))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.stack.callback(self.httpd.server_close)
        self.stack.callback(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:' + str(self.httpd.server_port)
        secret = uuid.uuid4().hex
        self.accounts = {
            'acc_curl': {'source': 'minimax', 'label': '测试账号',
                         'curl': "curl https://www.minimaxi.com/test -b 'session=" + secret + "'",
                         'phone': 'example', 'username': 'tester', 'password': uuid.uuid4().hex,
                         'accountId': 'example', 'apiKey': uuid.uuid4().hex,
                         'dashboardHidden': True, 'gateway': {'enabled': True, 'maxConcurrency': 2}},
            'acc_google': {'source': 'googleAi', 'curl': '', 'refreshToken': uuid.uuid4().hex,
                           'proxy': '', 'label': 'Google'},
        }
        self.sms = server.load_sms_config()
        for cfg in self.sms.values():
            cfg.update(enabled=True, apiKey=uuid.uuid4().hex)
        server.save_requests(self.paths['requests'], self.accounts)
        server.save_order(self.paths['order'], ['acc_google', 'acc_curl'])
        server.save_sms_config(self.sms)

    def request(self, path, payload=None):
        req = Request(self.base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={} if payload is None else {'Content-Type': 'application/json'})
        try:
            response = urlopen(req)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, json.load(response)

    def test_unauthed_export_import_round_trip_and_merge(self):
        status, headers, backup = self.request('/api/config/export')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(backup['accounts'], self.accounts)
        self.assertEqual(backup['sms'], self.sms)
        # Simulate a separate destination with one unrelated and one stale account.
        unrelated = {'source': 'googleAi', 'refreshToken': uuid.uuid4().hex}
        server.save_requests(self.paths['requests'], {'acc_other': unrelated, 'acc_curl': unrelated})
        server.save_order(self.paths['order'], ['acc_other', 'acc_curl'])
        server.save_sms_config({})
        for name in ('snapshot', 'results'):
            server.save_snapshot(self.paths[name], {'acc_curl': {'old': True}, 'acc_other': {'keep': True}})
        status, _, result = self.request('/api/config/import', backup)
        self.assertEqual(status, 200)
        self.assertEqual(result, {'ok': True, 'added': 1, 'updated': 1, 'smsPlatforms': 2})
        self.assertEqual(server.load_requests(self.paths['requests']), dict(self.accounts, acc_other=unrelated))
        self.assertEqual(server.load_sms_config(), self.sms)
        self.assertEqual(server.load_order(self.paths['order']), ['acc_google', 'acc_curl', 'acc_other'])
        for name in ('snapshot', 'results'):
            self.assertEqual(server.load_snapshot(self.paths[name]), {'acc_other': {'keep': True}})
        self.assertEqual(self.request('/api/config/import', backup)[2]['added'], 0)
        self.execute.assert_not_called()
        self.authenticate.assert_not_called()

    def test_invalid_files_leave_all_settings_unchanged(self):
        backup = self.request('/api/config/export')[2]
        invalid = []
        for key, value in [('version', 2), ('version', True), ('accounts', []), ('sms', []),
                           ('order', ['acc_missing']), ('order', ['acc_curl', 'acc_curl'])]:
            item = copy.deepcopy(backup)
            item[key] = value
            invalid.append(item)
        for key, value in [('curl', 'curl https://unapproved.example/test'), ('label', {}),
                           ('source', 'kimi'), ('gateway', {'maxConcurrency': 11})]:
            item = copy.deepcopy(backup)
            item['accounts']['acc_curl'][key] = value
            invalid.append(item)
        item = copy.deepcopy(backup)
        item['sms']['eomsg']['origin'] = 'file:///example'
        invalid.append(item)
        before = {name: path.read_bytes() for name, path in self.paths.items() if path.exists()}
        for item in invalid:
            with self.subTest(item=invalid.index(item)):
                status, _, response = self.request('/api/config/import', item)
                self.assertEqual(status, 400)
                self.assertNotIn(self.sms['smsnex']['apiKey'], json.dumps(response))
                self.assertEqual({name: path.read_bytes() for name, path in self.paths.items()
                                  if path.exists()}, before)

    def test_write_failure_rolls_back_prior_replacements(self):
        backup = self.request('/api/config/export')[2]
        backup['accounts']['acc_curl']['label'] = 'changed'
        before = {name: path.read_bytes() for name, path in self.paths.items() if path.exists()}
        original = server.os.replace

        def fail_sms(source, target):
            if Path(target) == self.paths['sms']:
                raise OSError('simulated write failure')
            original(source, target)

        with patch.object(server.os, 'replace', side_effect=fail_sms):
            self.assertEqual(self.request('/api/config/import', backup)[0], 500)
        self.assertEqual({name: path.read_bytes() for name, path in self.paths.items()
                          if path.exists()}, before)
        self.assertFalse(list(self.paths['sms'].parent.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main()
