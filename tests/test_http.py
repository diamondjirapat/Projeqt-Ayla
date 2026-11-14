from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from utils.http import MAX_RESPONSE_BYTES, safe_http_request


class SafeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, responses, **kwargs):
        self.calls = []

        @asynccontextmanager
        async def request(method, url, **options):
            self.calls.append((method, url, options))
            yield responses.pop(0)

        @asynccontextmanager
        async def session(**options):
            yield SimpleNamespace(request=request)

        with (
            patch('utils.http.resolve_public_url', new=AsyncMock(return_value=('example.com', ['93.184.216.34']))),
            patch('utils.http.aiohttp.TCPConnector'),
            patch('utils.http.aiohttp.ClientSession', side_effect=session),
        ):
            return await safe_http_request('POST', 'https://example.com/start', allowed_domains=[], **kwargs)

    def response(self, chunks=(), status=200, headers=None):
        async def iter_chunked(size):
            for chunk in chunks:
                yield chunk

        return SimpleNamespace(status=status, headers=headers or {}, content=SimpleNamespace(iter_chunked=iter_chunked))

    async def test_reads_all_chunks_before_returning(self):
        result = await self.fetch([self.response([b'{"message":', b'"complete"}'])])
        self.assertEqual(result[2], b'{"message":"complete"}')

    async def test_enforces_the_limit_across_multiple_chunks(self):
        with self.assertRaisesRegex(ValueError, '2 MB'):
            await self.fetch([self.response([b'x' * MAX_RESPONSE_BYTES, b'y'])])

    async def test_larger_bounded_responses_can_be_requested_without_changing_the_default(self):
        result = await self.fetch(
            [self.response([b'x' * MAX_RESPONSE_BYTES, b'y'])], max_response_bytes=MAX_RESPONSE_BYTES + 1
        )
        self.assertEqual(len(result[2]), MAX_RESPONSE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'limit'):
            await self.fetch([self.response([b'x' * 11])], max_response_bytes=10)
        with self.assertRaisesRegex(ValueError, '8 MB'):
            await self.fetch([], max_response_bytes=9 * 1024 * 1024)

    async def test_redirect_to_another_origin_drops_credentials(self):
        await self.fetch(
            [
                self.response(status=307, headers={'Location': 'https://other.example/final'}),
                self.response([b'ok']),
            ],
            headers={
                'Authorization': 'Bearer secret',
                'COOKIE': 'session=secret',
                'Host': 'example.com',
                'Accept': 'text/plain',
            },
        )
        self.assertEqual(self.calls[1][2]['headers'], {'Accept': 'text/plain'})
        self.assertEqual(self.calls[1][0], 'POST')

    async def test_303_redirect_uses_get_without_the_original_body(self):
        await self.fetch(
            [
                self.response(status=303, headers={'Location': '/final'}),
                self.response([b'ok']),
            ],
            body=b'payload',
            headers={'Content-Type': 'application/json', 'Content-Length': '7'},
        )
        method, url, options = self.calls[1]
        self.assertEqual(method, 'GET')
        self.assertIsNone(options['data'])
        self.assertIsNone(options['json'])
        self.assertEqual(options['headers'], {})
