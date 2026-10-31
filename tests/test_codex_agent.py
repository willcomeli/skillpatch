"""Codex subprocess contract and real SkillPatch stages, without a model API."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from harness.agent_client import AgentClient, AgentConfig
from excutor.executor_adapter.storage import write_json

SCRIPT = r'''
import json, os, pathlib, re, sys, time
prompt = sys.stdin.read()
report = pathlib.Path(re.search(r'Write the final JSON object to: (.+)', prompt).group(1))
mode = os.environ.get('FAKE_MODE', 'ok')
pathlib.Path('invocation.json').write_text(json.dumps({
    'argv': sys.argv[1:], 'prompt_length': len(prompt),
    'method_key_available': bool(os.environ.get('METHOD_API_KEY')),
    'default_codex_auth_present': 'CODEX_API_KEY' in os.environ,
    'default_openai_auth_present': 'OPENAI_API_KEY' in os.environ,
}))
if mode == 'timeout': time.sleep(20)
if mode != 'missing':
    report.write_text(os.environ.get('FAKE_REPORT', '{"summary":"fixture","improvements":[]}'))
if mode == 'invalid': report.write_text('{broken')
if mode == 'link':
    report.unlink()
    report.symlink_to(os.environ['FAKE_TARGET'])
print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture'}), flush=True)
if mode == 'recoverable_tool_error':
    print(json.dumps({'type': 'item.completed', 'item': {
        'type': 'command_execution', 'status': 'failed', 'exit_code': 1}}))
if mode == 'error':
    print(json.dumps({'type': 'error', 'message': 'provider failed ' + os.environ['METHOD_API_KEY']}))
if mode == 'reconnect':
    print(json.dumps({'type': 'error', 'message': 'Reconnecting... 1/5 (stream disconnected)'}))
if mode == 'turn_failed':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'provider failure'}}))
if mode != 'no_message':
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}))
if mode not in ('no_completed', 'turn_failed'):
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
if mode == 'nonzero': sys.exit(2)
'''


class CodexAgentTests(unittest.TestCase):
    def test_completed_turn_after_reconnect_succeeds(self):
        result = self.invoke('reconnect')
        self.assertFalse(result.errors)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.script = self.root / 'fake_codex.py'
        self.script.write_text(SCRIPT)
        self.material = self.root / 'context.json'
        self.material.write_text('{}')
        self.config = AgentConfig('gpt-5.5', 'https://method.invalid/v1', api_key_env='METHOD_API_KEY',
                                  harness='codex', command=(sys.executable, str(self.script)), timeout_sec=2)
        self.environment = {'METHOD_API_KEY': 'synthetic-codex-method-key',
                            'OPENAI_API_KEY': 'synthetic-unrelated-openai-key',
                            'CODEX_API_KEY': 'synthetic-unrelated-codex-key'}

    def invoke(self, mode='ok', **kwargs):
        return AgentClient().run(self.config, kwargs.pop('prompt', 'Analyze the material'),
            [self.material], self.root / mode, {**self.environment, 'FAKE_MODE': mode, **kwargs})

    def test_uses_explicit_model_provider_and_keeps_key_out_of_cli_and_artifacts(self):
        result = self.invoke()
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.final_text, 'done')
        request = json.loads((self.root / 'ok/request.json').read_text())
        self.assertEqual(request['wire_api'], 'responses')
        command = request['command']
        self.assertEqual(command[command.index('-m') + 1], 'gpt-5.5')
        self.assertIn('workspace-write', command)
        self.assertIn('--ignore-user-config', command)
        self.assertIn('--ephemeral', command)
        self.assertIn('cli_auth_credentials_store="ephemeral"', command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
        provider = next(arg for arg in command if arg.startswith('model_providers.skillpatch_method='))
        self.assertIn('https://method.invalid/v1', provider)
        self.assertIn('env_key = "METHOD_API_KEY"', provider)
        self.assertIn('wire_api = "responses"', provider)
        self.assertIn('requires_openai_auth = false', provider)
        invocation = json.loads((self.root / 'ok/workspace/invocation.json').read_text())
        self.assertTrue(invocation['method_key_available'])
        self.assertFalse(invocation['default_codex_auth_present'])
        self.assertFalse(invocation['default_openai_auth_present'])
        for path in (self.root / 'ok').rglob('*'):
            if path.is_file():
                for key in self.environment.values():
                    self.assertNotIn(key, path.read_text())

    def test_complete_turn_and_final_message_and_valid_report_are_all_required(self):
        for mode in ('no_completed', 'no_message', 'missing', 'invalid', 'turn_failed', 'nonzero'):
            with self.subTest(mode=mode):
                result = self.invoke(mode)
                self.assertEqual(result.status, 'failed', result)

    def test_provider_error_is_failure_and_redacted_even_with_success_event(self):
        result = self.invoke('error')
        self.assertEqual(result.status, 'failed')
        self.assertTrue(any('provider failed' in item for item in result.errors))
        self.assertNotIn(self.environment['METHOD_API_KEY'], json.dumps(result.errors))
        self.assertNotIn(self.environment['METHOD_API_KEY'],
                         (self.root / 'error/stdout.jsonl').read_text())

    def test_recovered_tool_error_is_not_a_terminal_agent_error(self):
        result = self.invoke('recoverable_tool_error')
        self.assertEqual(result.status, 'completed', result)

    def test_report_symlink_is_rejected(self):
        target = self.root / 'outside.json'
        target.write_text('{}')
        result = self.invoke('link', FAKE_TARGET=str(target))
        self.assertEqual(result.status, 'failed', result)
        self.assertEqual(target.read_text(), '{}')

    def test_large_prompt_is_sent_through_stdin(self):
        result = self.invoke('large', prompt='Analyze material.\n' + 'x' * 250000)
        self.assertEqual(result.status, 'completed', result)
        invocation = json.loads((self.root / 'large/workspace/invocation.json').read_text())
        self.assertGreater(invocation['prompt_length'], 250000)
        self.assertNotIn('x' * 100, ' '.join(invocation['argv']))

    def test_timeout_stops_the_codex_process(self):
        self.config = replace(self.config, timeout_sec=1)
        result = self.invoke('timeout')
        self.assertEqual(result.status, 'timeout')
        self.assertIsNotNone(result.return_code)

    def test_cancellation_is_saved_and_process_group_is_stopped(self):
        process = Mock(returncode=None)
        process.communicate.side_effect = KeyboardInterrupt
        process.poll.side_effect = lambda: process.returncode
        def stop(child):
            child.returncode = -15
        with patch('harness.agent_client.subprocess.Popen', return_value=process), \
                patch.object(AgentClient, '_stop', side_effect=stop) as stopped:
            result = self.invoke('cancelled')
        self.assertEqual(result.status, 'cancelled')
        stopped.assert_called_once_with(process)
        self.assertEqual(json.loads((self.root / 'cancelled/result.json').read_text())['status'], 'cancelled')

    def test_missing_method_key_does_not_fall_back_to_saved_codex_auth(self):
        self.environment.pop('METHOD_API_KEY')
        with patch('harness.agent_client.subprocess.Popen') as popen:
            result = self.invoke('no-key')
        self.assertEqual(result.status, 'failed')
        popen.assert_not_called()

    def test_real_diagnosis_and_generation_pipelines_accept_codex_reports(self):
        from method.skillpatch.patch_diagnosis.config import load_discovery_config
        from method.skillpatch.patch_diagnosis.discovery import run_discovery
        from method.skillpatch.patch_generation.config import Stage2Config
        from method.skillpatch.patch_generation.pipeline import run_stage2
        original = self.root / 'source.json'
        write_json(original, {'dataset': 'appworld', 'task_id': 'fixture_1', 'split': 'train',
            'instruction': 'Compare dates.', 'retrieved_memory_text': 'Normalize dates before comparing.',
            'success': False, 'score': 0, 'steps': 1,
            'trajectory': [{'action': 'Compare local dates.', 'next_observation': 'Wrong day.'}],
            'evaluation_summary': {'num_tests': 1, 'passed_tests': 0, 'failed_tests': 1}})
        prompt = self.root / 'prompt.md'
        prompt.write_text('Return the requested JSON report.')
        config = self.root / 'agent.toml'
        config.write_text('[diagnosis]\nharness="codex"\nmodel="gpt-5.5"\n'
            'base_url="https://method.invalid/v1"\napi_key_env="METHOD_API_KEY"\n'
            f'command={json.dumps([sys.executable, str(self.script)])}\n'
            f'prompt_path={json.dumps(str(prompt))}\nsession_timeout_sec=2\n')
        report = {'summary': 'Normalize date comparison.', 'improvements': [{
            'target': {'skill_ref': 'S0001', 'quote': 'Normalize dates before comparing.'},
            'diagnosis': 'Dates were local.', 'suggestion': 'Use the requested time zone.',
            'evidence': [{'evidence_id': 'T000001', 'quote': 'Compare local dates.'}]}]}
        first = run_discovery(original, load_discovery_config(config), self.root / 'diagnosis',
                              {**self.environment, 'FAKE_REPORT': json.dumps(report)})
        self.assertEqual(first['status'], 'completed', first)
        patch_report = {'status': 'candidate', 'reason': 'Clarify dates.',
                        'when_to_use': 'When comparing dates.',
                        'how_to_interpret': 'Resolve time zones before comparing.'}
        second = run_stage2(self.root / 'diagnosis', 'I000001', Stage2Config(config),
                            self.root / 'generation',
                            {**self.environment, 'FAKE_REPORT': json.dumps(patch_report)})
        self.assertEqual(second['status'], 'generated', second)
        self.assertTrue(Path(second['patch_path']).is_file())
