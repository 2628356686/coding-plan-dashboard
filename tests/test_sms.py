"""Exercise the SMS verification platform proxy with local, credential-free upstreams."""
import json
import pathlib
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import Request, urlopen

import server


def upstream_json(handler, payload, status=200):
    body = json.dumps(payload).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def upstream_text(handler, text, status=200):
    body = text.encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'text/plain; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class SmsProxyTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.smsnex_requests = []
        self.eomsg_requests = []
        owner = self

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.route('GET')

            def do_POST(self):
                self.route('POST')

            def route(self, method):
                parsed = urlparse(self.path)
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                length = int(self.headers.get('Content-Length', '0') or 0)
                raw = self.rfile.read(length) if length else b''
                if parsed.path.startswith('/openapi/v1'):
                    owner.smsnex_requests.append((method, parsed.path, query,
                                                  self.headers.get('Authorization'), raw))
                    if method == 'GET' and parsed.path == '/openapi/v1/me':
                        upstream_json(self, {'code': 0,
                                             'data': {'user_name': 'tester', 'balance': 12.5}})
                    elif method == 'GET' and parsed.path == '/openapi/v1/projects':
                        upstream_json(self, {'code': 0, 'data': {
                            'list': [{'id': '1', 'name': query.get('name'),
                                      'platform': query.get('platform')}], 'total': 1}})
                    elif method == 'GET' and parsed.path == '/openapi/v1/projects/demo/channels':
                        assert self.headers.get('Authorization') == 'Bearer secret-test'
                        upstream_json(self, {'code': 0, 'data': {'list': [
                            {'uid': 'b', 'price': 2.0, 'available_number': 3},
                            {'uid': 'a', 'price': 1.0, 'available_number': 5}]}})
                    elif method == 'POST' and parsed.path == '/openapi/v1/phone/rent':
                        body = json.loads(raw)
                        upstream_json(self, {'code': 0, 'data': {
                            'id': 2 if body.get('phone') == '123' else 1,
                            'phone': '16700001111', 'project_name': 'Demo',
                            'expires_at': 12345}})
                    elif method == 'GET' and parsed.path.endswith('/code'):
                        if parsed.path.split('/')[-2] == '8':
                            upstream_json(self, {'code': 0, 'data': {
                                'status': 'pending', 'code': None, 'sms': None}})
                        else:
                            upstream_json(self, {'code': 0, 'data': {
                                'status': 'success', 'code': '654321',
                                'sms': '【智谱AI】验证码 654321'}})
                    elif method == 'POST' and parsed.path.endswith('/release'):
                        upstream_json(self, {'code': 0, 'data': {'ok': True}})
                    else:
                        upstream_json(self, {'code': 1, 'message': 'not found'}, status=404)
                    return
                owner.eomsg_requests.append((method, parsed.path, query))
                code = query.get('code')
                if code == 'leftAmount':
                    upstream_text(self, '12.50')
                elif code == 'getPhone':
                    upstream_text(self, '16700001111')
                elif code == 'getMsg':
                    upstream_text(self, '尚未收到短信，请稍候'
                                  if query.get('phone') == '19999990000'
                                  else '【智谱AI】验证码 654321')
                elif code == 'release':
                    upstream_text(self, 'success')
                else:
                    upstream_text(self, 'ERR:unknown')

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream_origin = 'http://127.0.0.1:%d' % httpd.server_port
        upstream_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        upstream_thread.start()
        self.stack.callback(httpd.server_close)
        self.stack.callback(httpd.shutdown)

        class Quiet(server.DashboardHandler):
            def log_message(self, *args):
                pass

        httpd2 = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.base = 'http://127.0.0.1:%d' % httpd2.server_port
        thread = threading.Thread(target=httpd2.serve_forever, daemon=True)
        thread.start()
        self.stack.callback(httpd2.server_close)
        self.stack.callback(thread.join, 2)
        self.stack.callback(httpd2.shutdown)

        tmp = tempfile.TemporaryDirectory()
        self.stack.callback(tmp.cleanup)
        self.config_path = pathlib.Path(tmp.name) / 'sms.json'
        self.config_path.write_text(json.dumps({
            'smsnex': {'enabled': True, 'origin': self.upstream_origin,
                       'apiKey': 'secret-test', 'pollIntervalMs': 2100, 'maxWaitMs': 100},
            'eomsg': {'enabled': True, 'origin': self.upstream_origin + '/zc/data.php',
                      'apiKey': 'eomsg-token', 'pollIntervalMs': 2100, 'maxWaitMs': 100},
        }, ensure_ascii=False), encoding='utf-8')
        self.stack.enter_context(patch.object(server, 'SMS_CONFIG_PATH', self.config_path))
        self.stack.enter_context(patch.object(server, '_sms_code_gate', {'next': 0.0}))

    def call(self, path, method='GET', payload=None):
        request = Request(self.base + path, method=method,
                          data=json.dumps(payload, ensure_ascii=False).encode('utf-8')
                          if payload is not None else None,
                          headers={'Content-Type': 'application/json'}
                          if payload is not None else {})
        return urlopen(request, timeout=10)

    def reset_gate(self):
        server._sms_code_gate['next'] = 0.0

    def test_platform_listing_masks_keys_and_rejects_keyless_enable(self):
        with self.call('/api/sms/platform') as response:
            platforms = json.load(response)['platforms']
        self.assertEqual([p['id'] for p in platforms], ['smsnex', 'eomsg'])
        self.assertTrue(all(p['configured'] and p['enabled'] for p in platforms))
        self.assertTrue(all(p['keyPreview'].endswith('…') for p in platforms))
        dump = json.dumps(platforms, ensure_ascii=False)
        self.assertNotIn('secret-test', dump)
        self.assertNotIn('eomsg-token', dump)
        with self.assertRaises(HTTPError) as error:
            self.call('/api/sms/platform', 'POST',
                      {'provider': 'eomsg', 'enabled': True, 'clearApiKey': True})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        stored = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(stored['eomsg']['apiKey'], 'eomsg-token')
        self.assertTrue(stored['eomsg']['enabled'])
        self.call('/api/sms/platform', 'POST',
                  {'provider': 'smsnex', 'enabled': True,
                   'origin': self.upstream_origin + '/', 'apiKey': 'brand-new-key'}).close()
        stored = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(stored['smsnex']['apiKey'], 'brand-new-key')
        self.assertEqual(stored['smsnex']['origin'], self.upstream_origin)

    def test_smsnex_projects_channels_rent_normalize_sort_and_auth(self):
        with self.call('/api/sms/account?provider=smsnex') as response:
            account = json.load(response)
        self.assertEqual(account, {'userName': 'tester', 'balance': 12.5})
        with self.call('/api/sms/projects?provider=smsnex&platform=domestic&page=1&'
                       + urlencode({'name': 'Telegram'})) as response:
            projects = json.load(response)
        self.assertEqual(projects['list'][0]['name'], 'Telegram')
        with self.call('/api/sms/channels?provider=smsnex&projectId=demo&platform=domestic') as response:
            channels = json.load(response)
        self.assertEqual(channels['list'][0]['uid'], 'a')
        self.assertEqual(channels['list'][0]['availableNumber'], 5)
        with self.call('/api/sms/rent?provider=smsnex', 'POST',
                       {'projectId': 'demo', 'channelUid': 'a', 'platform': 'domestic',
                        'phone': '123'}) as response:
            phone = json.load(response)
        self.assertEqual(phone['id'], 2)
        self.assertEqual(phone['projectName'], 'Demo')
        self.assertEqual(phone['expiresAt'], 12345)
        method, path, _, auth, raw = self.smsnex_requests[-1]
        self.assertEqual((method, path, auth), ('POST', '/openapi/v1/phone/rent',
                                                'Bearer secret-test'))
        self.assertEqual(json.loads(raw)['channel_uid'], 'a')

    def test_smsnex_code_poll_timeout_and_release(self):
        self.reset_gate()
        with self.call('/api/sms/code/7?provider=smsnex') as response:
            result = json.load(response)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['code'], '654321')
        self.reset_gate()
        with self.call('/api/sms/poll/8?provider=smsnex') as response:
            self.assertEqual(json.load(response)['status'], 'timeout')
        self.call('/api/sms/release/7?provider=smsnex', 'POST').close()
        method, path, _, _, _ = self.smsnex_requests[-1]
        self.assertEqual((method, path), ('POST', '/openapi/v1/phone/7/release'))
        with self.assertRaises(HTTPError) as error:
            self.call('/api/sms/rent?provider=smsnex', 'POST', {'projectId': 'demo'})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_eomsg_flow_maps_token_protocol(self):
        with self.call('/api/sms/account?provider=eomsg') as response:
            self.assertEqual(json.load(response), {'balance': '12.50', 'username': 'EOMSG'})
        with self.call('/api/sms/rent?provider=eomsg', 'POST',
                       {'projectId': '智谱AI', 'cardType': '实卡'}) as response:
            phone = json.load(response)
        self.assertEqual(phone['phone'], '16700001111')
        self.assertEqual(phone['status'], 'pending')
        method, path, query = self.eomsg_requests[-1]
        self.assertEqual(method, 'GET')
        self.assertEqual(query['code'], 'getPhone')
        self.assertEqual(query['token'], 'eomsg-token')
        self.assertEqual(query['keyWord'], '智谱AI')
        self.assertEqual(query['cardType'], '实卡')
        with self.call('/api/sms/code/19999990000?provider=eomsg&keyWord='
                       + urlencode({'keyWord': '智谱AI'})) as response:
            self.assertEqual(json.load(response)['status'], 'pending')
        with self.call('/api/sms/code/16700001111?provider=eomsg&keyWord='
                       + urlencode({'keyWord': '智谱AI'})) as response:
            result = json.load(response)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['code'], '654321')
        with self.call('/api/sms/release/16700001111?provider=eomsg', 'POST') as response:
            self.assertEqual(json.load(response), {'ok': True, 'result': 'success'})
        with self.assertRaises(HTTPError) as error:
            self.call('/api/sms/code/16700001111?provider=eomsg')
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_eomsg_poll_skips_previous_sms_until_deadline(self):
        previous = urlencode({'previousSms': '【智谱AI】验证码 654321'})
        with self.call('/api/sms/poll/16700001111?provider=eomsg&keyWord='
                       + urlencode({'keyWord': '智谱AI'}) + '&' + previous) as response:
            self.assertEqual(json.load(response)['status'], 'timeout')

    def test_unconfigured_provider_is_rejected(self):
        self.config_path.write_text(json.dumps({
            'smsnex': {'enabled': False, 'origin': self.upstream_origin,
                       'apiKey': 'secret-test'},
            'eomsg': {'enabled': True, 'origin': self.upstream_origin, 'apiKey': ''},
        }), encoding='utf-8')
        for provider in ('smsnex', 'eomsg'):
            with self.assertRaises(HTTPError) as error:
                self.call('/api/sms/account?provider=' + provider)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()


class SmsHelperTest(unittest.TestCase):
    def test_verification_code_extracts_longest_4_to_8_digit_run(self):
        extract = server.sms_verification_code
        self.assertEqual(extract('【智谱AI】验证码 654321'), '654321')
        self.assertEqual(extract('Your code 1234567 keep it safe'), '1234567')
        self.assertEqual(extract('12 998877665544 1234'), '1234')
        self.assertIsNone(extract('no digits here'))
        self.assertIsNone(extract('123 1234567890'))

    def test_normalize_converts_snake_case_keys_recursively(self):
        value = server.sms_normalize({'user_name': 'a', 'a_list': [
            {'available_number': 3, 'nested_deep': {'max_price': 1.5}}]})
        self.assertEqual(value, {'userName': 'a', 'aList': [
            {'availableNumber': 3, 'nestedDeep': {'maxPrice': 1.5}}]})


if __name__ == '__main__':
    unittest.main()
