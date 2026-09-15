"""Codex reasoning translation uses synthetic upstream data, without API calls."""
import io
import json
import unittest

import server


def frame(value):
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode()


def chat(delta, finish=None):
    return frame({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})


def decode(event):
    return json.loads(event.decode().split('data: ', 1)[1])


def events(data, protocol='openai', enabled=True):
    return [decode(event) for event in server.gateway_responses_stream(
        io.BytesIO(data), protocol, 'test-model', include_reasoning=enabled)]


class CodexReasoningTest(unittest.TestCase):
    def test_reasoning_arrives_before_upstream_continues(self):
        advanced = []

        def source():
            yield from io.BytesIO(chat({'reasoning_content': '先分析'}))
            advanced.append(True)
            yield from io.BytesIO(chat({'content': '答案'}, 'stop') + b'data: [DONE]\n\n')

        stream = server.gateway_responses_stream(source(), 'openai', 'm', include_reasoning=True)
        received = []
        while True:
            event = decode(next(stream))
            received.append(event)
            if event['type'] == 'response.reasoning_summary_text.delta':
                break
        self.assertFalse(advanced)
        self.assertEqual(received[-1]['delta'], '先分析')
        received.extend(decode(event) for event in stream)
        output = received[-1]['response']['output']
        self.assertEqual([item['type'] for item in output], ['reasoning', 'message'])
        self.assertEqual(output[0]['summary'][0]['text'], '先分析')
        self.assertEqual(output[1]['content'][0]['text'], '答案')
        types = [e['type'] for e in received]
        self.assertLess(types.index('response.reasoning_summary_text.done'), types.index('response.output_text.delta'))
        self.assertEqual([e['sequence_number'] for e in received], list(range(len(received))))
        for event in received:
            if event['type'].startswith('response.reasoning_summary'):
                self.assertEqual(event['item_id'], output[0]['id'])
                self.assertEqual(event['output_index'], 0)
                self.assertEqual(event['summary_index'], 0)

    def test_reasoning_deltas_and_tools_keep_distinct_output_indices(self):
        data = chat({'reasoning_content': '分析'}) + chat({'reasoning': '完成'})
        data += chat({'tool_calls': [{'index': 0, 'id': 'call_test', 'type': 'function',
                                    'function': {'name': 'demo', 'arguments': '{}'}}]}, 'tool_calls')
        result = events(data + b'data: [DONE]\n\n')
        output = result[-1]['response']['output']
        self.assertEqual([item['type'] for item in output], ['reasoning', 'function_call'])
        self.assertEqual(output[0]['summary'][0]['text'], '分析完成')
        added = [e for e in result if e['type'] == 'response.output_item.added']
        self.assertEqual([e['output_index'] for e in added], [0, 1])
        # Summaries are display text, never provider-signed replay state.
        history = server.responses_to_openai({'input': output + [
            {'type': 'function_call_output', 'call_id': 'call_test', 'output': 'ok'}]})
        self.assertEqual([m['role'] for m in history['messages']], ['assistant', 'tool'])
        self.assertNotIn('分析', json.dumps(history, ensure_ascii=False))

    def test_anthropic_thinking_excludes_signatures_and_redacted_blocks(self):
        data = frame({'type': 'content_block_start', 'index': 0,
                      'content_block': {'type': 'thinking', 'thinking': '先'}})
        data += frame({'type': 'content_block_delta', 'index': 0,
                       'delta': {'type': 'thinking_delta', 'thinking': '分析'}})
        data += frame({'type': 'content_block_delta', 'index': 0,
                       'delta': {'type': 'signature_delta', 'signature': 'opaque-signature'}})
        data += frame({'type': 'content_block_start', 'index': 1,
                       'content_block': {'type': 'redacted_thinking', 'data': 'opaque-redacted'}})
        data += frame({'type': 'content_block_delta', 'index': 2,
                       'delta': {'type': 'text_delta', 'text': '答案'}})
        data += frame({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}})
        result = events(data + frame({'type': 'message_stop'}), 'anthropic')
        self.assertEqual(result[-1]['response']['output'][0]['summary'][0]['text'], '先分析')
        self.assertNotIn('opaque', json.dumps(result))

    def test_interleaved_reasoning_creates_separate_items(self):
        data = chat({'reasoning_content': 'first'}) + chat({'content': 'answer'})
        data += chat({'reasoning_content': 'second'}, 'stop') + b'data: [DONE]\n\n'
        output = events(data)[-1]['response']['output']
        self.assertEqual([i['type'] for i in output], ['reasoning', 'message', 'reasoning'])
        self.assertEqual(len({i['id'] for i in output}), 3)

    def test_truncation_never_claims_response_completed(self):
        partial = chat({'reasoning_content': 'partial'})
        result = events(partial)
        self.assertEqual(result[-1]['type'], 'response.failed')
        self.assertNotIn('response.reasoning_summary_text.done', [e['type'] for e in result])
        result = events(partial + chat({}, 'length') + b'data: [DONE]\n\n')
        self.assertEqual(result[-1]['type'], 'response.incomplete')
        self.assertEqual(result[-1]['response']['output'][0]['status'], 'incomplete')

    def test_disabled_and_absent_reasoning_emit_no_reasoning_items(self):
        for data, enabled in ((chat({'reasoning_content': 'hidden', 'content': 'answer'}, 'stop'), False),
                              (chat({'content': 'answer'}, 'stop'), True)):
            output = events(data + b'data: [DONE]\n\n', enabled=enabled)[-1]['response']['output']
            self.assertEqual([i['type'] for i in output], ['message'])

    def test_nonstream_openai_and_anthropic(self):
        anthropic = {'content': [{'type': 'thinking', 'thinking': 'analysis', 'signature': 'opaque'},
                                 {'type': 'text', 'text': 'answer'}], 'stop_reason': 'end_turn'}
        body = server.anthropic_response_to_openai(anthropic, 'm', include_reasoning=True)
        output = server.openai_to_responses(body, 'm', include_reasoning=True)['output']
        self.assertEqual(output[0]['summary'][0]['text'], 'analysis')
        self.assertEqual(output[1]['content'][0]['text'], 'answer')
        self.assertNotIn('opaque', json.dumps(output))
        self.assertEqual(server.openai_to_responses(body, 'm')['output'][0]['type'], 'message')
