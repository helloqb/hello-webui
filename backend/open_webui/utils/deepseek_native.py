"""DeepSeek Messages protocol and its projection onto Open WebUI chat events."""

import time
from contextlib import aclosing
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from fastapi import HTTPException
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import get_output_text
from open_webui.utils.session_pool import get_client_timeout, get_session
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

PROVIDER = 'deepseek_native'
BASE_URL = 'https://api.deepseek.com'
MODEL_ID = 'deepseek-native'
UPSTREAM_MODEL = 'deepseek-flash'
REPLAY_ITEM = 'open_webui:deepseek_native'
WEB_SEARCH = 'deepseek_web_search'
READ_URL = 'deepseek_read_url'
WEB_BLOCKS = {'server_tool_use', 'web_search_tool_result', 'web_fetch_tool_result'}
REPLAY_BLOCKS = WEB_BLOCKS | {'text', 'thinking', 'redacted_thinking'}


def model_list() -> dict:
    return {'object': 'list', 'data': [{'id': MODEL_ID, 'name': 'DeepSeek 原生', 'object': 'model'}]}


def native_messages(messages: list[dict], *, model_id: str | None = None) -> list[dict]:
    """Use an exact Messages replay for native answers and plain text for other answers."""
    result = []
    for message in messages:
        role = message.get('role')
        if role not in {'assistant', 'user'}:
            continue
        content = message.get('content')
        if role == 'assistant':
            output = message.get('output')
            output = output if isinstance(output, list) else []
            blocks = []
            if not model_id or message.get('model') == model_id:
                blocks = [
                    block
                    for item in output
                    if isinstance(item, dict)
                    and item.get('type') == REPLAY_ITEM
                    and isinstance(item.get('blocks'), list)
                    for block in item['blocks']
                    if isinstance(block, dict) and block.get('type') in REPLAY_BLOCKS
                ]
            content = blocks or get_output_text(output) or content
            if not blocks and isinstance(content, list):
                content = ''.join(
                    part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text'
                )
        if content:
            result.append({'role': role, 'content': content})
    return result


def build_request(form_data: dict, messages: list[dict]) -> dict:
    features = form_data.get('features') or {}
    tools = [READ_URL]
    if features.get('deepseek_search') is True:
        tools.append(WEB_SEARCH)
    payload = {
        'model': UPSTREAM_MODEL,
        'messages': messages,
        'stream': form_data.get('stream') is True,
        'thinking': {'type': 'enabled' if features.get('deepseek_thinking') is True else 'disabled'},
        'tools': [{'type': tool, 'name': tool} for tool in tools],
        'safety_check': {'level': 'high'},
    }
    system = []
    for message in form_data.get('messages', []):
        if message.get('role') != 'system':
            continue
        content = message.get('content')
        if isinstance(content, str) and content.strip():
            system.append(content)
        elif isinstance(content, list):
            system.extend(part['text'] for part in content if part.get('type') == 'text' and part.get('text'))
    if system:
        payload['system'] = '\n\n'.join(system)
    max_tokens = form_data.get('max_tokens') or form_data.get('max_completion_tokens')
    if type(max_tokens) is int and max_tokens > 0:
        payload['max_tokens'] = max_tokens
    return payload


def _sse(data: dict) -> str:
    return f'data: {JSONCodec.dumps(data)}\n\n'


async def _message_events(content):
    """Frame SSE across arbitrary transport chunks, including large signed tool blocks."""
    buffer = bytearray()
    data = []
    async for chunk in content.iter_any():
        buffer.extend(chunk)
        start = 0
        while (end := buffer.find(b'\n', start)) != -1:
            line = buffer[start:end].removesuffix(b'\r')
            start = end + 1
            if line.startswith(b'data:'):
                data.append(line[5:].removeprefix(b' '))
            elif not line and data:
                event = JSONCodec.loads(b'\n'.join(data))
                data.clear()
                if not isinstance(event, dict):
                    raise ValueError('Invalid DeepSeek event')
                yield event
        if start:
            del buffer[:start]


def _http_url(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return url.scheme in {'http', 'https'} and bool(url.hostname)
    except ValueError:
        return False


def _project_block(block, item_id, status):
    if block['type'] == 'thinking':
        return {
            'id': item_id,
            'type': 'reasoning',
            'status': status,
            'content': [{'type': 'reasoning_text', 'text': block.get('thinking', '')}],
            'summary': [],
        }
    text = block.get('text', '')
    annotations = [
        {'type': 'url_citation', 'url': citation['url'], 'title': citation.get('title') or citation['url']}
        for citation in block.get('citations', [])
        if _http_url(citation.get('url'))
    ]
    return {
        'id': item_id,
        'type': 'message',
        'role': 'assistant',
        'status': status,
        'content': [{'type': 'output_text', 'text': text, 'annotations': annotations}],
    }


def _merge_text(item, block):
    part = item['content'][0]
    projected = _project_block(block, item['id'], 'completed')['content'][0]
    existing = {annotation['url'] for annotation in part['annotations']}
    item['status'] = 'completed'
    part['text'] += projected['text']
    part['annotations'].extend(
        annotation for annotation in projected['annotations'] if annotation['url'] not in existing
    )


def _source(url, title):
    return {'source': {'name': title, 'url': url}, 'document': [title], 'metadata': [{'name': title, 'source': url}]}


def _sources(output, blocks, *, include_citations):
    seen = set()
    for item in output:
        if item['type'] != 'message':
            continue
        for citation in item['content'][0]['annotations']:
            if include_citations and citation['url'] not in seen:
                yield _source(citation['url'], citation['title'])
            seen.add(citation['url'])
    for block in blocks:
        if block['type'] != 'web_fetch_tool_result' or block.get('kind') != 'read_link':
            continue
        result = block.get('content') or {}
        url = result.get('url')
        if _http_url(url) and url not in seen:
            seen.add(url)
            yield _source(url, (result.get('content') or {}).get('title') or url)


def _replay_item(blocks):
    return {'type': REPLAY_ITEM, 'id': uuid4().hex, 'status': 'completed', 'blocks': blocks}


def _web_status(action, description, *, done):
    return {'event': {'type': 'status', 'data': {'action': action, 'description': description, 'done': done}}}


def _provider_status_error(status_code):
    messages = {
        400: 'DeepSeek rejected the request. Check the message, attachments, and context length.',
        401: 'DeepSeek rejected the API key. Check the server connection settings.',
        402: 'The DeepSeek account has insufficient balance.',
        403: 'DeepSeek denied access to this request.',
        413: 'The DeepSeek request is too large.',
        429: 'DeepSeek is rate limiting requests. Try again later.',
    }
    return HTTPException(
        status_code if status_code in messages else 502,
        messages.get(status_code, 'DeepSeek is unavailable. Try again later.'),
    )


class _MessageStream:
    """Accumulate provider blocks separately from their incremental presentation."""

    def __init__(self):
        self.blocks = {}
        self.output = []
        self.indexes = {}
        self.actions = {}
        self.usage = {}
        self.stop_reason = None

    def start_message(self, event):
        message = event.get('message') or {}
        self.usage.update(message.get('usage') or {})
        yield {'type': 'response.created', 'response': {'id': message.get('id'), 'model': message.get('model')}}

    def update_message(self, event):
        self.usage.update(event.get('usage') or {})
        self.stop_reason = (event.get('delta') or {}).get('stop_reason') or self.stop_reason
        return ()

    def start_block(self, event):
        index = event['index']
        block = self.blocks[index] = event['content_block']
        block_type = block['type']
        if block_type == 'server_tool_use' and block.get('name') in {WEB_SEARCH, READ_URL}:
            reading = block.get('input', {}).get('action') in {'read_link', 'open_page', 'find_in_page'}
            action = READ_URL if reading else WEB_SEARCH
            self.actions[block['id']] = (action, reading)
            yield _web_status(action, 'Reading a web page' if reading else 'Searching the web', done=False)
        if block_type not in {'text', 'thinking'}:
            return
        if block_type == 'text' and self.output and self.output[-1]['type'] == 'message':
            self.indexes[index] = len(self.output) - 1
        else:
            self.indexes[index] = len(self.output)
            item = _project_block({'type': block_type}, uuid4().hex, 'in_progress')
            self.output.append(item)
            yield {'type': 'response.output_item.added', 'output_index': self.indexes[index], 'item': item}
        if block.get(block_type):
            yield self.text_delta(index, block_type, block[block_type])

    def update_block(self, event):
        index = event['index']
        block = self.blocks[index]
        delta = event['delta']
        delta_type = delta['type']
        if delta_type in {'text_delta', 'thinking_delta', 'signature_delta'}:
            field = delta_type.removesuffix('_delta')
            block[field] = block.get(field, '') + delta[field]
            if field != 'signature':
                yield self.text_delta(index, field, delta[field])
        elif delta_type == 'citations_delta':
            block.setdefault('citations', []).append(delta['citation'])

    def text_delta(self, block_index, field, text):
        return {
            'type': 'response.output_text.delta' if field == 'text' else 'response.reasoning_text.delta',
            'output_index': self.indexes[block_index],
            'content_index': 0,
            'delta': text,
        }

    def stop_block(self, event):
        index = event['index']
        block = self.blocks[index]
        if index in self.indexes:
            position = self.indexes[index]
            if block['type'] == 'text':
                # DeepSeek starts a new text block after a citation within the same Markdown.
                _merge_text(self.output[position], block)
            else:
                self.output[position] = item = _project_block(block, self.output[position]['id'], 'completed')
                yield {'type': 'response.output_item.done', 'output_index': position, 'item': item}
        elif block['type'] in {'web_search_tool_result', 'web_fetch_tool_result'}:
            call = self.actions.pop(block.get('tool_use_id'), None)
            if call:
                action, reading = call
                content = block['content']
                entries = content if isinstance(content, list) else [content]
                failed = any(entry.get('error_code') for entry in entries)
                description = (
                    'DeepSeek web tool failed' if failed else ('Web page read' if reading else 'Web search completed')
                )
                yield _web_status(action, description, done=True)

    def unfinished_tools(self):
        for action, _ in self.actions.values():
            yield _web_status(action, 'DeepSeek web tool failed', done=True)

    def finish_message(self):
        yield from self.unfinished_tools()
        if self.stop_reason == 'refusal':
            # Replace any streamed material before Open WebUI persists the result.
            blank = _project_block({'type': 'text'}, uuid4().hex, 'completed')
            yield {'type': 'response.incomplete', 'response': {'output': [blank], 'usage': self.usage}}
            yield {'type': 'error', 'message': 'DeepSeek declined to answer this request.'}
            return
        ordered_blocks = [block for _, block in sorted(self.blocks.items())]
        for position, item in enumerate(self.output):
            if item['type'] == 'message':
                yield {'type': 'response.output_item.done', 'output_index': position, 'item': item}
        for source in _sources(self.output, ordered_blocks, include_citations=False):
            yield {'event': {'type': 'source', 'data': source}}
        self.output.append(_replay_item(ordered_blocks))
        incomplete = self.stop_reason == 'max_tokens'
        yield {
            'type': 'response.incomplete' if incomplete else 'response.completed',
            'response': {
                'status': 'incomplete' if incomplete else 'completed',
                **({'incomplete_details': {'reason': 'max_output_tokens'}} if incomplete else {}),
                'output': self.output,
                'usage': self.usage,
            },
        }


async def stream_events(response):
    stream = _MessageStream()
    handlers = {
        'message_start': stream.start_message,
        'message_delta': stream.update_message,
        'content_block_start': stream.start_block,
        'content_block_delta': stream.update_block,
        'content_block_stop': stream.stop_block,
    }
    error = 'DeepSeek ended the stream before completing the response.'
    try:
        async for event in _message_events(response.content):
            kind = event.get('type')
            if kind == 'error' or event.get('error'):
                error = 'DeepSeek could not complete the response. Try again.'
                break
            if kind == 'message_stop':
                for frame in stream.finish_message():
                    yield _sse(frame)
                return
            if handler := handlers.get(kind):
                for frame in handler(event):
                    yield _sse(frame)
    except TimeoutError:
        error = 'The DeepSeek response timed out.'
    except (aiohttp.ClientError, ValueError, KeyError, TypeError, AttributeError):
        error = 'The DeepSeek response was interrupted or invalid.'
    finally:
        response.close()
    for frame in stream.unfinished_tools():
        yield _sse(frame)
    yield _sse({'type': 'error', 'message': error})


def complete_response(body: dict, model_id: str) -> dict:
    if body.get('error') or not isinstance(body.get('content'), list):
        raise HTTPException(502, 'DeepSeek could not complete the response. Try again.')
    stop_reason = body.get('stop_reason')
    if stop_reason == 'refusal':
        raise HTTPException(400, 'DeepSeek declined to answer this request.')
    blocks = body['content']
    output = []
    for block in blocks:
        if block['type'] == 'text' and output and output[-1]['type'] == 'message':
            _merge_text(output[-1], block)
        elif block['type'] in {'text', 'thinking'}:
            output.append(_project_block(block, uuid4().hex, 'completed'))
    content = ''.join(block['text'] for block in blocks if block['type'] == 'text')
    sources = list(_sources(output, blocks, include_citations=True))
    output.append(_replay_item(blocks))
    usage = body.get('usage') or {}
    prompt_tokens = usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0)
    completion_tokens = usage.get('output_tokens', 0)
    return {
        'id': body.get('id'),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model_id,
        'output': output,
        **({'sources': sources} if sources else {}),
        'choices': [
            {
                'index': 0,
                'message': {'role': 'assistant', 'content': content},
                'finish_reason': 'length' if stop_reason == 'max_tokens' else 'stop',
            }
        ],
        'usage': {
            **usage,
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': prompt_tokens + completion_tokens,
        },
    }


def _streaming_response(response):
    if response.content_type != 'text/event-stream':
        raise HTTPException(502, 'DeepSeek returned an invalid streaming response.')

    async def body_iterator():
        async with aclosing(stream_events(response)) as events:
            async for event in events:
                yield event

    return StreamingResponse(body_iterator(), media_type='text/event-stream', background=BackgroundTask(response.close))


async def generate(form_data: dict, key: str, user, model_id: str):
    from open_webui.utils.deepseek_files import FILES_BETA, prepare_deepseek_messages

    prepared, file_refs = await prepare_deepseek_messages(form_data.get('messages', []), key, user)
    messages = native_messages(prepared, model_id=model_id)
    if not messages or messages[-1]['role'] != 'user':
        raise HTTPException(400, 'DeepSeek requires a user message.')
    payload = build_request(form_data, messages)
    headers = {
        'Authorization': f'Bearer {key}',
        'Accept': 'text/event-stream' if payload['stream'] else 'application/json',
    }
    if file_refs:
        headers['anthropic-beta'] = FILES_BETA
    response = None
    streaming = False
    try:
        session = await get_session()
        configured_timeout = get_client_timeout(stream=payload['stream'])
        timeout = aiohttp.ClientTimeout(
            total=configured_timeout.total or 600,
            connect=30,
            sock_read=configured_timeout.sock_read or (120 if payload['stream'] else None),
        )
        response = await session.post(
            f'{BASE_URL}/anthropic/v1/messages',
            json=payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        )
        if not 200 <= response.status < 300:
            raise _provider_status_error(response.status)
        if payload['stream']:
            stream = _streaming_response(response)
            streaming = True
            return stream
        return complete_response(await response.json(), model_id)
    except TimeoutError as exc:
        raise HTTPException(504, 'The DeepSeek response timed out.') from exc
    except (aiohttp.ClientError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(502, 'DeepSeek returned an invalid response or could not be reached.') from exc
    finally:
        if response is not None and not streaming:
            response.close()
