"""Transient model retries preserve requests and never retry invalid outputs."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError

from excutor.eviskill_runners.launcher import ChatClient, run_native
from set_up.native_config import NativeRunConfig, load_native_config


class NativeModelRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = NativeRunConfig(
            'appworld', self.root / 'tasks.json', None, 'fixture-model',
            'FIXTURE_API_KEY', 'FIXTURE_BASE_URL', 'https://model.example/v1',
            0.0, 4096, 30, {'data_dir': str(self.root)}, self.root / 'output',
        )
        self.environment = {'FIXTURE_API_KEY': 'fixture-test-secret'}
        self.messages = [{'role': 'user', 'content': 'Generate the next action.'}]
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    @staticmethod
    def response(content='print(1)', finish_reason='stop'):
        return io.BytesIO(json.dumps({'choices': [{'finish_reason': finish_reason,
                              'message': {'content': content}}]}).encode())

    @staticmethod
    def http_error(status):
        return HTTPError('https://model.example/v1/chat/completions', status,
                         'fixture HTTP error', {}, io.BytesIO(b'fixture error body'))

    def client(self, retries):
        return ChatClient(replace(self.config, max_retries=retries), self.environment)

    @staticmethod
    def streaming_response(done=True):
        chunks = [{'choices': [{'delta': {'content': 'print('}, 'finish_reason': None}]},
                  {'choices': [{'delta': {'content': '1)'}, 'finish_reason': None}]},
                  {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
        text = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
        return io.BytesIO((text + ('data: [DONE]\n\n' if done else '')).encode())

    @staticmethod
    def stream_required_error():
        body = json.dumps({'error': {'message': '非流式请求已禁用，请使用 stream=true'}}, ensure_ascii=False).encode()
        return HTTPError('https://model.example/v1/chat/completions', 400, 'Bad Request', {}, io.BytesIO(body))

    def test_explicit_stream_requirement_negotiates_without_changing_model_parameters(self):
        client = self.client(2)
        with patch('excutor.eviskill_runners.launcher.urlopen', side_effect=[
                self.stream_required_error(), self.streaming_response(), self.streaming_response()]) as request:
            self.assertEqual(client.chat(self.messages), 'print(1)')
            self.assertEqual(client.chat(self.messages), 'print(1)')
        bodies = [json.loads(call.args[0].data) for call in request.call_args_list]
        self.assertEqual({**bodies[0], 'stream': True}, bodies[1])
        self.assertEqual(bodies[1], bodies[2])
        self.assertEqual((client.request_attempts, client.retry_count), (3, 0))

    def test_interrupted_stream_is_retried_in_full_and_not_returned_as_partial_action(self):
        client = self.client(1)
        with patch('excutor.eviskill_runners.launcher.urlopen', side_effect=[
                self.stream_required_error(), self.streaming_response(False), self.streaming_response()]), \
             patch('excutor.eviskill_runners.launcher.time.sleep'):
            self.assertEqual(client.chat(self.messages), 'print(1)')
        self.assertEqual(client.retry_count, 1)

    def test_other_bad_requests_do_not_enable_streaming(self):
        client = self.client(2)
        with patch('excutor.eviskill_runners.launcher.urlopen', side_effect=self.http_error(400)) as request:
            with self.assertRaisesRegex(ValueError, '400'):
                client.chat(self.messages)
        self.assertFalse(client.stream_required)
        self.assertEqual(request.call_count, 1)

    def test_stream_can_be_selected_by_execution_role_configuration(self):
        client = ChatClient(self.config, {**self.environment, 'EXECUTION_STREAM': 'true'})
        with patch('excutor.eviskill_runners.launcher.urlopen', return_value=self.streaming_response()) as request:
            self.assertEqual(client.chat(self.messages), 'print(1)')
        self.assertTrue(json.loads(request.call_args.args[0].data)['stream'])

    def test_transient_errors_recover_with_identical_request(self):
        errors = [URLError('TLS EOF'), TimeoutError('timeout'), ConnectionError('reset')]
        errors.extend(self.http_error(status) for status in (408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530))
        for error in errors:
            with self.subTest(error=type(error).__name__, status=getattr(error, 'code', None)):
                client = self.client(2)
                with patch('excutor.eviskill_runners.launcher.urlopen',
                           side_effect=[error, self.response()]) as request, \
                     patch('excutor.eviskill_runners.launcher.time.sleep') as sleep:
                    self.assertEqual(client.chat(self.messages), 'print(1)')
                self.assertEqual(request.call_count, 2)
                first, second = [entry.args[0] for entry in request.call_args_list]
                self.assertEqual(first.data, second.data)
                self.assertEqual(first.full_url, 'https://model.example/v1/chat/completions')
                self.assertEqual(first.get_header('User-agent'), 'skillpatch-rebuild/1.0')
                self.assertEqual(json.loads(first.data)['messages'], self.messages)
                self.assertEqual((client.request_attempts, client.retry_count), (2, 1))
                sleep.assert_called_once_with(1)

    def test_transient_exhaustion_is_bounded(self):
        for kind in ('transport', 'http'):
            with self.subTest(kind=kind):
                client = self.client(2)
                errors = ([URLError('TLS EOF') for _ in range(3)] if kind == 'transport'
                          else [self.http_error(503) for _ in range(3)])
                with patch('excutor.eviskill_runners.launcher.urlopen', side_effect=errors) as request, \
                     patch('excutor.eviskill_runners.launcher.time.sleep') as sleep:
                    with self.assertRaises(URLError if kind == 'transport' else ValueError):
                        client.chat(self.messages)
                self.assertEqual(request.call_count, 3)
                self.assertEqual((client.request_attempts, client.retry_count), (3, 2))
                self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_http_403_is_not_retried(self):
        client = self.client(3)
        with patch('excutor.eviskill_runners.launcher.urlopen',
                   side_effect=self.http_error(403)) as request, \
             patch('excutor.eviskill_runners.launcher.time.sleep') as sleep:
            with self.assertRaisesRegex(ValueError, '403'):
                client.chat(self.messages)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual((client.request_attempts, client.retry_count), (1, 0))

    def test_malformed_and_nontext_outputs_are_not_retried(self):
        responses = [io.BytesIO(b'not JSON'), io.BytesIO(b'{}'), io.BytesIO(b'{"choices": []}'),
                     self.response(finish_reason='content_filter'), self.response(['not text'])]
        for index, response in enumerate(responses):
            with self.subTest(response=index):
                client = self.client(3)
                with patch('excutor.eviskill_runners.launcher.urlopen', return_value=response) as request, \
                     patch('excutor.eviskill_runners.launcher.time.sleep') as sleep:
                    with self.assertRaises(ValueError):
                        client.chat(self.messages)
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual((client.request_attempts, client.retry_count), (1, 0))

    def test_empty_or_truncated_actions_retry_the_identical_request(self):
        for content, finish in [('', 'stop'), ('   ', 'stop'), (None, 'stop'), ('partial code', 'length')]:
            with self.subTest(content=content, finish=finish):
                client = self.client(2)
                with patch('excutor.eviskill_runners.launcher.urlopen', side_effect=[
                        self.response(content, finish), self.response('print(2)')]) as request, \
                     patch('excutor.eviskill_runners.launcher.time.sleep'):
                    self.assertEqual(client.chat(self.messages), 'print(2)')
                self.assertEqual((client.request_attempts, client.retry_count), (2, 1))
                self.assertEqual(request.call_args_list[0].args[0].data, request.call_args_list[1].args[0].data)

    def test_incomplete_response_budget_is_bounded_and_diagnostic_is_saved(self):
        client = self.client(2)
        client.diagnostics_path = self.root / 'responses.jsonl'
        with patch('excutor.eviskill_runners.launcher.urlopen', side_effect=[
                self.response('', 'length') for _ in range(3)]) as request, \
             patch('excutor.eviskill_runners.launcher.time.sleep'):
            with self.assertRaisesRegex(ValueError, "finish_reason='length', content_chars=0"):
                client.chat(self.messages)
        self.assertEqual(request.call_count, 3)
        summaries = [json.loads(line) for line in client.diagnostics_path.read_text().splitlines()]
        self.assertEqual([row['finish_reason'] for row in summaries], ['length'] * 3)
        self.assertNotIn(self.environment['FIXTURE_API_KEY'], client.diagnostics_path.read_text())

    def test_default_zero_preserves_single_attempt_behavior(self):
        self.assertEqual(self.config.max_retries, 0)
        client = ChatClient(self.config, self.environment)
        with patch('excutor.eviskill_runners.launcher.urlopen', side_effect=URLError('TLS EOF')) as request, \
             patch('excutor.eviskill_runners.launcher.time.sleep') as sleep:
            with self.assertRaises(URLError):
                client.chat(self.messages)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual((client.request_attempts, client.retry_count), (1, 0))

    def test_parser_accepts_nonnegative_retry_budget_and_defaults_to_zero(self):
        config = self.root / 'native.toml'
        prefix = ('[task]\nbenchmark="appworld"\npath="tasks.json"\nskill_mode="no-skill"\n'
                  '[agent]\nmodel="fixture-model"\n')
        for setting, expected in (('', 0), ('max_retries=0\n', 0), ('max_retries=3\n', 3)):
            with self.subTest(setting=setting):
                config.write_text(prefix + setting)
                self.assertEqual(load_native_config(config).max_retries, expected)
        for value in ('-1', 'true', 'false', '1.5', '"2"'):
            with self.subTest(value=value):
                config.write_text(prefix + f'max_retries={value}\n')
                with self.assertRaisesRegex(ValueError, 'max_retries'):
                    load_native_config(config)

    def test_client_rejects_invalid_programmatic_retry_budget(self):
        for value in (-1, True, False, 1.5, '2'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'max_retries'):
                    self.client(value)

    def test_batch_saves_actual_attempts_and_retries_on_success_or_failure(self):
        self.config.tasks_path.write_text('["fixture_1"]')
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                client = self.client(1)
                runner = Mock()
                runner.action_agent.llm = client
                def run_task(*args, **kwargs):
                    client.chat(self.messages)
                    client.chat(self.messages)
                    return {'dataset': 'appworld', 'task_id': 'fixture_1'}
                runner.run_task.side_effect = run_task
                responses = [self.response(), URLError('TLS EOF'),
                             self.response() if succeeds else URLError('TLS EOF')]
                output = self.root / f'run-{succeeds}'
                with patch('excutor.eviskill_runners.launcher.create_runner', return_value=runner), \
                     patch('excutor.eviskill_runners.launcher.urlopen', side_effect=responses), \
                     patch('excutor.eviskill_runners.launcher.time.sleep'):
                    state = run_native(client.config, output, self.environment)
                self.assertEqual(state['status'], 'completed' if succeeds else 'backend_error', state)
                self.assertEqual(state['model_requests'], {'attempts': 3, 'retries': 1})
                saved = json.loads((output / 'result.json').read_text())
                self.assertEqual(saved['model_requests'], state['model_requests'])
                runner.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
