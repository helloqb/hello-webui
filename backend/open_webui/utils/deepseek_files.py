"""Prepare DeepSeek Messages attachments using Open WebUI's file records and storage."""

import asyncio
import base64
import hashlib
import mimetypes
import re
import time
from collections import OrderedDict
from pathlib import Path
from uuid import UUID
from weakref import WeakValueDictionary

import aiohttp
from fastapi import HTTPException
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.models.config import Config
from open_webui.models.files import FileModel, Files
from open_webui.models.users import UserModel
from open_webui.storage.provider import Storage
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.utils.session_pool import get_session

FILES_BETA = 'files-api-2025-04-14'
FILES_URL = 'https://api.deepseek.com/anthropic/v1/files'
FILE_LIFETIME = 30 * 24 * 60 * 60
MIB = 1024 * 1024
_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
_LAYOUT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.ppt', '.pptx'}
_LAYOUT_TYPES = {mimetypes.guess_type('file' + ext)[0] for ext in _LAYOUT_EXTENSIONS} - {None}
_LOCAL_IMAGE_URL = re.compile(r'/api/v1/files/([^/?#]+)/content(?:[?#].*)?')
_FILE_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, connect=30)
_PARSE_ERROR = (400, 'DeepSeek could not parse this attachment or does not support its format.')
_TIMEOUT_ERROR = (504, 'DeepSeek attachment upload or parsing timed out.')
_UPLOAD_ERRORS = {
    400: _PARSE_ERROR,
    408: _TIMEOUT_ERROR,
    413: (413, 'DeepSeek rejected the attachment because it is too large.'),
    422: _PARSE_ERROR,
    429: (429, 'DeepSeek reached an attachment quota or upload rate limit.'),
    504: _TIMEOUT_ERROR,
}

# Provider IDs stay on the server. Cache by key and requesting user, and check file access
# again before each use. The provider expires its copies independently of this cache.
_CacheKey = tuple[str, str, str, str]
_file_ids: OrderedDict[_CacheKey, tuple[str, float]] = OrderedDict()
_upload_locks: WeakValueDictionary[_CacheKey, asyncio.Lock] = WeakValueDictionary()


def _inline_image(url: str) -> dict:
    header, _, data = url.partition(',')
    media_type = header.removeprefix('data:').removesuffix(';base64')
    if media_type not in _IMAGE_TYPES or not header.endswith(';base64') or not data:
        raise HTTPException(400, 'DeepSeek Native supports JPEG, PNG, GIF, and WebP images.')
    if len(data) > ((32 * MIB + 2) // 3) * 4:
        raise HTTPException(413, 'DeepSeek Native inline images must be at most 32 MiB.')
    try:
        if len(base64.b64decode(data, validate=True)) > 32 * MIB:
            raise HTTPException(413, 'DeepSeek Native inline images must be at most 32 MiB.')
    except ValueError:
        raise HTTPException(400, 'Invalid inline image in DeepSeek Native chat.') from None
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': data}}


def _image_file_id(url: str) -> str:
    match = _LOCAL_IMAGE_URL.fullmatch(url)
    if match:
        return match.group(1)
    try:
        # Some clients refer to the saved file by its Open WebUI ID.
        UUID(url)
    except ValueError:
        raise HTTPException(
            400, 'Attach remote images through Open WebUI before sending them to DeepSeek Native.'
        ) from None
    return url


def _file_format(file: FileModel) -> tuple[str, str, str, int]:
    content_type = (file.meta or {}).get('content_type')
    if not isinstance(content_type, str) or content_type == 'application/octet-stream':
        content_type = mimetypes.guess_type(file.filename)[0] or 'application/octet-stream'
    if content_type in _IMAGE_TYPES:
        return 'image', 'user_data', content_type, 64 * MIB
    if content_type.startswith(('image/', 'audio/', 'video/')):
        raise HTTPException(400, 'Unsupported attachment format for DeepSeek Native.')
    if Path(file.filename).suffix.lower() in _LAYOUT_EXTENSIONS or content_type in _LAYOUT_TYPES:
        return 'document', 'rich_layout_recognition', 'application/octet-stream', 100 * MIB
    return 'document', 'user_data', 'application/octet-stream', 32 * MIB


async def _upload_file(file: FileModel, key: str, purpose: str, content_type: str, max_size: int) -> str:
    try:
        path = await asyncio.to_thread(Storage.get_file, file.path)
        with open(path, 'rb') as original:
            if Path(path).stat().st_size > max_size:
                raise HTTPException(413, f'This DeepSeek Native attachment must be at most {max_size // MIB} MiB.')
            form = aiohttp.FormData()
            # DeepSeek requires purpose before the file part so it can select a parser.
            form.add_field('purpose', purpose)
            form.add_field('expires_after[anchor]', 'created_at')
            form.add_field('expires_after[seconds]', str(FILE_LIFETIME))
            form.add_field('file', original, filename=file.filename, content_type=content_type)
            session = await get_session()
            async with session.post(
                FILES_URL,
                data=form,
                headers={'x-api-key': key, 'anthropic-beta': FILES_BETA},
                timeout=_FILE_UPLOAD_TIMEOUT,
                allow_redirects=False,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                if not 200 <= response.status < 300:
                    raise HTTPException(
                        *_UPLOAD_ERRORS.get(
                            response.status, (502, f'DeepSeek attachment upload failed (HTTP {response.status}).')
                        )
                    )
                result = await response.json()
    except TimeoutError:
        raise HTTPException(*_TIMEOUT_ERROR) from None
    except (aiohttp.ClientError, ValueError):
        raise HTTPException(502, 'DeepSeek attachment upload failed.') from None
    except (OSError, RuntimeError):
        raise HTTPException(404, 'Original attachment is unavailable.') from None

    provider_id = result.get('id') if isinstance(result, dict) else None
    if not isinstance(provider_id, str) or not provider_id.startswith('file-api-'):
        raise HTTPException(502, 'DeepSeek returned an invalid attachment reference.')
    return provider_id


async def _file_block(file: FileModel, key: str, user_id: str) -> dict:
    block_type, purpose, content_type, max_size = _file_format(file)
    key_fingerprint = hashlib.sha256(key.encode()).hexdigest()
    cache_key = (user_id, file.id, key_fingerprint, purpose)
    lock = _upload_locks.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _upload_locks[cache_key] = lock
    async with lock:
        entry = _file_ids.get(cache_key)
        if entry and entry[1] > time.monotonic():
            provider_id = entry[0]
            _file_ids.move_to_end(cache_key)
        else:
            provider_id = await _upload_file(file, key, purpose, content_type, max_size)
            _file_ids[cache_key] = (provider_id, time.monotonic() + FILE_LIFETIME - 3600)
            _file_ids.move_to_end(cache_key)
            if len(_file_ids) > 512:
                _file_ids.popitem(last=False)

    return {'type': block_type, 'source': {'type': 'file', 'file_id': provider_id}}


def _collect_user_content(content, text_parts: list[dict], image_urls: dict[str, None]) -> None:
    if isinstance(content, str) and content:
        text_parts.append({'type': 'text', 'text': content})
    if not isinstance(content, list):
        return

    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get('type')
        if part_type == 'image_url':
            image_url = part.get('image_url')
            url = image_url.get('url') if isinstance(image_url, dict) else image_url
            if not isinstance(url, str):
                raise HTTPException(400, 'Invalid image in DeepSeek Native chat.')
            image_urls[url] = None
        elif part_type in ('image', 'document', 'file', 'input_file', 'input_image'):
            raise HTTPException(400, 'Attach a file through Open WebUI to use it with DeepSeek Native.')
        elif part_type == 'text' and isinstance(part.get('text'), str) and part['text']:
            text_parts.append({'type': 'text', 'text': part['text']})


def _attachment_parts(message: dict) -> tuple[list, list[str]]:
    """Collect standard Open WebUI attachment references in their original user turn."""
    text_and_images = []
    file_ids = {}
    image_urls = {}
    attachments = message.get('files') or []
    if not isinstance(attachments, list):
        raise HTTPException(400, 'Invalid attachment list in DeepSeek Native chat.')
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise HTTPException(400, 'Invalid attachment in DeepSeek Native chat.')
        attachment_type = attachment.get('type')
        if attachment_type == 'text' and isinstance(attachment.get('content'), str):
            name = attachment.get('name') or 'Attachment'
            text_and_images.append({'type': 'text', 'text': f'{name}\n{attachment["content"]}'})
        elif attachment_type in ('file', 'image') and isinstance(attachment.get('id'), str) and attachment['id']:
            file_ids[attachment['id']] = None
        elif attachment_type == 'image' and isinstance(attachment.get('url'), str):
            image_urls[attachment['url']] = None
        else:
            raise HTTPException(400, 'DeepSeek Native supports uploaded file and image attachments.')

    _collect_user_content(message.get('content'), text_and_images, image_urls)
    for url in image_urls:
        if url.startswith('data:'):
            text_and_images.append(_inline_image(url))
        elif url not in file_ids:
            file_ids[_image_file_id(url)] = None
    return text_and_images, list(file_ids)


async def _read_attachment_record(file_id: str, user: UserModel, extensions: set[str]) -> FileModel:
    file = await Files.get_file_by_id(file_id)
    if not file or (
        file.user_id != user.id and user.role != 'admin' and not await has_access_to_file(file_id, 'read', user)
    ):
        raise HTTPException(404, 'Attachment was not found or is unavailable.')
    if not file.path:
        raise HTTPException(404, 'Original attachment is unavailable.')
    if _file_format(file)[0] == 'document' and extensions:
        if Path(file.filename).suffix.lower().lstrip('.') not in extensions:
            raise HTTPException(400, 'This attachment extension is disabled by the Open WebUI administrator.')
    return file


async def prepare_deepseek_messages(messages: list[dict], key: str, user: UserModel) -> tuple[list[dict], bool]:
    """Resolve each attachment and return whether the Messages request needs Files API headers."""
    prepared = []
    attachment_turns = []
    for message in messages:
        if message.get('role') != 'user':
            prepared.append(message)
            continue
        parts, file_ids = _attachment_parts(message)
        clean = {k: v for k, v in message.items() if k != 'files'}
        if not isinstance(message.get('content'), str) or message.get('files'):
            clean['content'] = parts
        if file_ids:
            attachment_turns.append((parts, file_ids))
        prepared.append(clean)

    all_ids = dict.fromkeys(file_id for _, file_ids in attachment_turns for file_id in file_ids)
    if not all_ids:
        return prepared, False

    extensions = {
        ext.lower().lstrip('.')
        for ext in (await Config.get('rag.file.allowed_extensions') or [])
        if isinstance(ext, str)
    }
    files = {file_id: await _read_attachment_record(file_id, user, extensions) for file_id in all_ids}
    for parts, file_ids in attachment_turns:
        parts[:0] = [await _file_block(files[file_id], key, user.id) for file_id in file_ids]

    return prepared, True
