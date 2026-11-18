import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from scripts.skillsbench.image_cache import BlobCache


class Content(httpx.AsyncByteStream):
    def __init__(self, data, fail=False):
        self.data, self.fail = data, fail

    async def __aiter__(self):
        yield self.data
        if self.fail:
            raise httpx.ReadError('connection reset')


class ImageResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_failed_and_successful_request_client_is_closed(self):
        data = b'abcdefgh'
        descriptor = {'size': 8, 'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
        clients, ranges = [], []
        def handle(request):
            if request.url.host == 'auth.docker.io':
                return httpx.Response(200, json={'token': 'test-public-token', 'expires_in': 300})
            ranges.append(request.headers['range'])
            if len(ranges) == 1:
                return httpx.Response(206, headers={'content-range': 'bytes 0-7/8'}, stream=Content(b'abcd', fail=True))
            return httpx.Response(206, headers={'content-range': 'bytes 4-7/8'}, stream=Content(b'efgh'))
        def factory():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            clients.append(client)
            return client
        with tempfile.TemporaryDirectory() as temporary:
            async with factory() as metadata_client:
                cache = BlobCache(temporary, metadata_client, request_client_factory=factory)
                with patch('scripts.skillsbench.image_cache.asyncio.sleep', new_callable=AsyncMock):
                    result = await cache.blob(descriptor)
                self.assertEqual(result.read_bytes(), data)
            self.assertTrue(all(client.is_closed for client in clients))
            self.assertEqual(ranges, ['bytes=0-7', 'bytes=4-7'])

    async def test_interrupted_original_blob_resumes_at_exact_written_offset_and_checks_digest(self):
        data = b'abcdefgh'
        descriptor = {'size': len(data), 'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
        ranges = []
        def handle(request):
            if request.url.host == 'auth.docker.io':
                return httpx.Response(200, json={'token': 'test-public-token', 'expires_in': 300})
            value = request.headers['range']
            ranges.append(value)
            if len(ranges) == 1:
                return httpx.Response(206, headers={'content-range': 'bytes 0-7/8'}, stream=Content(b'abcd', fail=True))
            self.assertEqual(value, 'bytes=4-7')
            return httpx.Response(206, headers={'content-range': 'bytes 4-7/8'}, stream=Content(b'efgh'))
        with tempfile.TemporaryDirectory() as temporary:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                cache = BlobCache(temporary, client)
                with patch('scripts.skillsbench.image_cache.asyncio.sleep', new_callable=AsyncMock):
                    path = await cache.blob(descriptor)
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(ranges, ['bytes=0-7', 'bytes=4-7'])
                self.assertEqual(cache.retries, 1)
                await cache.blob(descriptor)
                self.assertEqual(len(ranges), 2)  # A verified cached blob needs no network replay.

    async def test_full_size_wrong_digest_is_rejected_without_producing_verified_blob(self):
        expected = b'abcdefgh'
        descriptor = {'size': 8, 'digest': 'sha256:' + hashlib.sha256(expected).hexdigest()}
        def handle(request):
            if request.url.host == 'auth.docker.io':
                return httpx.Response(200, json={'token': 'test-public-token', 'expires_in': 300})
            return httpx.Response(206, headers={'content-range': 'bytes 0-7/8'}, stream=Content(b'wrongxxx'))
        with tempfile.TemporaryDirectory() as temporary:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with self.assertRaisesRegex(ValueError, 'digest verification'):
                    await BlobCache(temporary, client).blob(descriptor)
            self.assertFalse((Path(temporary) / 'blobs/sha256' / descriptor['digest'].split(':')[1]).exists())
