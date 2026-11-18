"""Protocol checks for script-only SkillsBench orchestration."""

import json
import multiprocessing
import os
import time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from scripts.skillsbench import experiment as exp


def _parallel_probe_job(label, activity, lock, counts):
    with lock:
        counts['active'] += 1
        counts['peak'] = max(counts['peak'], counts['active'])
        activity.append((label, os.getpid()))
    try:
        time.sleep(0.3)
        if label == 'bad':
            raise RuntimeError('Expected isolated task failure')
        return {'status': 'completed', 'pid': os.getpid()}
    finally:
        with lock:
            counts['active'] -= 1


class SkillsBenchExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tasks = self.root / 'tasks'
        self.tasks.mkdir()
        for name in ('one', 'two'):
            task = self.tasks / name
            task.mkdir()
            (task / 'task.md').write_text('---\nagent:\n  timeout_sec: 1717\nverifier:\n  timeout_sec: 919\nsandbox:\n  build_timeout_sec: 723\n---\nSolve it.\n')
            for skill in ('alpha', 'beta'):
                folder = task / 'environment/skills' / skill
                folder.mkdir(parents=True)
                (folder / 'SKILL.md').write_text('Original ' + skill)
                (folder / 'helper.py').write_text('print(1)\n')
        self.native = SimpleNamespace(model='executor', base_url='https://executor.invalid/v1')
        self.cfg = {'agent': 'openhands', 'bench_command': ['bench'], 'expected_backend_version': '0.6.3'}
        self.optimizer = SimpleNamespace(model='optimizer', api_key_env='METHOD_API_KEY',
                                         base_url_env='METHOD_BASE_URL', base_url='https://example.invalid/v1')

    def request(self, name='one'):
        return exp.replace(exp.request_for(self.tasks / name, self.cfg, self.native),
                           skills_dir=self.tasks / name / 'environment/skills')

    def additional_fixture(self, *, events=0, feedback='unavailable', prompt=''):
        output = self.root / 'run'
        source = output / 'previous/one'
        (source / 'backend').mkdir(parents=True)
        exp.write_json(source / 'result.json', {'status': 'agent_error',
            'execution': {'trajectory_events': events}, 'feedback': {'status': feedback}})
        (source / 'backend/stderr.log').write_text(prompt)
        policy = {'output_subdirectory': 'additional', 'tasks': [{'name': 'one',
            'failure_source': str(source), 'failure_result_sha256': exp.sha256_file(source / 'result.json'),
            'reason': 'approved preparation correction'}]}
        state = {'baselines': {'one': {'status': 'failed'}}, 'methods': {}}
        return output, policy, state

    def test_additional_preparation_never_replays_agent_execution_or_scoring(self):
        for events, feedback, prompt in ((1, 'unavailable', ''), (0, 'available', ''),
                                         (0, 'unavailable', 'Prompt 1/1: task')):
            with self.subTest(events=events, feedback=feedback, prompt=prompt):
                output, policy, state = self.additional_fixture(events=events, feedback=feedback, prompt=prompt)
                with patch.object(exp, '_batch_results', side_effect=AssertionError('must not execute')):
                    with self.assertRaisesRegex(ValueError, 'replay Agent'):
                        exp.additional_preparation_recovery(policy, state, {'one': self.request()},
                            output, {}, 1, (), lambda: None)
                import shutil
                shutil.rmtree(output)

    def test_additional_preparation_is_checkpointed_without_duplicate_model_calls(self):
        output, policy, state = self.additional_fixture()
        calls = []

        def worker(name, request, folder, environment, prepare_only):
            calls.append(name)
            folder.mkdir(parents=True)
            exp.write_json(folder / 'result.json', {'status': 'completed'})
            return {'status': 'completed', 'source': str(folder), 'hashes': exp.tree_hashes(folder)}

        with patch.object(exp, '_baseline_job', side_effect=worker):
            exp.additional_preparation_recovery(policy, state, {'one': self.request()},
                output, {}, 1, (), lambda: None)
            exp.additional_preparation_recovery(policy, state, {'one': self.request()},
                output, {}, 1, (), lambda: None)
        self.assertEqual(calls, ['one'])
        self.assertEqual(state['baselines']['one']['status'], 'completed')
        (output / 'additional/one/result.json').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'archive changed'):
            exp.additional_preparation_recovery(policy, state, {'one': self.request()},
                output, {}, 1, (), lambda: None)

    def test_additional_preparation_adopts_a_completed_archive_after_lost_response(self):
        output, policy, state = self.additional_fixture()
        folder = output / 'additional/one'
        folder.mkdir(parents=True)
        exp.write_json(folder / 'result.json', {'status': 'completed'})
        with patch.object(exp, 'load_archive', return_value=SimpleNamespace(task_id='one')) as load, \
                patch.object(exp, '_batch_results', return_value=iter(())):
            exp.additional_preparation_recovery(policy, state, {'one': self.request()},
                output, {}, 1, (), lambda: None)
        load.assert_called_once_with(folder.resolve())
        self.assertEqual(state['baselines']['one']['source'], str(folder.resolve()))

    def test_additional_preparation_cannot_change_references_after_methods_start(self):
        output, policy, state = self.additional_fixture()
        state['methods']['skillopt/two'] = {'status': 'completed'}
        with self.assertRaisesRegex(ValueError, 'before method optimization'):
            exp.additional_preparation_recovery(policy, state, {'one': self.request()},
                output, {}, 1, (), lambda: None)

    def test_scored_archive_registration_repair_does_not_reexecute_the_task(self):
        output = self.root / 'repair-run'
        folder = output / 'preparation_recovery/one'
        exp.write_json(folder / 'result.json', {'status': 'completed'})
        state = {'baselines': {'one': {'status': 'failed', 'error': 'old reader error'}},
                 'methods': {'skillopt/one': {'status': 'blocked'}}}
        archive = SimpleNamespace(task_id='one', benchmark='skillsbench', score=1)
        with patch.object(exp, 'load_archive', return_value=archive), \
                patch.object(exp, '_baseline_job', side_effect=AssertionError('must not execute')):
            repaired = exp.adopt_completed_references(state, {'one': self.request()}, output)
            self.assertEqual(exp.adopt_completed_references(state, {'one': self.request()}, output), {})
        self.assertEqual(repaired['one']['score'], 1)
        self.assertEqual(state['baselines']['one']['status'], 'completed')
        self.assertEqual(state['baselines']['one']['previous_reference_error'], 'old reader error')
        self.assertNotIn('skillopt/one', state['methods'])

    def test_registration_repair_preserves_invalid_archives_and_used_references(self):
        output = self.root / 'invalid-repair'
        exp.write_json(output / 'preparation_recovery/one/result.json', {'status': 'completed'})
        state = {'baselines': {'one': {'status': 'failed'}}, 'methods': {}}
        with patch.object(exp, 'load_archive', side_effect=ValueError('invalid archive')):
            self.assertEqual(exp.adopt_completed_references(state, {'one': self.request()}, output), {})
        self.assertEqual(state['baselines']['one']['status'], 'failed')
        state['methods']['skillopt/one'] = {'status': 'completed'}
        with patch.object(exp, 'load_archive', return_value=SimpleNamespace(
                task_id='one', benchmark='skillsbench', score=1)):
            with self.assertRaisesRegex(ValueError, 'already used'):
                exp.adopt_completed_references(state, {'one': self.request()}, output)

    def test_v1_1_environment_build_budget_is_preserved(self):
        path = self.tasks / 'one/task.md'
        path.write_text('---\nagent:\n  timeout_sec: 900\nverifier:\n  timeout_sec: 900\nenvironment:\n  build_timeout_sec: 600\nsandbox:\n  build_timeout_sec: 1800\n---\nSolve it.\n')
        request = self.request()
        self.assertEqual(request.sandbox_setup_timeout, 600)
        self.assertEqual(request.agent_idle_timeout, 0)

    def test_four_processes_continue_after_one_task_fails(self):
        with multiprocessing.Manager() as manager:
            activity = manager.list()
            counts = manager.dict(active=0, peak=0)
            lock = manager.Lock()
            names = ['bad'] + [str(i) for i in range(7)]
            jobs = [(name, (name, activity, lock, counts)) for name in names]
            results = dict(exp._batch_results(_parallel_probe_job, jobs, 4, ()))
            self.assertEqual(set(results), set(names))
            self.assertEqual(results['bad']['status'], 'failed')
            self.assertTrue(all(results[name]['status'] == 'completed' for name in names[1:]))
            self.assertGreaterEqual(counts['peak'], 2)
            self.assertLessEqual(counts['peak'], 4)
            self.assertTrue(all(pid != os.getpid() for _, pid in activity))

    def test_stopping_batch_does_not_queue_remaining_dataset(self):
        pool = MagicMock()
        pool.__enter__.return_value = pool

        def submit(worker, *arguments):
            future = Future()
            future.set_result({'status': 'completed'})
            return future

        pool.submit.side_effect = submit
        jobs = [(str(i), (str(i),)) for i in range(87)]
        with patch.object(exp, 'ProcessPoolExecutor', return_value=pool):
            results = exp._batch_results(None, jobs, 4, ())
            next(results)
            results.close()
        self.assertEqual(pool.submit.call_count, 4)

    def test_empty_reference_does_not_abort_other_baselines(self):
        def execute(request, folder, environment, **kwargs):
            if request.task_dir.name == 'one':
                raise ValueError('No trajectory produced')
            folder.mkdir(parents=True)
            (folder / 'result.json').write_text('{}')
        jobs = [(name, (name, self.request(name), self.root / 'baseline' / name, {}, False))
                for name in ('one', 'two')]
        with patch.object(exp, 'execute', side_effect=execute):
            results = dict(exp._batch_results(exp._baseline_job, jobs, 1, ()))
        self.assertEqual(results['one']['status'], 'failed')
        self.assertEqual(results['two']['status'], 'completed')

    def test_reference_reuse_verifies_hashes_and_ignores_failures(self):
        source = self.root / 'previous'
        source.mkdir()
        archive = source / 'baseline' / 'one'
        archive.mkdir(parents=True)
        evidence = archive / 'result.json'
        evidence.write_text('{"status":"completed"}')
        plan = {'executor': 'benchflow', 'execution_model': 'execution',
                'execution_base_url': 'https://execution.invalid/v1', 'config_sha256': 'config',
                'implementation_hashes': {}, 'task_hashes': {'one': {'task.md': 'source'}, 'two': {}}}
        state = {'status': 'failed', 'baselines': {
            'one': {'status': 'completed', 'source': str(archive), 'hashes': exp.tree_hashes(archive)},
            'two': {'status': 'failed', 'error': 'No trajectory'}}}
        exp.write_json(source / 'plan.json', plan)
        exp.write_json(source / 'result.json', state)
        reused = exp._reused_baselines(source, plan, ['one', 'two'])
        self.assertEqual(set(reused), {'one'})
        self.assertEqual(reused['one']['source'], str(archive))
        evidence.write_text('{"status":"changed"}')
        with self.assertRaisesRegex(ValueError, 'Reference archive changed'):
            exp._reused_baselines(source, plan, ['one', 'two'])

    def test_native_budgets_and_no_idle_cap(self):
        task = self.tasks / 'one'
        before = (task / 'task.md').read_bytes()
        request = self.request()
        self.assertEqual(request.agent_idle_timeout, 0)
        self.assertEqual(request.sandbox_setup_timeout, 723)
        self.assertFalse(hasattr(request, 'timeout_sec'))
        self.assertEqual(exp.metadata(task)['agent']['timeout_sec'], 1717)
        self.assertEqual(exp.metadata(task)['verifier']['timeout_sec'], 919)
        self.assertEqual((task / 'task.md').read_bytes(), before)

    def test_step_retry_upgrade_reuses_scored_archives_but_rejects_changed_task_budget(self):
        source = self.root / 'previous'
        archive = source / 'baseline/one'
        archive.mkdir(parents=True)
        request = self.request()
        native = exp.jsonable(request)
        exp.write_json(archive / 'request.json', native)
        exp.write_json(archive / 'result.json', {'status': 'completed', 'prepared': {'file_hashes': {'task.md': 'task'}}})
        before = {'executor': 'benchflow', 'execution_model': 'execution', 'execution_base_url': 'endpoint',
                  'config_sha256': 'before', 'implementation_hashes': {'excutor': {}, 'prompts': {}, 'method': {'unused': 'old'}},
                  'task_hashes': {'one': {'task.md': 'task'}}}
        after = {**before, 'config_sha256': 'after',
                 'implementation_hashes': {'excutor': {}, 'prompts': {}, 'method': {'unused': 'new'}}}
        state = {'status': 'cancelled', 'baselines': {'one': {'status': 'completed', 'source': str(archive),
                                                          'hashes': exp.tree_hashes(archive)}}}
        exp.write_json(source / 'plan.json', before)
        exp.write_json(source / 'result.json', state)
        requests = {'one': exp.replace(request, bench_command=(*request.bench_command, '--step-retries'))}
        with patch.object(exp, 'load_archive', return_value=SimpleNamespace(task_id='one', score=1, success=True)):
            reused = exp._reused_baselines(source, after, requests, step_retry_upgrade=True)
        self.assertEqual(reused['one']['official_score'], 1)
        requests['one'] = exp.replace(requests['one'], sandbox_setup_timeout=999)
        with self.assertRaisesRegex(ValueError, 'execution settings changed'):
            exp._reused_baselines(source, after, requests, step_retry_upgrade=True)

    def test_installer_proxy_preserves_original_command(self):
        from scripts.skillsbench.bench import install_command
        original = "uv tool install --from 'git+https://github.com/OpenHands/OpenHands-CLI.git@pinned' openhands"
        result = install_command(original, 'http://172.17.0.1:17899')
        self.assertTrue(result.endswith(' && ' + original))
        self.assertIn('HTTPS_PROXY=http://172.17.0.1:17899', result)
        self.assertIn('UV_HTTP_TIMEOUT=300', result)
        with self.assertRaises(ValueError):
            install_command(original, 'http://user:password@localhost:80')

    def test_custom_execution_provider_is_explicit_and_does_not_override_callers(self):
        from scripts.skillsbench.bench import resolve_execution_environment
        environment = {'BENCHFLOW_PROVIDER_BASE_URL': 'https://gateway.invalid/v1',
                       'BENCHFLOW_PROVIDER_API_KEY': 'test-key'}
        def original(agent, model, env):
            return env
        bound = resolve_execution_environment(original, 'openhands', 'openai/execution', None, environment)
        self.assertEqual(bound, environment)
        explicit = {'BENCHFLOW_PROVIDER_BASE_URL': 'https://explicit.invalid/v1',
                    'BENCHFLOW_PROVIDER_API_KEY': 'caller-key'}
        self.assertEqual(resolve_execution_environment(original, 'openhands', 'openai/execution', explicit, environment), explicit)
        self.assertEqual(resolve_execution_environment(original, 'oracle', 'openai/execution', None, environment), {})
        self.assertEqual(resolve_execution_environment(original, 'openhands', 'openai/execution', None, {'BENCHFLOW_PROVIDER_BASE_URL': 'https://gateway.invalid/v1'}), {})

    def test_registry_auth_uses_host_proxy_without_overriding_explicit_environment(self):
        from scripts.skillsbench.bench import host_proxy_environment
        original = {'METHOD_BASE_URL': 'https://method.invalid/v1', 'HTTPS_PROXY': 'http://explicit:1234'}
        result = host_proxy_environment('http://host.docker.internal:7897', original)
        self.assertEqual(result['HTTPS_PROXY'], original['HTTPS_PROXY'])
        self.assertEqual(result['http_proxy'], 'http://127.0.0.1:7897')
        self.assertEqual(result['METHOD_BASE_URL'], original['METHOD_BASE_URL'])
        self.assertNotIn('http_proxy', original)
        self.assertEqual(host_proxy_environment('http://172.17.0.1:17899', original), original)

    def test_installer_network_options_preserve_package_pins(self):
        from scripts.skillsbench.bench import install_command
        original = ('curl -LsSf https://astral.sh/uv/install.sh | sh && '
                    "uv tool install --from 'git+https://github.com/OpenHands/OpenHands-CLI.git@pinned' "
                    'openhands --python 3.12 --overrides /tmp/oh-sdk-overrides.txt')
        result = install_command(original, 'http://172.17.0.1:17899',
                                 'https://pypi.tuna.tsinghua.edu.cn/simple', True)
        expected = original.replace('https://astral.sh/uv/install.sh',
            'https://github.com/astral-sh/uv/releases/latest/download/uv-installer.sh')
        self.assertTrue(result.endswith(' && ' + expected))
        self.assertIn('UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple', result)
        for index in ('http://example.org/simple', 'https://user:secret@example.org/simple'):
            with self.assertRaises(ValueError):
                install_command(original, 'http://172.17.0.1:17899', index)

    def test_trace2skill_every_skill_uses_same_baseline_and_reference(self):
        request = self.request()
        baseline = self.root / 'baseline'
        output = self.root / 'trace2skill'
        calls = []

        def evolve(sources, initial, folder, environment, **kwargs):
            self.assertEqual(sources, [baseline])
            self.assertEqual((initial / 'SKILL.md').read_text(), 'Original ' + initial.name)
            exp.shutil.copytree(initial, folder / 'skill')
            (folder / 'skill/SKILL.md').write_text('Changed ' + initial.name)
            calls.append(initial.name)
            return {'status': 'completed', 'skill_dir': str(folder / 'skill')}

        def execute(candidate, folder, environment):
            self.assertEqual(candidate.task_dir, request.task_dir)
            for skill in ('alpha', 'beta'):
                self.assertEqual((candidate.skills_dir / skill / 'SKILL.md').read_text(), 'Changed ' + skill)
                self.assertEqual((candidate.skills_dir / skill / 'helper.py').read_text(), 'print(1)\n')
            return folder

        with patch('method_input_adapters.trace2skill.evolution.evolve', side_effect=evolve), \
             patch.object(exp, 'execute', side_effect=execute) as live, \
             patch.object(exp, 'comparison', return_value={'score_delta': 1}):
            result = exp.optimize_pack('trace2skill', baseline, request, output, {}, self.optimizer, None)
        self.assertEqual(calls, ['alpha', 'beta'])
        self.assertEqual(live.call_count, 1)
        self.assertTrue(result['skill_changed'])
        self.assertEqual((request.skills_dir / 'alpha/SKILL.md').read_text(), 'Original alpha')

    def test_skillopt_reuses_reference_and_runs_candidate_on_same_task(self):
        request = self.request()
        baseline = self.root / 'baseline'
        baseline.mkdir()
        (baseline / 'events.jsonl').write_text('full trajectory\n')
        seen = []

        def train(trainer):
            adapter = trainer.adapter
            loader = adapter.get_dataloader()
            self.assertEqual(loader.get_train_size(), 1)
            for split in ('train', 'val'):
                batch = loader.build_train_batch(1, 0) if split == 'train' else loader.build_eval_batch(1, 'valid_seen', 0)
                self.assertEqual([row['id'] for row in adapter.build_env_from_batch(batch)], ['one'])
            initial = (request.skills_dir / 'alpha/SKILL.md').read_text()
            for phase, envs, text in (
                    ('selection', adapter.build_eval_env(1, 'valid_seen', 0), initial),
                    ('training', adapter.build_train_env(1, 0), initial),
                    ('candidate', adapter.build_eval_env(1, 'valid_seen', 0), 'Candidate alpha')):
                adapter.rollout(envs, text, self.root / phase)
            return {'status': 'completed'}

        def export(sources, output, ids):
            seen.append(sources[0])
            output.mkdir()
            return [{'hard': 0, 'soft': 0}]

        def execute(candidate, output, environment):
            self.assertEqual(candidate.task_dir, request.task_dir)
            self.assertEqual((candidate.skills_dir / 'alpha/SKILL.md').read_text(), 'Candidate alpha')
            self.assertEqual((candidate.skills_dir / 'beta/SKILL.md').read_text(), 'Original beta')
            self.assertEqual((candidate.skills_dir / 'alpha/helper.py').read_text(), 'print(1)\n')
            return output

        with patch('skillopt.engine.trainer.ReflACTTrainer.train', train), \
             patch('method_input_adapters.skillopt.results.export_results', side_effect=export), \
             patch.object(exp, 'execute', side_effect=execute) as live:
            exp.optimize_skillopt_one(baseline, request, 'alpha', self.root / 'optimization',
                                     {'METHOD_API_KEY': 'test-key'}, self.optimizer,
                                     exp.ROOT / 'configs/skillsbench/skillopt.yaml')
        self.assertEqual(seen[:2], [baseline, baseline])
        self.assertEqual(live.call_count, 1)
        self.assertEqual((baseline / 'events.jsonl').read_text(), 'full trajectory\n')

    def test_baseline_only_finishes_batch_without_calling_methods(self):
        for name in ('method', 'method_input_adapters', 'excutor', 'prompts'):
            (self.root / name).mkdir()
        config = self.root / 'experiment.toml'
        config.write_text('fixture')
        (self.root / 'optimizer.toml').write_text('fixture')
        (self.root / 'skillopt.yaml').write_text('fixture')
        cfg = {**self.cfg, 'optimizer_config': 'optimizer.toml', 'skillopt_config': 'skillopt.yaml'}
        self.optimizer.metadata = lambda: {'model': 'optimizer'}
        def execute(request, output, environment, **kwargs):
            if marker_path:
                marker_path.write_text('{}')
            if request.task_dir.name == 'one':
                raise ValueError('task timed out')
            output.mkdir(parents=True)
            exp.write_json(output / 'result.json', {'prepared': {'task_dir': request.task_dir,
                           'skills_dir': request.task_dir / 'environment/skills'}})
            return output
        for index, marker in enumerate((None, 'BASELINE_ONLY_REQUESTED.json',
                'ENERGY_PRECACHE_COLLECTION_REQUESTED.json', 'SEISMIC_COMPAT_COLLECTION_REQUESTED.json',
                'LATEX_HF_COLLECTION_REQUESTED.json')):
            with self.subTest(marker=marker):
                output = self.root / f'run-{index}'
                marker_path = output / marker if marker else None
                with patch.object(exp, 'ROOT', self.root), \
                     patch.object(exp, 'load_settings', return_value=(cfg, sorted(self.tasks.iterdir()), self.native, self.optimizer, {})), \
                     patch.object(exp, 'execute', side_effect=execute), \
                     patch.object(exp, 'optimize_pack') as pack, patch.object(exp, 'optimize_skillpatch') as skillpatch:
                    state = exp.run(config, output, {}, baseline_only=marker is None)
                self.assertEqual(state['phase'], 'baseline_finished')
                self.assertEqual(state['status'], 'completed_with_errors')
                self.assertEqual(len(state['baselines']), 2)
                self.assertEqual(state['methods'], {})
                pack.assert_not_called()
                skillpatch.assert_not_called()

    def test_collects_entire_batch_before_any_method(self):
        for name in ('method', 'method_input_adapters', 'excutor', 'prompts'):
            (self.root / name).mkdir()
        config = self.root / 'experiment.toml'
        config.write_text('fixture')
        (self.root / 'optimizer.toml').write_text('fixture')
        (self.root / 'skillopt.yaml').write_text('fixture')
        cfg = {**self.cfg, 'optimizer_config': 'optimizer.toml', 'skillopt_config': 'skillopt.yaml'}
        self.optimizer.metadata = lambda: {'model': 'optimizer', 'missing_environment': ()}
        order = []

        def execute(request, output, environment, **kwargs):
            order.append('baseline-' + request.task_dir.name)
            output.mkdir(parents=True)
            exp.write_json(output / 'result.json', {'prepared': {'task_dir': request.task_dir,
                           'skills_dir': request.task_dir / 'environment/skills'}})
            return output

        def optimize(method, baseline, request, output, environment, optimizer, skillopt_config):
            order.append(method + '-' + request.task_dir.name)
            self.assertEqual(order[:2], ['baseline-one', 'baseline-two'])
            return {'status': 'completed'}

        with patch.object(exp, 'ROOT', self.root), \
             patch.object(exp, 'load_settings', return_value=(cfg, sorted(self.tasks.iterdir()), self.native, self.optimizer, {})), \
             patch.object(exp, 'execute', side_effect=execute), \
             patch.object(exp, 'optimize_pack', side_effect=optimize):
            state = exp.run(config, self.root / 'run', {}, methods=('trace2skill', 'skillopt'))
            self.assertEqual(state['status'], 'completed')
            # Serialized tuples must not make an identical resume fail.
            again = exp.run(config, self.root / 'run', {}, methods=('trace2skill', 'skillopt'), resume=True)
            self.assertEqual(again['status'], 'completed')
        self.assertEqual(len(order), 6)

    def test_original_skillopt_trainer_completes_source_task_gate(self):
        request = self.request()
        baseline = self.root / 'baseline'
        baseline.mkdir()
        (baseline / 'events.jsonl').write_text('complete source trace\n')
        live_sources = []

        def execute(candidate, output, environment):
            live_sources.append(output)
            self.assertEqual(candidate.task_dir, request.task_dir)
            self.assertIn('Check inventory', (candidate.skills_dir / 'alpha/SKILL.md').read_text())
            return output

        def export(sources, output, ids):
            output.mkdir(parents=True)
            passed = sources[0] != baseline
            rows = [{'id': ids[0], 'hard': int(passed), 'soft': float(passed),
                     'n_turns': 1, 'task_type': 'one'}]
            (output / 'results.jsonl').write_text(json.dumps(rows[0]) + '\n')
            return rows

        patches = [{'source_type': 'failure', 'batch_size': 1, 'patch': {
            'reasoning': 'Missing precondition', 'edits': [{'op': 'append', 'content': 'Check inventory'}]}}]
        with patch('method_input_adapters.skillopt.adapter.SharedExecutionAdapter.reflect', return_value=patches) as reflect, \
             patch('method_input_adapters.skillopt.results.export_results', side_effect=export), \
             patch.object(exp, 'execute', side_effect=execute):
            output = self.root / 'optimization'
            summary = exp.optimize_skillopt_one(baseline, request, 'alpha', output,
                {'METHOD_API_KEY': 'test-key'}, self.optimizer, exp.ROOT / 'configs/skillsbench/skillopt.yaml')
        self.assertEqual(summary['best_selection_hard'], 1)
        self.assertEqual(reflect.call_count, 1)
        self.assertEqual([row['id'] for row in reflect.call_args.args[0]], ['one'])
        self.assertIn('Check inventory', (output / 'best_skill.md').read_text())
        self.assertEqual(len(live_sources), 1)


if __name__ == '__main__':
    unittest.main()
