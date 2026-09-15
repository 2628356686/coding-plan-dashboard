"""Codex protocol conversion with synthetic IDs and no external services."""
import json
import unittest

import server


def history(protocol, ident='vendor.call:one'):
    if protocol == 'responses':
        return {'input': [
            {'type': 'message', 'id': 'resp_example_msg', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'vendor.call:one'}]},
            {'type': 'function_call', 'id': 'vendor_item', 'call_id': ident,
             'name': 'check', 'arguments': '{"id":"vendor.call:one"}'},
            {'type': 'function_call_output', 'call_id': ident, 'output': 'ok'}]}
    if protocol == 'openai':
        return {'messages': [
            {'role': 'assistant', 'id': 'resp_example_msg', 'content': 'vendor.call:one',
             'tool_calls': [{'id': ident, 'type': 'function',
                             'function': {'name': 'check', 'arguments': '{"id":"vendor.call:one"}'}}]},
            {'role': 'tool', 'tool_call_id': ident, 'content': 'ok'}]}
    return {'messages': [
        {'role': 'assistant', 'id': 'resp_example_msg', 'content': [
            {'type': 'tool_use', 'id': ident, 'name': 'check', 'input': {'id': 'vendor.call:one'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': ident, 'content': 'ok'}]}]}


class CodexCompatibilityTest(unittest.TestCase):
    def test_platform_gate_and_originator_precedence(self):
        for name in ('Codex Desktop', 'codex_cli_rs', 'codex', 'codex/1', 'codex_exec'):
            self.assertTrue(server.is_codex_gateway_platform(name))
        for name in ('unknown', 'Claude Code', 'not-codex', 'codexish', 'ZCode', ''):
            self.assertFalse(server.is_codex_gateway_platform(name))
        platform = server.detect_gateway_agent_platform({'Originator': 'ZCode', 'User-Agent': 'codex/1'})
        self.assertFalse(server.is_codex_gateway_platform(platform))

    def test_anthropic_fallback_keeps_tools_and_images(self):
        body = history('anthropic', 'call_valid')
        body['tools'] = [{'name': 'check', 'input_schema': {'type': 'object'}}]
        body['tool_choice'] = {'type': 'any', 'disable_parallel_tool_use': True}
        body['messages'][1]['content'][0]['content'] = [
            {'type': 'text', 'text': 'ok'},
            {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/image.png'}}]
        result = server.anthropic_to_openai(body, codex_compat=True)
        self.assertEqual(result['messages'][0]['tool_calls'][0]['id'], 'call_valid')
        self.assertEqual(result['messages'][1]['tool_call_id'], 'call_valid')
        self.assertEqual(result['messages'][2]['content'][0]['type'], 'image_url')
        self.assertEqual(result['tools'][0]['function']['name'], 'check')
        self.assertEqual(result['tool_choice'], 'required')
        self.assertFalse(result['parallel_tool_calls'])

    def test_anthropic_tool_reply_and_stream_keep_call_identity(self):
        body = {'choices': [{'message': {'tool_calls': [{'id': 'call_test',
            'function': {'name': 'check', 'arguments': '{"x":1}'}}]}, 'finish_reason': 'tool_calls'}]}
        reply = server.openai_to_anthropic(body, 'test-model', codex_compat=True)
        self.assertEqual(reply['stop_reason'], 'tool_use')
        wire = server.gateway_message_events(reply, 'anthropic', codex_compat=True).decode()
        events = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith('data: ')]
        start = next(e for e in events if e['type'] == 'content_block_start')
        self.assertEqual(start['content_block']['id'], 'call_test')
        delta = next(e for e in events if e['type'] == 'content_block_delta')
        self.assertEqual(json.loads(delta['delta']['partial_json']), {'x': 1})


if __name__ == '__main__':
    unittest.main()
