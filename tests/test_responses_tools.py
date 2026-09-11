"""Responses tool lifecycle tests using synthetic messages and screenshots only."""
import io
import json
import unittest

import server


TOOLS = [{'type': 'function', 'name': 'screenshot', 'description': 'Capture an app',
          'parameters': {'type': 'object', 'properties': {'app': {'type': 'string'}},
                         'required': ['app']}, 'strict': False}]


def frame(payload):
    return ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode()


def chat_delta(calls=None, text=None, finish=None):
    delta = {}
    if calls is not None:
        delta['tool_calls'] = calls
    if text is not None:
        delta['content'] = text
    return frame({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})


def events(data, context=None, protocol='openai'):
    return [json.loads(e.decode().split('data: ', 1)[1]) for e in
            server.gateway_responses_stream(io.BytesIO(data), protocol, 'test-model', context)]


class ResponsesToolsTest(unittest.TestCase):
    def test_client_tool_search_loads_tools_for_followup(self):
        search = {'type': 'tool_search', 'execution': 'client', 'description': 'Find tools',
                  'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}
        context = {}
        converted = server.responses_to_openai({'tools': [search], 'input': 'Find screenshot'}, context)
        name = converted['tools'][0]['function']['name']
        data = chat_delta([{'index': 0, 'id': 'search_a', 'function': {'name': name, 'arguments': '{"query":"screenshot"}'}}])
        output = events(data + b'data: [DONE]\n\n', context)[-1]['response']['output']
        self.assertEqual(output[0]['type'], 'tool_search_call')
        self.assertEqual(output[0]['execution'], 'client')
        self.assertEqual(output[0]['arguments'], {'query': 'screenshot'})
        followup = {'tools': [search], 'input': output + [
            {'type': 'tool_search_output', 'execution': 'client', 'call_id': 'search_a', 'tools': TOOLS}]}
        result = server.responses_to_openai(followup)
        self.assertEqual([t['function']['name'] for t in result['tools']], [name, 'screenshot'])
        self.assertEqual(result['messages'][1]['tool_call_id'], 'search_a')
        self.assertIn('screenshot', result['messages'][1]['content'])

    def test_tools_choice_and_parallel_results_with_screenshot_roundtrip(self):
        body = {'tools': TOOLS, 'tool_choice': {'type': 'function', 'name': 'screenshot'},
                'parallel_tool_calls': False, 'input': [
                    {'role': 'user', 'content': 'Inspect two apps'},
                    {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Inspecting'}]},
                    {'type': 'function_call', 'call_id': 'a', 'name': 'screenshot', 'arguments': '{"app":"a"}'},
                    {'type': 'function_call', 'call_id': 'b', 'name': 'screenshot', 'arguments': '{"app":"b"}'},
                    {'type': 'function_call_output', 'call_id': 'a', 'output': [
                        {'type': 'input_text', 'text': 'App a'},
                        {'type': 'input_image', 'image_url': 'data:image/png;base64,QUJD', 'detail': 'original'}]},
                    {'type': 'function_call_output', 'call_id': 'b', 'output': 'App b'},
                    {'role': 'user', 'content': 'Continue'}]}
        original = json.dumps(body)
        result = server.responses_to_openai(body)
        self.assertEqual(result['tools'][0]['function']['parameters'], TOOLS[0]['parameters'])
        self.assertEqual(result['tool_choice'], {'type': 'function', 'function': {'name': 'screenshot'}})
        self.assertFalse(result['parallel_tool_calls'])
        self.assertEqual([m['role'] for m in result['messages']], ['user', 'assistant', 'tool', 'tool', 'user', 'user'])
        self.assertEqual(len(result['messages'][1]['tool_calls']), 2)
        self.assertEqual(result['messages'][2]['content'], 'App a')
        self.assertEqual(result['messages'][4]['content'][0]['image_url']['detail'], 'original')
        self.assertEqual(json.dumps(body), original)

    def test_mcp_json_wrapper_image_output_is_not_stringified(self):
        result = server.responses_to_openai({'input': [
            {'type': 'function_call', 'call_id': 'a', 'name': 'screenshot', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'a', 'output': json.dumps({'content': [
                {'type': 'text', 'text': 'Window'},
                {'type': 'image', 'mimeType': 'image/png', 'data': 'QUJD'}]})}]})
        self.assertEqual(result['messages'][1]['content'], 'Window')
        self.assertEqual(result['messages'][2]['content'][0]['image_url']['url'], 'data:image/png;base64,QUJD')

    def test_custom_namespace_definitions_calls_and_results(self):
        context = {}
        tools = [{'type': 'namespace', 'name': 'functions', 'tools': [
            {'type': 'custom', 'name': 'exec', 'description': 'Execute code', 'format': {'type': 'text'}}]},
            {'type': 'namespace', 'name': 'another', 'tools': [
                {'type': 'function', 'name': 'exec', 'parameters': {'type': 'object'}}]}]
        converted = server.responses_to_openai({'tools': tools, 'input': 'Run code'}, context)
        alias = converted['tools'][0]['function']['name']
        self.assertNotEqual(alias, converted['tools'][1]['function']['name'])
        raw = 'const x = "你好";\nprint(x);'
        response = server.openai_to_responses({'choices': [{'message': {'tool_calls': [
            {'id': 'a', 'function': {'name': alias, 'arguments': json.dumps({'input': raw})}}]}}]}, 'm', context)
        item = response['output'][0]
        self.assertEqual((item['type'], item['name'], item['namespace'], item['input']),
                         ('custom_tool_call', 'exec', 'functions', raw))
        replay = server.responses_to_openai({'tools': tools, 'input': [item,
            {'type': 'custom_tool_call_output', 'call_id': 'a', 'output': 'ok'}]})
        self.assertEqual(replay['messages'][0]['tool_calls'][0]['function']['name'], alias)
        self.assertEqual(json.loads(replay['messages'][0]['tool_calls'][0]['function']['arguments']), {'input': raw})

    def test_rejects_unsupported_features_and_broken_history_explicitly(self):
        for body in ({'tools': [{'type': 'web_search'}]}, {'previous_response_id': 'old'},
                     {'input': [{'type': 'function_call_output', 'call_id': 'missing', 'output': 'ok'}]},
                     {'input': [{'type': 'function_call', 'call_id': 'a', 'name': 'x', 'arguments': '{}'}]}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                server.responses_to_openai(body)

    def test_input_image_order_preserved(self):
        parts = [{'type': 'input_text', 'text': 'before'},
                 {'type': 'input_image', 'image_url': 'https://example.com/a.png'},
                 {'type': 'input_text', 'text': 'after'}]
        result = server.responses_to_openai({'input': [{'role': 'user', 'content': parts}]})
        self.assertEqual([p['type'] for p in result['messages'][0]['content']], ['text', 'image_url', 'text'])

    def test_nonstream_token_limit_does_not_complete_partial_tool(self):
        response = server.openai_to_responses({'choices': [{'finish_reason': 'length', 'message': {
            'tool_calls': [{'id': 'a', 'function': {'name': 'screenshot', 'arguments': '{'}}]}}]}, 'm')
        self.assertEqual(response['status'], 'incomplete')
        self.assertEqual(response['output'], [])

    def test_anthropic_fallback_preserves_tools_history_and_images(self):
        chat = server.responses_to_openai({'tools': TOOLS, 'tool_choice': 'required', 'input': [
            {'type': 'function_call', 'call_id': 'a', 'name': 'screenshot', 'arguments': '{"app":"a"}'},
            {'type': 'function_call_output', 'call_id': 'a', 'output': [
                {'type': 'input_image', 'image_url': 'data:image/png;base64,QUJD'}]}]})
        converted = server.openai_to_anthropic_request(chat)
        self.assertEqual(converted['tools'][0]['input_schema'], TOOLS[0]['parameters'])
        self.assertEqual(converted['tool_choice']['type'], 'any')
        self.assertEqual(converted['messages'][0]['content'][0]['type'], 'tool_use')
        self.assertEqual([p['type'] for p in converted['messages'][1]['content']], ['tool_result', 'image'])


class ResponsesToolStreamsTest(unittest.TestCase):
    def test_fragmented_names_arguments_parallel_calls_and_text(self):
        data = chat_delta(text='Inspecting')
        data += chat_delta([{'index': 0, 'id': 'call_', 'function': {'name': 'screen'}}])
        data += chat_delta([{'index': 0, 'id': 'a', 'function': {'name': 'shot', 'arguments': '{"app":'}},
                            {'index': 1, 'id': 'call_b', 'function': {'name': 'screenshot', 'arguments': '{"app":"b"}'}}])
        data += chat_delta([{'index': 0, 'function': {'arguments': '"你好"}'}}])
        data += chat_delta(finish='tool_calls') + b'data: [DONE]\n\n'
        result = events(data)
        self.assertEqual(result[-1]['type'], 'response.completed')
        output = result[-1]['response']['output']
        self.assertEqual([i['type'] for i in output], ['message', 'function_call', 'function_call'])
        self.assertEqual(output[1]['call_id'], 'call_a')
        self.assertEqual(json.loads(output[1]['arguments']), {'app': '你好'})
        self.assertEqual(json.loads(output[2]['arguments']), {'app': 'b'})
        self.assertEqual([e['sequence_number'] for e in result], list(range(len(result))))
        for item in output[1:]:
            deltas = [e['delta'] for e in result if e['type'] == 'response.function_call_arguments.delta' and e['item_id'] == item['id']]
            self.assertEqual(''.join(deltas), item['arguments'])
            done = [e for e in result if e['type'] == 'response.output_item.done' and e['item']['id'] == item['id']]
            self.assertEqual(len(done), 1)

    def test_tool_only_and_empty_arguments_have_executable_output(self):
        data = chat_delta([{'index': 0, 'id': 'a', 'function': {'name': 'screenshot'}}])
        result = events(data + chat_delta(finish='tool_calls') + b'data: [DONE]')
        self.assertEqual(result[-1]['type'], 'response.completed')
        self.assertEqual(result[-1]['response']['output'][0]['arguments'], '{}')

    def test_arguments_are_emitted_before_upstream_completion(self):
        prefix = chat_delta([{'index': 0, 'id': 'a', 'function': {'name': 'screenshot', 'arguments': '{"app":'}}])
        def source():
            yield from io.BytesIO(prefix)
            self.fail('stream was buffered instead of returning the argument delta')
        stream = server.gateway_responses_stream(source(), 'openai', 'm')
        for event in stream:
            if b'response.function_call_arguments.delta' in event:
                break
        else:
            self.fail('No argument delta')
        stream.close()

    def test_custom_tool_stream_unwraps_json_input(self):
        context = {'exec': {'name': 'exec', 'type': 'custom'}}
        raw = 'print("你好")\n'
        args = json.dumps({'input': raw})
        data = chat_delta([{'index': 0, 'id': 'a', 'function': {'name': 'exec', 'arguments': args[:8]}}])
        data += chat_delta([{'index': 0, 'function': {'arguments': args[8:]}}])
        result = events(data + chat_delta(finish='tool_calls') + b'data: [DONE]\n\n', context)
        self.assertEqual(result[-1]['response']['output'][0]['input'], raw)
        self.assertEqual([e['input'] for e in result if e['type'] == 'response.custom_tool_call_input.done'], [raw])

    def test_failed_or_truncated_tool_streams_never_complete_calls(self):
        prefix = chat_delta([{'index': 0, 'id': 'a', 'function': {'name': 'screenshot', 'arguments': '{'}}])
        for ending in (b'', b'data: [DONE]\n\n', chat_delta(finish='length') + b'data: [DONE]\n\n'):
            with self.subTest(ending=ending):
                result = events(prefix + ending)
                self.assertIn(result[-1]['type'], ['response.failed', 'response.incomplete'])
                self.assertNotIn('response.output_item.done', [e['type'] for e in result])
                self.assertNotIn('response.completed', [e['type'] for e in result])

    def test_undeclared_tool_fails_without_execution(self):
        data = chat_delta([{'index': 0, 'id': 'a', 'function': {'name': 'unknown', 'arguments': '{}'}}])
        self.assertEqual(events(data + b'data: [DONE]\n\n', {})[-1]['type'], 'response.failed')

    def test_anthropic_tool_json_deltas(self):
        data = frame({'type': 'message_start', 'message': {}})
        data += frame({'type': 'content_block_start', 'index': 1, 'content_block': {
            'type': 'tool_use', 'id': 'a', 'name': 'screenshot', 'input': {}}})
        data += frame({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"app":"a"}'}})
        data += frame({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}})
        data += frame({'type': 'message_stop'})
        result = events(data, protocol='anthropic')
        self.assertEqual(result[-1]['type'], 'response.completed')
        self.assertEqual(json.loads(result[-1]['response']['output'][0]['arguments']), {'app': 'a'})


if __name__ == '__main__':
    unittest.main()
