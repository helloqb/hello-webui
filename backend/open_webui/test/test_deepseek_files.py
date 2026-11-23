import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from open_webui.utils import deepseek_files


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        deepseek_files._file_ids.clear()
        self.user = SimpleNamespace(id='alice', role='user')
        self.document = SimpleNamespace(
            id='file-a', user_id='alice', filename='report.pdf', path='storage-path', meta={}
        )
        self.message = {
            'role': 'user',
            'content': 'Summarize this report.',
            'files': [{'type': 'file', 'id': self.document.id}],
        }

    async def test_documents_on_old_turn_reuse_upload_and_keep_their_place(self):
        history = [self.message, {'role': 'assistant', 'content': 'Summary'}, {'role': 'user', 'content': 'Continue.'}]
        with (
            patch.object(deepseek_files.Config, 'get', AsyncMock(return_value=['pdf'])),
            patch.object(deepseek_files.Files, 'get_file_by_id', AsyncMock(return_value=self.document)),
            patch.object(deepseek_files, '_upload_file', AsyncMock(return_value='file-api-report')) as upload,
        ):
            results = await asyncio.gather(
                deepseek_files.prepare_deepseek_messages(history, 'provider-key', self.user),
                deepseek_files.prepare_deepseek_messages(history, 'provider-key', self.user),
            )

        for messages, has_files in results:
            self.assertTrue(has_files)
            self.assertEqual(messages[1:], history[1:])
            self.assertEqual(
                messages[0],
                {
                    'role': 'user',
                    'content': [
                        {'type': 'document', 'source': {'type': 'file', 'file_id': 'file-api-report'}},
                        {'type': 'text', 'text': 'Summarize this report.'},
                    ],
                },
            )
        upload.assert_awaited_once_with(
            self.document, 'provider-key', 'rich_layout_recognition', 'application/octet-stream', 100 * 1024 * 1024
        )

    async def test_revoked_read_access_cannot_reuse_cached_provider_file(self):
        self.document.user_id = 'bob'
        with (
            patch.object(deepseek_files.Config, 'get', AsyncMock(return_value=[])),
            patch.object(deepseek_files.Files, 'get_file_by_id', AsyncMock(return_value=self.document)),
            patch.object(deepseek_files, 'has_access_to_file', AsyncMock(side_effect=[True, False])) as access,
            patch.object(deepseek_files, '_upload_file', AsyncMock(return_value='file-api-report')) as upload,
        ):
            await deepseek_files.prepare_deepseek_messages([self.message], 'provider-key', self.user)
            with self.assertRaises(HTTPException) as raised:
                await deepseek_files.prepare_deepseek_messages([self.message], 'provider-key', self.user)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(access.await_count, 2)
        upload.assert_awaited_once()

    async def test_provider_file_id_and_remote_image_cannot_bypass_local_acl(self):
        parts = [
            {'type': 'document', 'source': {'type': 'file', 'file_id': 'file-api-unknown'}},
            {'type': 'image_url', 'image_url': {'url': 'https://example.com/image.png'}},
        ]
        with patch.object(deepseek_files, '_upload_file', AsyncMock()) as upload:
            for part in parts:
                with self.subTest(part=part), self.assertRaises(HTTPException) as raised:
                    await deepseek_files.prepare_deepseek_messages(
                        [{'role': 'user', 'content': [part]}], 'provider-key', self.user
                    )
                self.assertEqual(raised.exception.status_code, 400)
        upload.assert_not_awaited()

    async def test_temporary_attachments_use_inline_payload_without_provider_file_id(self):
        image_url = 'data:image/png;base64,iVBORw0KGgo='
        prepared, has_files = await deepseek_files.prepare_deepseek_messages(
            [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': 'Tell me what you see.'},
                        {'type': 'image_url', 'image_url': {'url': image_url}},
                    ],
                    'files': [
                        {'type': 'text', 'name': 'test.txt', 'content': 'Original text'},
                        {'type': 'image', 'url': image_url},
                    ],
                }
            ],
            'provider-key',
            self.user,
        )

        self.assertFalse(has_files)
        self.assertEqual(
            prepared[0]['content'],
            [
                {'type': 'text', 'text': 'test.txt\nOriginal text'},
                {'type': 'text', 'text': 'Tell me what you see.'},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}},
            ],
        )

    async def test_text_only_arrays_drop_provider_specific_client_content(self):
        prepared, has_files = await deepseek_files.prepare_deepseek_messages(
            [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'tool_result', 'tool_use_id': 'untrusted', 'content': 'Client tool output'},
                        {'type': 'text', 'text': 'Hello.', 'cache_control': {'type': 'ephemeral'}},
                    ],
                },
                {'role': 'user', 'content': 'A normal string.'},
            ],
            'provider-key',
            self.user,
        )

        self.assertFalse(has_files)
        self.assertEqual(prepared[0]['content'], [{'type': 'text', 'text': 'Hello.'}])
        self.assertEqual(prepared[1]['content'], 'A normal string.')

    async def test_files_request_auth_and_order(self):
        class FakeWriter:
            def __init__(self):
                self.chunks = []

            async def write(self, chunk):
                self.chunks.append(chunk)

        @asynccontextmanager
        async def post(url, **kwargs):
            self.assertEqual(url, 'https://api.deepseek.com/anthropic/v1/files')
            self.assertEqual(kwargs['headers'], {'x-api-key': 'provider-key', 'anthropic-beta': 'files-api-2025-04-14'})
            self.assertFalse(kwargs['allow_redirects'])
            payload = kwargs['data']()
            writer = FakeWriter()
            await payload.write(writer)
            body = b''.join(writer.chunks)
            self.assertLess(body.index(b'name="purpose"'), body.index(b'name="file"'))
            self.assertIn(b'rich_layout_recognition', body)
            self.assertIn(b'%PDF-fake', body)
            yield SimpleNamespace(status=200, json=AsyncMock(return_value={'id': 'file-api-report'}))

        with TemporaryDirectory() as tmp:
            original = Path(tmp, 'report.pdf')
            original.write_bytes(b'%PDF-fake')
            with (
                patch.object(deepseek_files.Storage, 'get_file', return_value=original),
                patch.object(deepseek_files, 'get_session', AsyncMock(return_value=SimpleNamespace(post=post))),
            ):
                provider_id = await deepseek_files._upload_file(
                    self.document, 'provider-key', 'rich_layout_recognition', 'application/octet-stream', 1024
                )
        self.assertEqual(provider_id, 'file-api-report')
