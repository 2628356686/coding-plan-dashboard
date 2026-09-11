"""Exercise all gateway protocols over HTTP with a local, credential-free upstream."""
import json
import io
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import server
REAL_SELECT = server.select_gateway_account


class ImageConversionTest(unittest.TestCase):
    def test_anthropic_images_convert_to_openai_parts(self):
        body = {'model': 'm', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': '这是什么'},
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'QUJD'}},
            {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/a.png'}},
        ]}]}
        content = server.anthropic_to_openai(body)['messages'][0]['content']
        self.assertEqual(content[0], {'type': 'text', 'text': '这是什么'})
        self.assertEqual(content[1], {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}})
        self.assertEqual(content[2], {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}})

    def test_anthropic_text_only_content_stays_a_string(self):
        body = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}]}]}
        content = server.anthropic_to_openai(body)['messages'][0]['content']
        self.assertEqual(content, 'ab')

    def test_openai_images_convert_to_anthropic_blocks(self):
        body = {'model': 'm', 'messages': [
            {'role': 'system', 'content': [
                {'type': 'text', 'text': 'sys'},
                {'type': 'image_url', 'image_url': {'url': 'https://example.com/s.png'}}]},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': '看图'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,QUJD'}},
                {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}},
                {'type': 'image_url', 'image_url': {'url': 'ftp://unsupported'}},
            ]}]}
        result = server.openai_to_anthropic_request(body)
        self.assertEqual(result['system'], 'sys')
        content = result['messages'][0]['content']
        self.assertEqual(content[0], {'type': 'text', 'text': '看图'})
        self.assertEqual(content[1], {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'QUJD'}})
        self.assertEqual(content[2], {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/a.png'}})
        self.assertEqual(len(content), 3)

    def test_responses_input_image_survives_conversion(self):
        body = {'model': 'm', 'input': [
            {'role': 'user', 'content': [
                {'type': 'input_text', 'text': '看图'},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,QUJD'},
            ]},
            {'type': 'input_image', 'image_url': 'https://example.com/a.png'},
        ]}
        messages = server.responses_to_openai(body)['messages']
        self.assertEqual(messages[0]['content'][0], {'type': 'text', 'text': '看图'})
        self.assertEqual(messages[0]['content'][1], {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}})
        self.assertEqual(messages[1]['content'][0], {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}})


class GatewayProtocolsTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.requests = []
        self.failure = None
        self.failures = []
        self.stack.enter_context(patch.dict(server._gateway_cooldowns, {}, clear=True))
        self.stack.enter_context(patch.dict(server._gateway_quota_cache, {}, clear=True))
        self.hold_stream = False
        self.hold_response = False
        self.release_stream = threading.Event()
        self.upstream_finished = threading.Event()
        owner = self

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, body))
                protocol = 'anthropic' if self.path.endswith('/messages') else 'openai'
                if protocol == 'anthropic':
                    message = {'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                               'model': 'test-model', 'content': [{'type': 'text', 'text': '你好'}],
                               'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 2}}
                else:
                    message = {'id': 'chat_test', 'model': 'test-model', 'object': 'chat.completion',
                               'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '你好'},
                                            'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 1, 'completion_tokens': 2}}
                data = server.gateway_message_events(message, protocol) if body.get('stream') else json.dumps(message).encode()
                status = 200
                mime = 'text/event-stream' if body.get('stream') else 'application/json'
                if owner.failure:
                    status, data, mime = owner.failure
                if owner.failures:
                    status, data, mime = owner.failures.pop(0)
                if owner.hold_response:
                    owner.release_stream.wait(10)
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if owner.hold_stream and body.get('stream'):
                    marker = b'event: content_block_stop' if protocol == 'anthropic' else b'data: {"id"'
                    split = data.find(marker) if protocol == 'anthropic' else data.rfind(marker)
                    self.wfile.write(data[:split])
                    self.wfile.flush()
                    owner.release_stream.wait(10)
                    self.wfile.write(data[split:])
                else:
                    self.wfile.write(data)
                owner.upstream_finished.set()

        class QuietGateway(server.DashboardHandler):
            def log_message(self, *args):
                pass

        def start(handler):
            httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            self.stack.callback(httpd.server_close)
            self.stack.callback(thread.join, 2)
            self.stack.callback(httpd.shutdown)
            return 'http://127.0.0.1:%s' % httpd.server_port

        self.upstream = start(Upstream)
        self.gateway = start(QuietGateway)
        self.config = {'enabled': True, 'apiKey': 'test-only', 'maxRetries': 1, 'upstreams': {}}
        self.stack.enter_context(patch.object(server, 'load_gateway_config', return_value=self.config))
        self.stack.enter_context(patch.object(server, 'load_requests', return_value={}))
        self.stack.enter_context(patch.object(server, 'load_snapshot', return_value={}))
        self.stack.enter_context(patch.object(server, 'select_gateway_account', return_value=('test', {'apiKey': 'test-only'})))
        self.stack.enter_context(patch.object(server, 'gateway_category_for_account', return_value='test'))
        self.stack.enter_context(patch.object(server, 'record_gateway_request'))

    def call(self, protocol, stream, model='test-model'):
        paths = {'openai': '/v1/chat/completions', 'anthropic': '/v1/messages', 'responses': '/v1/responses'}
        body = {'model': model, 'stream': stream, 'max_tokens': 100,
                'messages': [{'role': 'user', 'content': 'hello'}]}
        if protocol == 'responses':
            body['input'] = 'hello'
        req = Request(self.gateway + paths[protocol], data=json.dumps(body).encode(),
                      headers={'x-api-key': 'test-only', 'Content-Type': 'application/json'})
        return urlopen(req, timeout=5)

    def wait_active(self, count):
        deadline = time.monotonic() + 3
        while True:
            with urlopen(self.gateway + '/api/gateway/active', timeout=3) as response:
                status = json.load(response)
            if status['activeCount'] == count:
                return status
            if time.monotonic() >= deadline:
                self.fail('Expected %s active requests, got %s' % (count, status['activeCount']))
            time.sleep(0.01)

    def test_concurrency_shows_accounts_models_and_clears_on_completion(self):
        self.config['upstreams'] = {'test': {'openaiBaseUrl': self.upstream, 'anthropicBaseUrl': self.upstream}}
        self.hold_response = True
        account_a = ('account-a', {'label': '账号 A', 'apiKey': 'test-only'})
        account_b = ('account-b', {'label': '账号 B', 'apiKey': 'test-only'})

        def request(protocol, stream, model):
            with self.call(protocol, stream, model) as response:
                response.read()

        with patch.object(server, 'select_gateway_account', side_effect=[account_a, account_a, account_b, account_b]):
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending = [pool.submit(request, protocol, stream, 'model-' + str(i))
                           for i, (protocol, stream) in enumerate([
                               ('openai', True), ('anthropic', True), ('responses', True), ('openai', False)])]
                try:
                    status = self.wait_active(4)
                    active = status['activeRequests']
                    self.assertEqual({r['accountLabel'] for r in active}, {'账号 A', '账号 B'})
                    self.assertEqual({r['model'] for r in active}, {'model-' + str(i) for i in range(4)})
                    self.assertEqual(len({r['id'] for r in active}), 4)
                    self.assertEqual(sum(r['stream'] for r in active), 3)
                    self.assertTrue(all(r['elapsedSeconds'] >= 0 for r in active))
                    self.assertNotIn('apiKey', json.dumps(status))
                    self.assertNotIn('test-only', json.dumps(status))
                finally:
                    self.release_stream.set()
                for future in pending:
                    future.result(timeout=5)
        self.wait_active(0)

    def test_three_protocols_streaming_and_json_with_both_upstreams(self):
        for upstream in ('openai', 'anthropic'):
            self.config['upstreams'] = {'test': {upstream + 'BaseUrl': self.upstream}}
            for protocol in ('openai', 'anthropic', 'responses'):
                for stream in (False, True):
                    with self.subTest(upstream=upstream, protocol=protocol, stream=stream):
                        with self.call(protocol, stream) as response:
                            text = response.read().decode()
                            self.assertEqual(response.status, 200)
                            if stream:
                                self.assertIn('text/event-stream', response.headers['Content-Type'])
                                payloads = [json.loads(line[6:]) for line in text.splitlines()
                                            if line.startswith('data: ') and line != 'data: [DONE]']
                                self.assertIn('你好', json.dumps(payloads, ensure_ascii=False))
                                ending = {'openai': 'data: [DONE]', 'anthropic': 'event: message_stop',
                                          'responses': 'event: response.completed'}[protocol]
                                self.assertIn(ending, text)
                            else:
                                result = json.loads(text)
                                content = (result['choices'][0]['message']['content'] if protocol == 'openai'
                                           else result['content'][0]['text'] if protocol == 'anthropic'
                                           else result['output'][0]['content'][0]['text'])
                                self.assertEqual(content, '你好')
                            self.assertEqual(self.requests[-1][1]['stream'], stream and (protocol == upstream or protocol == 'responses'))

    def test_responses_delta_arrives_before_upstream_can_finish(self):
        for upstream in ('openai', 'anthropic'):
            with self.subTest(upstream=upstream):
                self.config['upstreams'] = {'test': {upstream + 'BaseUrl': self.upstream}}
                self.hold_stream = True
                self.release_stream.clear()
                self.upstream_finished.clear()
                try:
                    with self.call('responses', True) as response:
                        events = []
                        for line in response:
                            if not line.startswith(b'data: '):
                                continue
                            event = json.loads(line[6:])
                            events.append(event)
                            if event['type'] == 'response.output_text.delta':
                                self.assertEqual(event['delta'], '你好')
                                self.assertFalse(self.upstream_finished.is_set())
                                break
                        else:
                            self.fail('No delta received before upstream completed')
                        self.release_stream.set()
                        events.extend(json.loads(line[6:]) for line in response if line.startswith(b'data: '))
                        self.assertEqual(events[-1]['type'], 'response.completed')
                        self.assertEqual(events[-1]['response']['output'][0]['content'][0]['text'], '你好')
                        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
                finally:
                    self.release_stream.set()

    def test_image_content_reaches_openai_upstream_from_anthropic_inbound(self):
        self.config['upstreams'] = {'test': {'openaiBaseUrl': self.upstream}}
        body = {'model': 'test-model', 'stream': False, 'max_tokens': 10,
                'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': '这是什么'},
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'QUJD'}},
                ]}]}
        req = Request(self.gateway + '/v1/messages', data=json.dumps(body).encode(),
                      headers={'x-api-key': 'test-only', 'Content-Type': 'application/json'})
        with urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            json.load(response)
        path, upstream_body = self.requests[-1]
        self.assertEqual(path, '/chat/completions')
        content = upstream_body['messages'][0]['content']
        self.assertEqual(content[0], {'type': 'text', 'text': '这是什么'})
        self.assertIn({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,QUJD'}}, content)

    def test_upstream_errors_are_not_successful_empty_messages(self):
        self.config['upstreams'] = {'test': {'openaiBaseUrl': self.upstream}}
        for protocol in ('openai', 'anthropic', 'responses'):
            for stream in (False, True):
                for failure in ((400, b'{"error":"bad request"}', 'application/json'),
                                (200, b'not json', 'text/plain')):
                    with self.subTest(protocol=protocol, stream=stream, failure=failure[0]):
                        self.failure = failure
                        with self.assertRaises(HTTPError) as error:
                            self.call(protocol, stream)
                        self.assertEqual(error.exception.code, 400 if failure[0] == 400 else 502)
                        error.exception.close()
                        self.wait_active(0)

    def test_quota_error_retries_another_account_for_all_protocols(self):
        accounts = {'first': {'apiKey': 'test-only'}, 'second': {'apiKey': 'test-only'}}
        self.config['maxRetries'] = 2
        self.config['upstreams'] = {'test': {'openaiBaseUrl': self.upstream, 'anthropicBaseUrl': self.upstream}}
        # Restore the real selector hidden by the fixture's default mock.
        with patch.object(server, 'load_requests', return_value=accounts), \
             patch.object(server, 'select_gateway_account', REAL_SELECT), \
             patch.object(server.random, 'choices', side_effect=lambda candidates, **kwargs: [candidates[0]]):
            for protocol in ('openai', 'anthropic', 'responses'):
                for stream in (False, True):
                    with self.subTest(protocol=protocol, stream=stream):
                        server._gateway_cooldowns.clear()
                        self.requests.clear()
                        self.failures = [(429, b'{"error":"quota exceeded"}', 'application/json')]
                        with self.call(protocol, stream) as response:
                            text = response.read().decode()
                            self.assertEqual(response.status, 200)
                            self.assertNotIn('quota exceeded', text)
                        self.assertEqual(len(self.requests), 2)
                        self.assertIn('first', server._gateway_cooldowns)
                        self.assertNotIn('second', server._gateway_cooldowns)
                        self.wait_active(0)

    def test_stream_connection_failure_retries_without_leaking_active_requests(self):
        accounts = {'first': {'apiKey': 'test-only'}, 'second': {'apiKey': 'test-only'}}
        self.config['maxRetries'] = 2
        self.config['upstreams'] = {'test': {'openaiBaseUrl': self.upstream, 'anthropicBaseUrl': self.upstream}}
        real_open = server.urlopen
        with patch.object(server, 'load_requests', return_value=accounts), \
             patch.object(server, 'select_gateway_account', REAL_SELECT), \
             patch.object(server.random, 'choices', side_effect=lambda candidates, **kwargs: [candidates[0]]):
            for protocol in ('openai', 'anthropic', 'responses'):
                server._gateway_cooldowns.clear()
                attempts = []
                def open_upstream(*args, **kwargs):
                    attempts.append(1)
                    if len(attempts) == 1:
                        raise URLError('synthetic connection error')
                    return real_open(*args, **kwargs)
                with patch.object(server, 'urlopen', side_effect=open_upstream):
                    with self.call(protocol, True) as response:
                        response.read()
                        self.assertEqual(response.status, 200)
                self.assertEqual(len(attempts), 2)
                self.wait_active(0)

    def test_concurrency_setting_validates_and_preserves_existing_gateway_options(self):
        accounts = {'a': {'source': 'volcAgent', 'curl': 'curl https://console.volcengine.com/',
                          'gateway': {'enabled': False, 'maxConcurrency': 3}}}
        with patch.object(server, 'load_requests', return_value=accounts), \
             patch.object(server, 'infer_source', return_value='volcAgent'), \
             patch.object(server, 'save_requests') as save:
            def update(fields):
                req = Request(self.gateway + '/api/requests',
                              data=json.dumps(dict(id='a', **fields)).encode(),
                              headers={'Content-Type': 'application/json'})
                return urlopen(req, timeout=3)
            for limit in (1, 10):
                with update({'gateway': {'maxConcurrency': limit}}) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(accounts['a']['gateway'], {'enabled': False, 'maxConcurrency': limit})
            with update({'label': 'Updated'}) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(accounts['a']['gateway']['maxConcurrency'], 10)
            for invalid in (0, 11, 1.5, True, '2', None):
                save.reset_mock()
                with self.assertRaises(HTTPError) as error:
                    update({'gateway': {'maxConcurrency': invalid}})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                save.assert_not_called()

    def test_account_gateway_toggle_preserves_account_and_controls_routing(self):
        account = {'source': 'volcAgent', 'label': 'Example', 'accountId': 'provider-account',
                   'apiKey': 'test-only', 'curl': 'synthetic', 'gateway': {'maxConcurrency': 4}}
        accounts = {'a': account}
        with patch.object(server, 'load_requests', return_value=accounts), \
             patch.object(server, 'save_requests') as save:
            self.assertEqual(REAL_SELECT(accounts, {})[0], 'a')
            for enabled in (False, True):
                req = Request(self.gateway + '/api/requests/gateway',
                              data=json.dumps({'id': 'a', 'enabled': enabled}).encode(),
                              headers={'Content-Type': 'application/json'})
                with urlopen(req, timeout=3) as response:
                    result = json.load(response)
                self.assertEqual(result['gateway'], {'enabled': enabled, 'maxConcurrency': 4})
                self.assertEqual(REAL_SELECT(accounts, {})[0], 'a' if enabled else None)
                self.assertEqual(account['apiKey'], 'test-only')
                self.assertEqual(account['curl'], 'synthetic')
                self.assertEqual(account['label'], 'Example')
                self.assertEqual(account['accountId'], 'provider-account')
                self.assertNotIn('test-only', json.dumps(result))
            self.assertEqual(save.call_count, 2)
            for payload, code in (({'id': 'a', 'enabled': 'false'}, 400),
                                  ({'id': 'missing', 'enabled': False}, 404)):
                save.reset_mock()
                req = Request(self.gateway + '/api/requests/gateway', data=json.dumps(payload).encode(),
                              headers={'Content-Type': 'application/json'})
                with self.assertRaises(HTTPError) as error:
                    urlopen(req, timeout=3)
                self.assertEqual(error.exception.code, code)
                error.exception.close()
                save.assert_not_called()

    def test_keyless_account_cannot_enable_gateway(self):
        account = {'source': 'volcAgent', 'label': 'NoKey', 'apiKey': '   '}
        accounts = {'a': account}
        with patch.object(server, 'load_requests', return_value=accounts), \
             patch.object(server, 'save_requests') as save:
            def toggle(enabled):
                req = Request(self.gateway + '/api/requests/gateway',
                              data=json.dumps({'id': 'a', 'enabled': enabled}).encode(),
                              headers={'Content-Type': 'application/json'})
                return urlopen(req, timeout=3)
            with self.assertRaises(HTTPError) as error:
                toggle(True)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            self.assertIsNone(account.get('gateway'))
            save.assert_not_called()
            with toggle(False) as response:
                result = json.load(response)
            self.assertEqual(result['gateway'], {'enabled': False})
            self.assertEqual(save.call_count, 1)


class PortSettingTest(unittest.TestCase):
    def test_normalize_gateway_port(self):
        cases = {None: None, '': None, 0: None, -1: None, 70000: None, 'abc': None,
                 1: 1, 65535: 65535, 8080: 8080, '8080': 8080}
        for value, expected in cases.items():
            self.assertEqual(server.normalize_gateway_port(value), expected, repr(value))
        self.assertIsNone(server.normalize_gateway_port(True))
        self.assertIsNone(server.normalize_gateway_port(False))

    def test_load_gateway_config_defaults_and_sanitizes_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'gateway.json'
            with patch.object(server, 'GATEWAY_CONFIG_PATH', path):
                self.assertIsNone(server.load_gateway_config()['port'])
                path.write_text(json.dumps({'port': 9090, 'apiKey': 'k'}), encoding='utf-8')
                config = server.load_gateway_config()
                self.assertEqual(config['port'], 9090)
                self.assertEqual(config['apiKey'], 'k')
                self.assertEqual(config['maxRetries'], 3)
                path.write_text(json.dumps({'port': 'not-a-port'}), encoding='utf-8')
                self.assertIsNone(server.load_gateway_config()['port'])


class GatewayPortConfigTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        class Quiet(server.DashboardHandler):
            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.old_port = httpd.server_address[1]
        self.base = 'http://127.0.0.1:%d' % self.old_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.stack.callback(httpd.shutdown)
        self.stack.callback(httpd.server_close)
        self.stack.callback(thread.join, 2)
        self.stack.enter_context(patch.dict(server._active_dashboard_server, {'httpd': httpd}, clear=True))
        self.stack.callback(self._stop_switched_server)
        self.stack.enter_context(patch.object(server, 'DashboardHandler', Quiet))
        tmp = tempfile.TemporaryDirectory()
        self.stack.callback(tmp.cleanup)
        self.config_path = pathlib.Path(tmp.name) / 'gateway.json'
        self.base_config = {'enabled': True, 'apiKey': 'k'}
        self.config_path.write_text(json.dumps(self.base_config), encoding='utf-8')
        self.stack.enter_context(patch.object(server, 'GATEWAY_CONFIG_PATH', self.config_path))

    def _stop_switched_server(self):
        httpd = server._active_dashboard_server['httpd']
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    def free_port(self):
        probe = socket.socket()
        probe.bind(('0.0.0.0', 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def occupy_port(self):
        port = self.free_port()
        sock = socket.socket()
        if os.name == 'nt':
            # Windows honours SO_REUSEADDR double binds unless the occupant is exclusive.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(('0.0.0.0', port))
        sock.listen(1)
        self.stack.callback(sock.close)
        return port

    def post_config(self, payload):
        req = Request(self.base + '/api/gateway/config', data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
        return urlopen(req, timeout=5)

    def wait_serving(self, base, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return urlopen(base + '/api/gateway/config', timeout=2)
            except OSError:
                # Windows may reset backlog connections instead of refusing them.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def test_port_change_rebinds_serves_and_persists(self):
        new_port = self.free_port()
        with self.post_config({'port': new_port}) as response:
            result = json.load(response)
        self.assertEqual(result['port'], new_port)
        self.assertEqual(result['effectivePort'], new_port)
        self.assertEqual(json.loads(self.config_path.read_text(encoding='utf-8'))['port'], new_port)
        new_base = 'http://127.0.0.1:%d' % new_port
        with self.wait_serving(new_base) as response:
            self.assertEqual(json.load(response)['effectivePort'], new_port)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                urlopen(self.base + '/api/gateway/config', timeout=1).close()
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail('old port %d is still serving' % self.old_port)

    def test_port_change_to_occupied_port_is_rejected(self):
        occupied = self.occupy_port()
        with self.assertRaises(HTTPError) as error:
            self.post_config({'port': occupied})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(json.loads(self.config_path.read_text(encoding='utf-8')), self.base_config)
        with self.wait_serving(self.base) as response:
            self.assertEqual(json.load(response)['effectivePort'], self.old_port)

    def test_invalid_port_is_rejected_without_persisting(self):
        for value in (70000, 'abc', True, -1):
            with self.assertRaises(HTTPError) as error:
                self.post_config({'port': value})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertEqual(json.loads(self.config_path.read_text(encoding='utf-8')), self.base_config)

    def test_null_port_rebinds_to_env_port(self):
        env_port = self.free_port()
        with patch.dict(os.environ, {'PORT': str(env_port)}):
            with self.post_config({'port': None}) as response:
                result = json.load(response)
        self.assertIsNone(result['port'])
        self.assertEqual(result['effectivePort'], env_port)
        self.assertIsNone(json.loads(self.config_path.read_text(encoding='utf-8'))['port'])
        env_base = 'http://127.0.0.1:%d' % env_port
        with self.wait_serving(env_base) as response:
            self.assertEqual(json.load(response)['effectivePort'], env_port)


class ResponsesStreamTest(unittest.TestCase):
    def convert(self, data, protocol='openai'):
        return [json.loads(event.decode().split('data: ', 1)[1])
                for event in server.gateway_responses_stream(io.BytesIO(data), protocol, 'test-model')]

    def test_multiline_sse_heartbeats_usage_and_text_fragments(self):
        data = b': heartbeat\r\n\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"Hello "}}]}\r\n\r\n'
        data += b'data: {"choices":[{"delta":{"content":"world"},"finish_reason":"stop"}]}\n\n'
        data += b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
        data += b'data: [DONE]\n\n'
        events = self.convert(data)
        self.assertEqual([e['delta'] for e in events if e['type'] == 'response.output_text.delta'], ['Hello ', 'world'])
        self.assertEqual(events[-1]['response']['usage']['total_tokens'], 5)
        self.assertEqual(events[-1]['response']['output'][0]['content'][0]['text'], 'Hello world')

    def test_broken_streams_fail_instead_of_completing(self):
        prefix = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        for tail in (b'', b'data: invalid\n\n', b'data: {"error":{"message":"secret upstream detail"}}\n\n'):
            events = self.convert(prefix + tail)
            self.assertEqual(events[-1]['type'], 'response.failed')
            self.assertNotIn('response.completed', [e['type'] for e in events])
            self.assertNotIn('secret upstream detail', json.dumps(events))

    def test_token_limit_is_incomplete(self):
        events = self.convert(b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n')
        self.assertEqual(events[-1]['type'], 'response.incomplete')
        self.assertEqual(events[-1]['response']['incomplete_details']['reason'], 'max_output_tokens')


class ActiveRequestLifecycleTest(unittest.TestCase):
    def test_exception_removes_active_request(self):
        with self.assertRaises(RuntimeError):
            with server.track_gateway_request('account', {'label': 'Example'}, 'model', 'responses', True):
                self.assertEqual(server.gateway_active_snapshot()['activeCount'], 1)
                raise RuntimeError('connection ended')
        self.assertEqual(server.gateway_active_snapshot()['activeCount'], 0)


class RoutingTest(unittest.TestCase):
    def test_default_limit_and_capacity_recovery(self):
        accounts = {'a': {'apiKey': 'test-only'}}
        self.assertEqual(server.gateway_concurrency_limit(accounts['a']), 1)
        with server.reserve_gateway_account(accounts, {}, 'model', 'responses', True) as first:
            self.assertEqual(first[0], 'a')
            with server.reserve_gateway_account(accounts, {}, 'model', 'responses', True) as second:
                self.assertIsNone(second[0])
        self.assertEqual(server.select_gateway_account(accounts, {})[0], 'a')

    def test_weight_uses_quota_times_available_slots(self):
        accounts = {'a': {'apiKey': 'test-only', 'gateway': {'maxConcurrency': 5}},
                    'b': {'apiKey': 'test-only', 'gateway': {'maxConcurrency': 10}}}
        snapshot = {'a': {'usedPercent': 20}, 'b': {'usedPercent': 60}}
        with server.track_gateway_request('a', {}, 'model', 'openai', True):
            with server.track_gateway_request('a', {}, 'model', 'openai', True):
                with patch.object(server.random, 'choices', return_value=['b']) as choose:
                    server.select_gateway_account(accounts, snapshot)
                    self.assertAlmostEqual(choose.call_args.kwargs['weights'][0], 0.8 * 3)
                    self.assertEqual(choose.call_args.kwargs['weights'][1], 0.4 * 10)

    def test_simultaneous_reservations_never_exceed_limit(self):
        accounts = {'a': {'apiKey': 'test-only', 'gateway': {'maxConcurrency': 3}}}
        release = threading.Event()
        barrier = threading.Barrier(12)
        def reserve():
            with server.reserve_gateway_account(accounts, {}, 'model', 'openai', True) as selected:
                barrier.wait(timeout=3)
                release.wait(3)
                return selected[0]
        with ThreadPoolExecutor(max_workers=12) as pool:
            pending = [pool.submit(reserve) for _ in range(12)]
            try:
                deadline = time.monotonic() + 3
                while server.gateway_active_snapshot()['activeCount'] < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(server.gateway_active_snapshot()['activeCount'], 3)
            finally:
                release.set()
            self.assertEqual(sum(f.result(timeout=5) == 'a' for f in pending), 3)

    def test_reservation_is_visible_to_next_selection_and_released(self):
        accounts = {'a': {'apiKey': 'test-only', 'gateway': {'maxConcurrency': 2}}}
        with patch.dict(server._gateway_quota_cache, {}, clear=True), \
             patch.object(server.random, 'choices', return_value=['a']) as choose:
            with server.reserve_gateway_account(accounts, {}, 'model', 'openai', True):
                with server.reserve_gateway_account(accounts, {}, 'model', 'openai', True):
                    self.assertEqual(choose.call_args_list[0].kwargs['weights'], [1.0])
                    self.assertEqual(choose.call_args_list[1].kwargs['weights'], [0.5])
                    self.assertEqual(server.gateway_active_snapshot()['activeCount'], 2)
            self.assertEqual(server.gateway_active_snapshot()['activeCount'], 0)

    def test_disabled_cooling_and_missing_keys_are_excluded(self):
        accounts = {'disabled': {'apiKey': 'test-only', 'gateway': {'enabled': False}},
                    'cooling': {'apiKey': 'test-only'}, 'no-key': {}}
        with patch.dict(server._gateway_cooldowns, {'cooling': time.time() + 60}, clear=True):
            self.assertEqual(server.select_gateway_account(accounts, {}), (None, None))

    def test_exhausted_accounts_are_skipped_and_concurrency_reduces_weight(self):
        accounts = {key: {'apiKey': 'test-only'} for key in ('empty', 'busy', 'idle')}
        accounts['busy']['gateway'] = {'maxConcurrency': 2}
        snapshot = {'empty': {'usedPercent': 100}, 'busy': {'usedPercent': 20}, 'idle': {'usedPercent': 60}}
        with server.track_gateway_request('busy', {}, 'model', 'openai', True):
            with patch.object(server.random, 'choices', return_value=['idle']) as choose:
                self.assertEqual(server.select_gateway_account(accounts, snapshot)[0], 'idle')
                self.assertEqual(choose.call_args.args[0], ['busy', 'idle'])
                self.assertEqual(choose.call_args.kwargs['weights'], [0.8, 0.4])
        self.assertEqual(server.select_gateway_account({'empty': accounts['empty']}, snapshot), (None, None))

    def test_server_quota_refresh_changes_routing_and_keeps_last_good_data(self):
        accounts = {'a': {'source': 'volcAgent', 'apiKey': 'test-only', 'curl': 'synthetic'}}
        with patch.dict(server._gateway_quota_cache, {}, clear=True):
            with patch.object(server, 'execute_curl', return_value=(200, json.dumps({'Result': {
                'AFPFiveHour': {'Quota': 100, 'Used': 20}, 'AFPWeekly': {'Quota': 100, 'Used': 100}}}), '')):
                server.refresh_gateway_quotas(accounts)
            snapshot = server.gateway_routing_snapshot({'a': {'usedPercent': 0}})
            self.assertEqual(server.account_remaining_percent('a', snapshot), 0)
            self.assertEqual(server.select_gateway_account(accounts, snapshot), (None, None))
            with patch.object(server, 'execute_curl', return_value=(403, '{}', '')):
                server.refresh_gateway_quotas(accounts)
            self.assertEqual(server.account_remaining_percent('a', server.gateway_routing_snapshot({})), 0)
            with patch.object(server, 'execute_curl', return_value=(200, '{"Result":{"QuotaUsage":[{"Percent":"10%"}]}}', '')):
                server.refresh_gateway_quotas(accounts)
            self.assertEqual(server.account_remaining_percent('a', server.gateway_routing_snapshot({})), 90)


class OpenLoginTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        class Quiet(server.DashboardHandler):
            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.base = 'http://127.0.0.1:%d' % httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.stack.callback(httpd.server_close)
        self.stack.callback(thread.join, 2)
        self.stack.callback(httpd.shutdown)

    def test_open_login_launches_browser_with_fixed_url(self):
        with patch.object(server, 'find_private_browser', return_value=['chrome', '--incognito']), \
             patch.object(server.subprocess, 'Popen') as popen:
            with urlopen(Request(self.base + '/api/open-login', method='POST'), timeout=3) as response:
                result = json.load(response)
            self.assertTrue(result['ok'])
            self.assertEqual(result['browser'], 'chrome')
            popen.assert_called_once()
            self.assertEqual(popen.call_args[0][0],
                             ['chrome', '--incognito', server.VOLC_LOGIN_URL])

    def test_open_login_without_browser_returns_503(self):
        with patch.object(server, 'find_private_browser', return_value=None):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.base + '/api/open-login', method='POST'), timeout=3)
            self.assertEqual(error.exception.code, 503)
            error.exception.close()

    def test_find_private_browser_returns_flag_pair_when_available(self):
        found = server.find_private_browser()
        if found is not None:
            self.assertEqual(len(found), 2)
            self.assertIn(found[1], ('--incognito', '--inprivate', '-private-window'))


if __name__ == '__main__':
    unittest.main()
