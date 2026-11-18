"""Recovery stays inside the failed operation, never around a task rollout."""

import ast
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import shlex
import ssl
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scripts.skillsbench.retry_policy import transient_network_failure
from scripts.skillsbench.step_retries import InstallEnvironment, install_policy, install_step_wrapper, method_request_retries, request_retry_install_command, retry_operation
from scripts.skillsbench.openhands_request_retry import check_stream_transport, extend_retry_decorator


class RecoveryPolicyTests(unittest.TestCase):
    def test_pip_missing_version_requires_same_package_index_transport_failure(self):
        lookup = ('Could not fetch URL https://pypi.org/simple/qutip/: '
                  '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol\n')
        missing = ('ERROR: Could not find a version that satisfies the requirement qutip==4.7.6 (from versions: none)\n'
                   'ERROR: No matching distribution found for qutip==4.7.6')
        self.assertTrue(transient_network_failure(lookup + missing))
        self.assertFalse(transient_network_failure(missing))
        self.assertFalse(transient_network_failure(lookup.replace('/qutip/', '/pip/') + missing))
        self.assertFalse(transient_network_failure(lookup + missing + '\ncertificate verify failed'))
        self.assertFalse(transient_network_failure(lookup + missing + '\n403 Forbidden'))
        self.assertFalse(transient_network_failure(lookup + missing + '\nNo matching distribution found for absent'))

    def test_registry_eof_and_deadline_do_not_retry_agent_limits_or_bad_inputs(self):
        for message in (
            'failed to fetch anonymous token: Get "https://auth.docker.io/token": EOF',
            'failed to do request: Head "https://gcr.io/v2/image/manifests/latest": EOF',
            '#3 ERROR: DeadlineExceeded: context deadline exceeded',
        ):
            self.assertTrue(transient_network_failure(message))
            self.assertFalse(transient_network_failure(message + '\ncertificate verify failed'))
            self.assertFalse(transient_network_failure(message + '\nno space left on device'))
        for message in ('EOF while parsing JSON', 'Agent prompt exceeded wall-clock budget 900s',
                        'Verifier timed out after 900 seconds', 'context length exceeded'):
            self.assertFalse(transient_network_failure(message))

    def test_response_carried_http_status_controls_network_retry(self):
        from types import SimpleNamespace
        for status, expected in ((502, True), (503, True), (429, True), (401, False), (403, False), (404, False)):
            error = RuntimeError('HTTP response error')
            error.response = SimpleNamespace(status_code=status)
            self.assertEqual(transient_network_failure(error), expected)

    def test_install_policy_is_scoped_to_the_native_config_ancestors(self):
        self.assertIsNone(install_policy([]))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = root / 'task/backend/config.json'
            config.parent.mkdir(parents=True)
            self.assertIsNone(install_policy(['--config', str(config)]))
            policy = {'total_timeout_seconds': None}
            (root / 'INSTALL_POLICY_REQUESTED.json').write_text(json.dumps(policy))
            self.assertEqual(install_policy(['--config', str(config)]), policy)
            local = {'total_timeout_seconds': 12}
            (config.parent / 'INSTALL_POLICY_REQUESTED.json').write_text(json.dumps(local))
            self.assertEqual(install_policy(['--config', str(config)]), local)

    def test_observed_preparation_network_errors_are_retryable(self):
        for text in ('Connection failed [IP: 91.189.91.102 80]', '502 Bad Gateway', '502  Bad Gateway',
                     'Unable to connect to mirrors.tuna.tsinghua.edu.cn:http:',
                     'error: RPC failed; curl 56 GnuTLS recv error (-9): Error decoding the received TLS packet.\nfatal: early EOF\nfatal: fetch-pack: invalid index-pack output',
                     'curl: (18) transfer closed with outstanding read data remaining',
                     "fatal: unable to access 'https://github.com/OpenHands/OpenHands-CLI.git/': gnutls_handshake() failed: The TLS connection was non-properly terminated.",
                     'OSError: I/O error: I/O error: error decoding response body',
                     "Error: Client network socket disconnected before secure TLS connection was established; code: 'ECONNRESET'",
                     'Something wicked happened resolving ports.ubuntu.com',
                     'failed to fetch anonymous token: dial tcp: i/o timeout',
                     'requests.exceptions.SSLError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol',
                     'ssl.SSLEOFError: connection ended during TLS handshake',
                     'ConnectionError: Network error: Request middleware error: error sending request for url (https://us.aws.cdn.hf.co/xorbs/default/object)',
                     'ReadTimeoutError: Read timed out', 'Connection reset by peer'):
            self.assertTrue(transient_network_failure(RuntimeError(text)), text)

    def test_git_sideband_disconnect_retries_without_treating_all_pack_errors_as_network(self):
        observed = ('error: 2696 bytes of body are still expected\n'
                    'fetch-pack: unexpected disconnect while reading sideband packet\n'
                    'fatal: early EOF\nfatal: fetch-pack: invalid index-pack output')
        self.assertTrue(transient_network_failure(observed))
        self.assertFalse(transient_network_failure('fatal: fetch-pack: invalid index-pack output'))
        self.assertFalse(transient_network_failure(observed + '\nno space left on device'))

    def test_playwright_download_transport_errors_preserve_permanent_exclusions(self):
        for message in (
            'Error: Request to https://playwright.azureedge.net/builds/chromium/1148/chromium-linux-arm64.zip timed out after 30000ms',
            'Error: Download failed: server closed connection. URL: https://playwright-akamai.azureedge.net/builds/chromium/1148/chromium-linux-arm64.zip',
        ):
            self.assertTrue(transient_network_failure(message))
            self.assertFalse(transient_network_failure(message + '\ncertificate verify failed'))
        self.assertFalse(transient_network_failure('Agent timed out after 900s'))

    def test_permanent_errors_and_cancellation_are_not_network_retries(self):
        for error in (RuntimeError('Unable to locate package xyz'),
                      RuntimeError('no matching manifest for linux/arm64'),
                      RuntimeError('no space left on device'),
                      RuntimeError('SSL certificate problem: self-signed certificate'),
                      RuntimeError('certificate verify failed; SSLEOFError: UNEXPECTED_EOF_WHILE_READING'),
                      RuntimeError('fatal: fetch-pack: invalid index-pack output'),
                      ValueError('invalid tool arguments'), asyncio.CancelledError(), KeyboardInterrupt()):
            self.assertFalse(transient_network_failure(error), repr(error))
        try:
            try:
                raise ssl.SSLCertVerificationError('certificate verify failed')
            except ssl.SSLCertVerificationError as cause:
                raise ConnectionError('connection error') from cause
        except ConnectionError as error:
            self.assertFalse(transient_network_failure(error))

    def test_status_codes_override_text(self):
        for status, expected in ((401, False), (403, False), (400, False), (429, True), (502, True)):
            error = RuntimeError('connection timed out')
            error.status_code = status
            self.assertEqual(transient_network_failure(error), expected)

    def test_missing_package_after_lost_index_is_a_network_failure(self):
        for detail in ("Could not resolve 'ports.ubuntu.com'", 'Connection failed [IP: 91.189.91.104 80]'):
            message = ('W: Failed to fetch http://ports.ubuntu.com/ubuntu-ports/dists/noble/universe/Packages '
                       + detail + '\nE: Unable to locate package tesseract-ocr')
            self.assertTrue(transient_network_failure(RuntimeError(message)))
        self.assertFalse(transient_network_failure(RuntimeError('E: Unable to locate package truly-absent')))

    def test_sdkman_offline_message_does_not_prove_a_missing_java_version(self):
        # SDKMAN emits this branch before querying version availability when
        # its candidates-service healthcheck was empty or returned HTML.
        for text in ('==== INTERNET NOT REACHABLE! ====',
                     'Stop! java 8.0.392-tem is not available. Internet unreachable.'):
            self.assertTrue(transient_network_failure(text))
        self.assertFalse(transient_network_failure(
            'Stop! java 8.0.392-tem is not available. Possible causes: invalid version or platform.'))
        self.assertFalse(transient_network_failure(
            'Internet unreachable. SSL certificate problem: self-signed certificate'))

    def test_curl_network_protocol_failures_and_local_errors_stay_distinct(self):
        observed = ('error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly: CANCEL (err 8)\n'
                    'error: 7038 bytes of body are still expected\nfatal: early EOF\n'
                    'fatal: fetch-pack: invalid index-pack output')
        self.assertTrue(transient_network_failure(observed))
        for code in (5, 6, 7, 16, 18, 28, 35, 52, 55, 56, 92, 95, 96):
            self.assertTrue(transient_network_failure(f'curl: ({code}) download failed'), code)
        for code in (23, 26, 27, 47, 58, 59, 60, 67, 77, 90, 91, 94, 98):
            self.assertFalse(transient_network_failure(f'curl: ({code}) download failed'), code)

    def test_partial_pip_transfer_retries_without_accepting_a_bad_hash(self):
        error = 'ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\nExpected sha256 original\nGot other'
        self.assertTrue(transient_network_failure('412.1/513.0 MB\n' + error))
        self.assertFalse(transient_network_failure('513.0/513.0 MB\n' + error))
        self.assertFalse(transient_network_failure(error))
        self.assertFalse(transient_network_failure('0.1/513.0 MB\n513.0/513.0 MB\n' + error))

    def test_installed_pth_has_a_real_newline(self):
        code = shlex.split(request_retry_install_command())[2]
        writes = [node for node in ast.walk(ast.parse(code))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == 'write_text']
        startup = ast.literal_eval(writes[0].args[0])
        self.assertTrue(startup.endswith('\n'))
        self.assertNotIn('\\n', startup)
        self.assertIn('install_or_exit()', startup)


class PreparationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_buildkit_single_connection_transport_failure_is_preparation_only(self):
        message = 'Unavailable: connection error: desc = "transport: Error while dialing: only one connection allowed"'
        operation = AsyncMock(side_effect=[RuntimeError(message), 'ready'])
        with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
            self.assertEqual(await retry_operation(operation, 'docker_startup'), 'ready')
        for stage, error in (('agent_execution', message), ('docker_build', message + '; permission denied')):
            operation = AsyncMock(side_effect=RuntimeError(error))
            with self.assertRaises(RuntimeError):
                await retry_operation(operation, stage)
            self.assertEqual(operation.await_count, 1)

    async def test_registry_content_lease_retries_only_preparation(self):
        message = 'failed to resolve source metadata: unable to lease content: lease does not exist: not found'
        for stage in ('docker_build', 'docker_startup'):
            operation = AsyncMock(side_effect=[RuntimeError(message), 'ready'])
            with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
                self.assertEqual(await retry_operation(operation, stage), 'ready')
            self.assertEqual(operation.await_count, 2)
        for stage, error in (('agent_execution', message), ('docker_build', message + '; no space left on device')):
            operation = AsyncMock(side_effect=RuntimeError(error))
            with self.assertRaises(RuntimeError):
                await retry_operation(operation, stage)
            self.assertEqual(operation.await_count, 1)

    async def test_lost_build_cache_lease_retries_preparation_without_retrying_agent(self):
        error = RuntimeError('failed to solve: failed to compute cache key: lease does not exist: not found')
        operation = AsyncMock(side_effect=[error, 'rebuilt'])
        with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
            self.assertEqual(await retry_operation(operation, 'docker_build'), 'rebuilt')
        operation = AsyncMock(side_effect=error)
        with self.assertRaises(RuntimeError):
            await retry_operation(operation, 'agent_execution')
        self.assertEqual(operation.await_count, 1)

    async def test_container_removal_race_retries_only_pre_agent_startup(self):
        error = RuntimeError('Error response from daemon: container is marked for removal and cannot be started')
        operation = AsyncMock(side_effect=[error, error, 'ready'])
        with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
            self.assertEqual(await retry_operation(operation, 'docker_startup'), 'ready')
        self.assertEqual(operation.await_count, 3)
        operation = AsyncMock(side_effect=error)
        with self.assertRaises(RuntimeError):
            await retry_operation(operation, 'agent_execution')
        self.assertEqual(operation.await_count, 1)

    async def test_uncapped_install_has_no_container_or_exec_deadline(self):
        env = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(return_code=0)))
        proxy = InstallEnvironment(env, 'installer')
        await proxy.exec('installer', timeout_sec=None)
        command = env.exec.call_args.args[0]
        self.assertIn('flock --exclusive', command)
        self.assertNotIn('timeout --verbose', command)
        self.assertIsNone(env.exec.call_args.kwargs['timeout_sec'])

    async def test_more_than_five_failures_retry_same_operation_and_state(self):
        state = {'completed_step': 4}
        identities = []

        async def operation():
            identities.append(id(state))
            if len(identities) <= 7:
                raise ConnectionError('connection reset by peer')
            return state

        with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await retry_operation(operation, 'build')
        self.assertIs(result, state)
        self.assertEqual(state, {'completed_step': 4})
        self.assertEqual(len(identities), 8)
        self.assertEqual(len(set(identities)), 1)
        self.assertEqual(sleep.await_count, 7)

    async def test_permanent_error_and_cancellation_propagate_without_retry(self):
        for error in (RuntimeError('Unable to locate package nope'), asyncio.CancelledError()):
            operation = AsyncMock(side_effect=error)
            with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaises(type(error)):
                    await retry_operation(operation, 'build')
            self.assertEqual(operation.await_count, 1)
            sleep.assert_not_awaited()

    async def test_install_retry_preserves_container_and_lock_identity(self):
        calls = []
        env = SimpleNamespace()

        async def execute(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                raise RuntimeError('Command timed out after 1815 seconds')
            return SimpleNamespace(return_code=0, stdout='ready', stderr='')

        env.exec = execute

        async def native(proxy, agent, rollout_dir, **kwargs):
            self.assertIs(proxy.env, env)
            return await proxy.exec('install exactly this', timeout_sec=1800)

        with tempfile.TemporaryDirectory() as folder, \
             patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
            wrapped = install_step_wrapper(native, {'openhands': 'install exactly this'})
            result = await wrapped(env, 'openhands', Path(folder))
        self.assertEqual(result.stdout, 'ready')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertIn('flock --exclusive', calls[0][0])
        self.assertIn('1800s', calls[0][0])

    async def test_tool_actions_are_not_wrapped_or_replayed(self):
        env = SimpleNamespace(exec=AsyncMock(return_value='done'))
        proxy = InstallEnvironment(env, 'installer')
        self.assertEqual(await proxy.exec('mutate task output', timeout_sec=12), 'done')
        env.exec.assert_awaited_once_with('mutate task output', timeout_sec=12)


class MethodRequestRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_and_async_method_clients_retry_same_http_payload(self):
        import httpx
        from openai import OpenAI, AsyncOpenAI
        from openai._base_client import SyncAPIClient
        native = SyncAPIClient.request
        bodies = []

        def handler(request):
            bodies.append(request.content)
            if len(bodies) <= 7:
                return httpx.Response(502, json={'error': {'message': 'temporary upstream failure'}})
            return httpx.Response(200, json={'id': 'ok', 'object': 'chat.completion', 'created': 1,
                'model': 'method', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]})

        with method_request_retries(), patch('scripts.skillsbench.step_retries.time.sleep'), \
             patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
            with OpenAI(api_key='fixture', base_url='https://fixture.invalid/v1', max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                result = client.chat.completions.create(model='method', messages=[{'role': 'user', 'content': 'same step'}])
            self.assertEqual(result.choices[0].message.content, 'OK')
            self.assertEqual(len(bodies), 8)
            self.assertEqual(len(set(bodies)), 1)
            bodies.clear()
            async with AsyncOpenAI(api_key='fixture', base_url='https://fixture.invalid/v1', max_retries=0,
                                   http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
                result = await client.chat.completions.create(model='method', messages=[{'role': 'user', 'content': 'same step'}])
            self.assertEqual(result.choices[0].message.content, 'OK')
            self.assertEqual(len(bodies), 8)
            self.assertEqual(len(set(bodies)), 1)
        self.assertIs(SyncAPIClient.request, native)


def pinned_mixin():
    """Load the verified upstream module, stubbing only its logger/error imports."""
    path = os.environ.get('SKILLPATCH_TEST_RETRY_MIXIN')
    if not path or not importlib.util.find_spec('tenacity'):
        raise unittest.SkipTest('Provide verified SDK RetryMixin source and Tenacity for this integration check')
    modules = {name: ModuleType(name) for name in (
        'openhands', 'openhands.sdk', 'openhands.sdk.llm',
        'openhands.sdk.llm.exceptions', 'openhands.sdk.logger')}
    for module in modules.values():
        module.__path__ = []
    modules['openhands.sdk.llm.exceptions'].LLMNoResponseError = type('LLMNoResponseError', (Exception,), {})
    modules['openhands.sdk.logger'].get_logger = logging.getLogger
    spec = importlib.util.spec_from_file_location('verified_sdk_retry_mixin', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.RetryMixin, modules['openhands.sdk.llm.exceptions'].LLMNoResponseError


class OpenHandsRequestRetryTests(unittest.TestCase):
    def test_native_sdk_retries_transport_more_than_five_times_and_executes_tool_once(self):
        mixin, no_response = pinned_mixin()
        native = mixin.retry_decorator
        mixin.retry_decorator = extend_retry_decorator(native)
        client = mixin()
        request = {'messages': [{'role': 'user', 'content': 'Continue step 4'}], 'temperature': 0}
        sent, tools = [], []

        @client.retry_decorator(num_retries=5, retry_exceptions=(no_response,), retry_min_wait=0, retry_max_wait=0, retry_multiplier=0)
        def same_request(**kwargs):
            sent.append(json.loads(json.dumps(request)))
            if len(sent) <= 7:
                raise TimeoutError('request timed out')
            return {'tool_call': 'write result once'}

        response = same_request()
        tools.append(response['tool_call'])
        self.assertEqual(len(sent), 8)
        self.assertTrue(all(value == request for value in sent))
        self.assertEqual(tools, ['write result once'])

    def test_stream_without_terminal_finish_is_retried_inside_request(self):
        mixin, no_response = pinned_mixin()
        mixin.retry_decorator = extend_retry_decorator(mixin.retry_decorator)
        calls = []

        def transport(self, **kwargs):
            calls.append(kwargs)
            return {'choices': [{'finish_reason': None if len(calls) == 1 else 'tool_calls'}]}

        wrapped = check_stream_transport(transport)
        client = mixin()

        @client.retry_decorator(num_retries=5, retry_exceptions=(no_response,), retry_min_wait=0, retry_max_wait=0, retry_multiplier=0)
        def request():
            return wrapped(client, enable_streaming=True, messages=['same saved conversation'])

        self.assertEqual(request()['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(calls[0], calls[1])

    def test_native_non_network_retry_policy_is_preserved(self):
        mixin, no_response = pinned_mixin()
        mixin.retry_decorator = extend_retry_decorator(mixin.retry_decorator)
        client = mixin()
        calls = []

        @client.retry_decorator(num_retries=5, retry_exceptions=(no_response,), retry_min_wait=0, retry_max_wait=0, retry_multiplier=0)
        def invalid_request():
            calls.append(True)
            raise ValueError('invalid API request')

        with self.assertRaises(ValueError):
            invalid_request()
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
