"""Resume original public OCI blobs, verify digests, then preload native Docker."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time

import httpx

from .retry_policy import transient_network_failure, retry_delay, RETRYABLE_STATUS

REPOSITORY = 'bugswarm/cached-images'
REGISTRY = 'https://registry-1.docker.io/v2/' + REPOSITORY
PIECE = 8 * 1024 * 1024


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return 'sha256:' + digest.hexdigest()


class BlobCache:
    def __init__(self, folder, client, workers=8, request_client_factory=None):
        self.folder, self.client = Path(folder), client
        self.request_client_factory = request_client_factory
        self.semaphore = asyncio.Semaphore(workers)
        self.auth_lock = asyncio.Lock()
        self.token, self.expires = None, 0
        self.retries = 0
        self.retry_errors = {}

    def record_retry(self, error):
        self.retries += 1
        kind = type(error).__name__
        status = getattr(error, 'status_code', None)
        if status is not None:
            kind += ':' + str(status)
        self.retry_errors[kind] = self.retry_errors.get(kind, 0) + 1

    async def token_response(self):
        kwargs = {'params': {'service': 'registry.docker.io', 'scope': 'repository:' + REPOSITORY + ':pull'}}
        if self.request_client_factory:
            async with self.request_client_factory() as client:
                return await client.get('https://auth.docker.io/token', **kwargs)
        return await self.client.get('https://auth.docker.io/token', **kwargs)

    @asynccontextmanager
    async def blob_response(self, digest, headers):
        if self.request_client_factory:
            # A failed proxy TLS handshake can leave an unusable pooled entry.
            # Closing the entire per-request client releases it on every exit.
            async with self.request_client_factory() as client:
                async with client.stream('GET', REGISTRY + '/blobs/' + digest, headers=headers) as response:
                    yield response
        else:
            async with self.client.stream('GET', REGISTRY + '/blobs/' + digest, headers=headers) as response:
                yield response

    async def authorization(self):
        async with self.auth_lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            attempt = 0
            while True:
                try:
                    response = await self.token_response()
                    if response.status_code in RETRYABLE_STATUS:
                        raise ConnectionError('Transient public token endpoint response')
                    response.raise_for_status()
                    break
                except Exception as error:
                    if not transient_network_failure(error):
                        raise
                    attempt += 1
                    self.record_retry(error)
                    await asyncio.sleep(retry_delay(attempt))
            data = response.json()
            self.token = data['token']
            self.expires = time.monotonic() + data.get('expires_in', 300) - 30
            return self.token

    async def piece(self, digest, total, start, end):
        folder = self.folder / 'pieces' / digest.split(':')[1]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{start}-{end}.part'
        attempt = 0
        authentication_failures = 0
        async with self.semaphore:
            while True:
                saved = path.stat().st_size if path.exists() else 0
                if saved == end - start + 1:
                    return path
                if saved > end - start + 1:
                    raise ValueError('Partial blob exceeds its exact range')
                offset = start + saved
                token = await self.authorization()
                try:
                    async with self.blob_response(digest, headers={
                        'Authorization': 'Bearer ' + token, 'Range': f'bytes={offset}-{end}',
                        'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'}) as response:
                        if response.status_code == 401:
                            authentication_failures += 1
                            if authentication_failures > 1:
                                raise RuntimeError('401 Unauthorized for original public image')
                            if self.token == token:
                                self.token = None
                            raise ConnectionError('Public pull token expired; refreshing')
                        if response.status_code in RETRYABLE_STATUS:
                            error = ConnectionError('Transient registry response ' + str(response.status_code))
                            error.status_code = response.status_code
                            raise error
                        response.raise_for_status()
                        expected = f'bytes {offset}-{end}/{total}'
                        if response.status_code == 206:
                            if response.headers.get('content-range') != expected:
                                raise ValueError('Registry returned a different byte range')
                        elif not (response.status_code == 200 and offset == 0 and end == total - 1):
                            raise ValueError('Registry does not honor byte-range resume')
                        with path.open('ab', buffering=0) as stream:
                            remaining = end - offset + 1
                            async for block in response.aiter_raw():
                                if len(block) > remaining:
                                    raise ValueError('Registry body exceeds requested byte range')
                                stream.write(block)
                                remaining -= len(block)
                            if remaining:
                                raise ConnectionError('Registry body ended before the requested range')
                except Exception as error:
                    if not transient_network_failure(error):
                        raise
                    attempt += 1
                    self.record_retry(error)
                    # Keep already-written bytes; no token or signed URL enters logs.
                    await asyncio.sleep(retry_delay(attempt))

    async def blob(self, descriptor):
        digest, size = descriptor['digest'], descriptor['size']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or size <= 0:
            raise ValueError('Invalid original image descriptor')
        destination = self.folder / 'blobs/sha256' / digest.split(':')[1]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.stat().st_size != size or digest_file(destination) != digest:
                raise ValueError('Existing cached blob failed digest verification')
            return destination
        pieces = await asyncio.gather(*(self.piece(digest, size, start, min(start + PIECE, size) - 1)
                                        for start in range(0, size, PIECE)))
        partial = destination.with_suffix('.assembling')
        with partial.open('wb') as stream:
            for piece in pieces:
                with piece.open('rb') as content:
                    shutil.copyfileobj(content, stream)
        if partial.stat().st_size != size or digest_file(partial) != digest:
            raise ValueError('Completed original blob failed digest verification')
        partial.replace(destination)
        shutil.rmtree(self.folder / 'pieces' / digest.split(':')[1])
        return destination


def load_original_image(spec, folder, log):
    manifest = spec['manifest_bytes'].encode()
    if 'sha256:' + hashlib.sha256(manifest).hexdigest() != spec['manifest_digest']:
        raise ValueError('Pinned original image manifest changed')
    config = json.loads((folder / 'blobs/sha256' / spec['manifest']['config']['digest'].split(':')[1]).read_text())
    reference = REPOSITORY + ':' + spec['tag']
    descriptor = {'mediaType': spec['manifest']['mediaType'], 'digest': spec['manifest_digest'],
        'size': len(manifest), 'annotations': {'containerd.io/distribution.source.docker.io': REPOSITORY,
            'io.containerd.image.name': 'docker.io/' + reference,
            'org.opencontainers.image.ref.name': spec['tag']}}
    index = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                        'manifests': [descriptor]}).encode()
    with log.open('ab') as output:
        child = subprocess.Popen(['docker', 'image', 'load', '--platform', config['os'] + '/' + config['architecture']],
            stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
        try:
            with tarfile.open(fileobj=child.stdin, mode='w|') as archive:
                def add(name, content):
                    item = tarfile.TarInfo(name)
                    item.size, item.mode = len(content), 0o644
                    archive.addfile(item, io.BytesIO(content))
                add('oci-layout', b'{"imageLayoutVersion":"1.0.0"}')
                add('index.json', index)
                add('blobs/sha256/' + spec['manifest_digest'].split(':')[1], manifest)
                unique = {d['digest']: d for d in [spec['manifest']['config'], *spec['manifest']['layers']]}
                for digest in unique:
                    path = folder / 'blobs/sha256' / digest.split(':')[1]
                    archive.add(path, arcname='blobs/sha256/' + digest.split(':')[1], recursive=False)
            child.stdin.close()
            if child.wait():
                raise RuntimeError('Docker rejected the verified original OCI image')
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait()
    inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference], text=True))[0]
    if (inspected['RootFS']['Layers'] != config['rootfs']['diff_ids']
            or inspected['Architecture'] != config['architecture'] or inspected['Os'] != config['os']):
        raise ValueError('Loaded image filesystem/platform differs from original config')
    return {'reference': reference, 'manifest_digest': spec['manifest_digest'],
            'image_id': inspected['Id'], 'platform': config['os'] + '/' + config['architecture'],
            'rootfs_diff_ids_verified': True, 'rootfs_diff_ids': config['rootfs']['diff_ids']}


async def main(run):
    specs = json.loads((run / 'image-resume-manifests.json').read_text())
    request = json.loads((run / 'IMAGE_RESUME_PREPARATION_REQUESTED.json').read_text())
    if request['tool_sha256'] != digest_file(Path(__file__)).split(':')[1]:
        raise ValueError('Image resume tool changed after preparation was requested')
    if request['manifests_sha256'] != digest_file(run / 'image-resume-manifests.json').split(':')[1]:
        raise ValueError('Original image manifests changed after preparation was requested')
    folder = run / 'image-resume-cache'
    folder.mkdir(exist_ok=True)
    state = {'pid': os.getpid(), 'status': 'running', 'current_task': None, 'imported': {},
             'scope': 'Original public images only; no Agent/task replay; per-blob SHA256 verified'}
    progress_path = run / 'image-resume-progress.json'
    if progress_path.exists():
        previous_state = json.loads(progress_path.read_text())
        state['imported'] = previous_state.get('imported', {})
        state['request_retries'] = previous_state.get('request_retries', 0)
        state['retry_errors'] = previous_state.get('retry_errors', {'historical_unspecified': state['request_retries']})
    def save():
        temporary = progress_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        temporary.replace(progress_path)
    def new_client():
        return httpx.AsyncClient(proxy='http://127.0.0.1:7897', trust_env=False,
            follow_redirects=True, timeout=httpx.Timeout(300, connect=15, pool=30),
            limits=httpx.Limits(max_keepalive_connections=0))
    async with new_client() as client:
        cache = BlobCache(folder, client, request_client_factory=new_client)
        cache.retries = state.get('request_retries', 0)
        cache.retry_errors = state.get('retry_errors', {})
        for spec in specs:
            if json.loads((run / 'IMAGE_RESUME_PREPARATION_REQUESTED.json').read_text())['status'] == 'cancelled':
                return
            previous = state['imported'].get(spec['task'])
            if previous:
                existing = subprocess.run(['docker', 'image', 'inspect', previous['reference']],
                                          capture_output=True, text=True)
                inspected = json.loads(existing.stdout)[0] if existing.returncode == 0 else None
                if inspected and inspected['RootFS']['Layers'] == previous['rootfs_diff_ids']:
                    continue
            state.update(current_task=spec['task'], stage='downloading', compressed_bytes=spec['compressed_bytes'])
            unique = {d['digest']: d for d in [spec['manifest']['config'], *spec['manifest']['layers']]}
            worker = asyncio.gather(*(cache.blob(d) for d in unique.values()))
            while not worker.done():
                if json.loads((run / 'IMAGE_RESUME_PREPARATION_REQUESTED.json').read_text())['status'] == 'cancelled':
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    state.update(status='cancelled', stage='cancelled')
                    save()
                    return
                def size(path):
                    try:
                        return path.stat().st_size
                    except FileNotFoundError:
                        return 0
                state['cached_bytes'] = sum(size(folder / 'blobs/sha256' / d.split(':')[1])
                    if (folder / 'blobs/sha256' / d.split(':')[1]).exists()
                    else sum(size(p) for p in (folder / 'pieces' / d.split(':')[1]).glob('*.part'))
                    for d in unique)
                state['request_retries'] = cache.retries
                state['retry_errors'] = cache.retry_errors
                save()
                await asyncio.sleep(5)
            await worker
            state['stage'] = 'loading_verified_oci'
            save()
            loaded = await asyncio.to_thread(load_original_image, spec, folder, run / 'image-resume-load.log')
            state['imported'][spec['task']] = loaded
            save()
            # Remove only this helper's verified duplicate download files after
            # Docker has loaded and verified the image. Native image/cache stays.
            for digest in unique:
                (folder / 'blobs/sha256' / digest.split(':')[1]).unlink(missing_ok=True)
        state.update(status='completed', stage='finished')
        save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.run.resolve()))
    except Exception as error:
        (args.run / 'image-resume-error.json').write_text(json.dumps({'error_type': type(error).__name__}) + '\n')
        raise RuntimeError('Image cache preparation failed: ' + type(error).__name__) from None
