import json
import unittest
from copy import deepcopy

from open_webui.utils.deepseek_native import (
    MODEL_ID,
    REPLAY_ITEM,
    build_request,
    complete_response,
    native_messages,
    stream_events,
)
from open_webui.utils.misc import get_output_text

URL = 'https://example.com/report'
CITATION = {
    'type': 'web_search_result_location',
    'url': URL,
    'title': '公开报告',
    'cited_text': '报告已发布。',
    'encrypted_index': '',
}
READ_CALL = {
    'type': 'server_tool_use',
    'id': 'srvtoolu_rl_00_test',
    'name': 'deepseek_read_url',
    'input': {'action': 'read_link', 'url': URL},
    'kind': 'read_link',
    'caller': {'type': 'direct'},
}
READ_RESULT = {
    'type': 'web_fetch_tool_result',
    'tool_use_id': READ_CALL['id'],
    'kind': 'read_link',
    'signature': 'SGFzYVByb3ZpZGVyU2lnbmF0dXJl',
    'content': {
        'type': 'web_fetch_result',
        'url': URL,
        'published_at': None,
        'content': {
            'type': 'document',
            'title': CITATION['title'],
            'source': {'type': 'text', 'media_type': 'text/plain', 'data': ''},
        },
    },
}
SEARCH_CALL = {
    'type': 'server_tool_use',
    'id': 'srvtoolu_ds_00_test',
    'name': 'deepseek_web_search',
    'input': {'action': 'search', 'queries': ['公开报告']},
    'kind': 'deep_search',
    'caller': {'type': 'direct'},
}
SEARCH_RESULT = {
    'type': 'web_search_tool_result',
    'tool_use_id': SEARCH_CALL['id'],
    'kind': 'deep_search',
    'content': [
        {
            'type': 'web_search_result',
            'url': URL,
            'title': CITATION['title'],
            'snippet': '检索摘要',
            'site_name': 'Example',
            'page_age': None,
            'site_icon': None,
            'encrypted_content': 'opaque-search-content',
        }
    ],
}
THINKING = {'type': 'thinking', 'thinking': '我会先核对来源。', 'signature': 'provider-thinking-signature'}
ANSWER = {'type': 'text', 'text': CITATION['cited_text'], 'citations': [CITATION]}
USAGE = {'input_tokens': 17, 'output_tokens': 5, 'cache_read_input_tokens': 2}


def sse_frame(event):
    event_line = f'event: {event["type"]}\r\n' if isinstance(event, dict) and 'type' in event else ''
    return f'{event_line}data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n'.encode()


class MockResponse:
    def __init__(self, events, end_error=None):
        self.content = self
        self.events = events
        self.end_error = end_error
        self.closed = False

    async def iter_any(self):
        wire = b': keep-alive\r\n\r\n' + b''.join(map(sse_frame, self.events))
        for offset in range(0, len(wire), 13):
            yield wire[offset : offset + 13]
        if self.end_error is not None:
            raise self.end_error

    def close(self):
        self.closed = True


def as_webui_message(output, content):
    # Cross the same JSON boundary as a saved and reopened Open WebUI chat.
    return json.loads(json.dumps({'role': 'assistant', 'model': MODEL_ID, 'content': content, 'output': output}))


def block_start(index, content_block):
    return {'type': 'content_block_start', 'index': index, 'content_block': content_block}


def block_stop(index):
    return {'type': 'content_block_stop', 'index': index}


def block_delta(index, type_name, **fields):
    return {'type': 'content_block_delta', 'index': index, 'delta': {'type': type_name, **fields}}


class NativeRequestTest(unittest.TestCase):
    def test_search_and_thinking_toggles_are_independent(self):
        base = {
            'model': 'client-model',
            'stream': True,
            'messages': [{'role': 'user', 'content': 'Hello'}],
            'tools': [{'type': 'function', 'function': {'name': 'local_web_search'}}],
            'tool_choice': 'required',
            'temperature': 0.5,
        }
        for search, thinking in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(search=search, thinking=thinking):
                request = build_request(
                    {**base, 'features': {'deepseek_search': search, 'deepseek_thinking': thinking}},
                    native_messages(base['messages']),
                )
                expected_tools = [{'type': 'deepseek_read_url', 'name': 'deepseek_read_url'}]
                if search:
                    expected_tools.append({'type': 'deepseek_web_search', 'name': 'deepseek_web_search'})
                self.assertEqual(request['model'], 'deepseek-flash')
                self.assertEqual(request['thinking'], {'type': 'enabled' if thinking else 'disabled'})
                self.assertEqual(request['tools'], expected_tools)
                self.assertEqual(request['messages'], base['messages'])
                self.assertTrue(request['stream'])
                self.assertNotIn('tool_choice', request)
                self.assertNotIn('temperature', request)
                self.assertNotIn('system', request)

    def test_explicit_system_context_and_provider_file_blocks(self):
        user = {
            'role': 'user',
            'content': [
                {'type': 'document', 'source': {'type': 'file', 'file_id': 'file-api-document'}},
                {'type': 'text', 'text': '请总结。'},
            ],
        }
        form = {
            'messages': [
                {'role': 'system', 'content': 'Match the requested language.'},
                {'role': 'system', 'content': [{'type': 'text', 'text': 'Use metric units.'}]},
                user,
            ],
            'max_completion_tokens': 1024,
        }
        request = build_request(form, native_messages(form['messages']))
        self.assertEqual(request['system'], 'Match the requested language.\n\nUse metric units.')
        self.assertEqual(request['messages'], [user])
        self.assertEqual(request['max_tokens'], 1024)

    def test_reopened_chat_replays_provider_blocks(self):
        cited_answer = {**ANSWER, 'text': '报告已发布'}
        blocks = [THINKING, READ_CALL, READ_RESULT, cited_answer, {'type': 'text', 'text': '。'}]
        body = {'id': 'msg_test', 'content': deepcopy(blocks), 'stop_reason': 'end_turn', 'usage': USAGE}
        completion = complete_response(body, MODEL_ID)
        answer = completion['choices'][0]['message']['content']
        self.assertEqual(answer, ANSWER['text'])
        self.assertEqual(get_output_text(completion['output']), answer)
        self.assertEqual(completion['usage']['total_tokens'], 24)
        history = [
            {'role': 'user', 'content': f'Summarize {URL}'},
            as_webui_message(completion['output'], answer),
            {'role': 'user', 'content': 'What else does the page say?'},
        ]
        replay = build_request({}, native_messages(history, model_id=MODEL_ID))
        self.assertEqual(replay['messages'][1], {'role': 'assistant', 'content': blocks})
        self.assertEqual(replay['messages'][2], history[2])
        self.assertEqual(
            native_messages(history, model_id='different-model')[1],
            {'role': 'assistant', 'content': answer},
        )


class NativeStreamTest(unittest.IsolatedAsyncioTestCase):
    async def read_stream(self, frames, end_error=None):
        response = MockResponse(frames, end_error=end_error)
        output = [json.loads(frame.removeprefix('data: ')) async for frame in stream_events(response)]
        self.assertTrue(response.closed)
        return output

    async def test_combined_search_stream_keeps_order_and_provider_data(self):
        frames = [
            {'type': 'message_start', 'message': {'id': 'msg_test', 'usage': {'input_tokens': 0}}},
            {'type': 'ping'},
            block_start(0, {'type': 'thinking', 'thinking': ''}),
            block_delta(0, 'thinking_delta', thinking=THINKING['thinking']),
            block_delta(0, 'signature_delta', signature=THINKING['signature'][:10]),
            block_delta(0, 'signature_delta', signature=THINKING['signature'][10:]),
            block_stop(0),
            block_start(1, SEARCH_CALL),
            block_stop(1),
            block_start(2, SEARCH_RESULT),
            block_stop(2),
            block_start(3, {'type': 'text', 'text': ''}),
            block_delta(3, 'text_delta', text=ANSWER['text']),
            block_delta(3, 'citations_delta', citation=CITATION),
            block_stop(3),
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': USAGE},
            {'type': 'message_stop'},
        ]
        events = await self.read_stream(frames)
        self.assertEqual(
            [event.get('type', event.get('event', {}).get('type')) for event in events],
            [
                'response.created',
                'response.output_item.added',
                'response.reasoning_text.delta',
                'response.output_item.done',
                'status',
                'status',
                'response.output_item.added',
                'response.output_text.delta',
                'response.output_item.done',
                'response.completed',
            ],
        )
        self.assertEqual(events[2]['delta'], THINKING['thinking'])
        self.assertEqual(events[4]['event']['data']['action'], 'deepseek_web_search')
        self.assertFalse(events[4]['event']['data']['done'])
        self.assertTrue(events[5]['event']['data']['done'])
        self.assertEqual(events[7]['delta'], ANSWER['text'])
        self.assertEqual(events[8]['item']['content'][0]['annotations'][0]['url'], URL)
        response = events[-1]['response']
        self.assertEqual(response['usage'], USAGE)
        self.assertEqual(response['output'][-1]['type'], REPLAY_ITEM)
        self.assertEqual(response['output'][-1]['blocks'], [THINKING, SEARCH_CALL, SEARCH_RESULT, ANSWER])

    async def test_signed_streamed_link_survives_reopen_and_citation_boundaries(self):
        # DeepSeek ends a text block at a citation, even in the middle of a sentence.
        cited = {
            'type': 'text',
            'text': '报告已发布',
            'citations': [{**CITATION, 'cited_text': '报告已发布'}],
        }
        suffix = {'type': 'text', 'text': '。\n\n继续阅读。'}
        events = await self.read_stream(
            [
                {'type': 'message_start', 'message': {'id': 'msg_test'}},
                block_start(0, READ_CALL),
                block_stop(0),
                block_start(1, READ_RESULT),
                block_stop(1),
                block_start(2, {'type': 'text', 'text': ''}),
                block_delta(2, 'text_delta', text=cited['text']),
                block_delta(2, 'citations_delta', citation=cited['citations'][0]),
                block_stop(2),
                block_start(3, {'type': 'text', 'text': ''}),
                block_delta(3, 'text_delta', text=suffix['text']),
                block_stop(3),
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
                {'type': 'message_stop'},
            ]
        )
        final = events[-1]['response']['output']
        text = get_output_text(final)
        self.assertEqual(text, '报告已发布。\n\n继续阅读。')
        assistant = as_webui_message(final, text)
        history = [assistant, {'role': 'user', 'content': '报告里有哪些建议？'}]
        self.assertEqual(
            native_messages(history, model_id=MODEL_ID)[0],
            {'role': 'assistant', 'content': [READ_CALL, READ_RESULT, cited, suffix]},
        )

    async def test_refusal_replaces_streamed_text(self):
        events = await self.read_stream(
            [
                {'type': 'message_start', 'message': {'id': 'msg_test'}},
                block_start(0, {'type': 'text', 'text': ''}),
                block_delta(0, 'text_delta', text='Provider later withdrew this text.'),
                block_stop(0),
                {'type': 'message_delta', 'delta': {'stop_reason': 'refusal'}, 'usage': USAGE},
                {'type': 'message_stop'},
            ]
        )
        replacements = [event['response']['output'] for event in events if 'output' in event.get('response', {})]
        self.assertEqual(len(replacements), 1)
        self.assertNotIn('Provider later withdrew', json.dumps(replacements[0]))
        self.assertNotIn(REPLAY_ITEM, {item['type'] for item in replacements[0]})

    async def test_interrupted_or_error_stream_cannot_produce_replay_blocks(self):
        frames = [
            {'type': 'message_start', 'message': {'id': 'msg_test'}},
            block_start(0, READ_CALL),
        ]
        for error_frame in (None, {'error': {'message': 'upstream error'}}):
            with self.subTest(error_frame=error_frame):
                events = await self.read_stream(frames if error_frame is None else [*frames, error_frame])
                self.assertEqual(events[-1]['type'], 'error')
                self.assertNotIn('response', events[-1])
                self.assertTrue(events[-2]['event']['data']['done'])
                self.assertEqual(events[1]['event']['data']['action'], 'deepseek_read_url')
                self.assertNotIn('upstream error', json.dumps(events))

    async def test_timeout_and_malformed_frame_surface_safe_errors(self):
        timeout = await self.read_stream([], end_error=TimeoutError())
        self.assertEqual(timeout[-1]['type'], 'error')
        self.assertIn('timed out', timeout[-1]['message'])
        malformed = await self.read_stream([None])
        self.assertEqual(malformed[-1]['type'], 'error')

    async def test_unknown_blocks_do_not_change_the_text_or_tool_context(self):
        unknown = {'type': 'unrecognized_provider_extension', 'field': 'preserve on disk'}
        events = await self.read_stream(
            [
                {'type': 'message_start', 'message': {'id': 'msg_test'}},
                block_start(0, unknown),
                block_stop(0),
                block_start(1, ANSWER),
                block_stop(1),
                {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}},
                {'type': 'message_stop'},
            ]
        )
        self.assertEqual(events[-1]['type'], 'response.incomplete')
        output = events[-1]['response']['output']
        self.assertEqual(output[-1]['blocks'], [unknown, ANSWER])
        assistant = as_webui_message(output, ANSWER['text'])
        self.assertEqual(native_messages([assistant]), [{'role': 'assistant', 'content': [ANSWER]}])

    async def test_closing_stream_releases_upstream(self):
        response = MockResponse(
            [
                block_start(0, {'type': 'text', 'text': ''}),
                {'type': 'message_stop'},
            ]
        )
        events = stream_events(response)
        await anext(events)
        await events.aclose()
        self.assertTrue(response.closed)
