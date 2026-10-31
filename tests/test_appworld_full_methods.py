"""Single-round experiment policies reuse the smoke implementation and real archives."""

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import write_json
from method_input_adapters.execution import SharedExecutorBridge, load_tasks
from set_up.appworld_smoke import (
    prepare_method_configs, run_skillopt, run_skillpatch, run_trace2skill,
)
from set_up.appworld_smoke_inputs import prepare_inputs
import test_appworld_smoke as smoke_fixtures


class AppWorldFullMethodTests(unittest.TestCase):
    first_quote = smoke_fixtures.AppWorldSmokeTests.first_quote
    second_quote = smoke_fixtures.AppWorldSmokeTests.second_quote
    collect_source = smoke_fixtures.AppWorldSmokeTests.collect_source
    read = staticmethod(smoke_fixtures.AppWorldSmokeTests.read)
    fake_train = smoke_fixtures.AppWorldSmokeTests.fake_train
    fake_evolve = smoke_fixtures.AppWorldSmokeTests.fake_evolve

    def setUp(self):
        smoke_fixtures.AppWorldSmokeTests.setUp(self)
        self.inputs = prepare_inputs(self.manifest_path, self.root / 'inputs', dev_limit=2)
        self.configs = prepare_method_configs(self.inputs, profile='experiment')

    def unchanged_train(self, manifest, initial_skill, output, environment, *, emit_candidate=False, **kwargs):
        from method_input_adapters.skillopt.adapter import SharedExecutionAdapter

        output = Path(output)
        output.mkdir()
        entries = load_tasks(manifest)
        adapter = SharedExecutionAdapter(entries, SharedExecutorBridge(environment),
            frozen_trajectory_manifest=kwargs['frozen_trajectory_manifest'], initial_skill=initial_skill)
        train = [entry for entry in entries if entry['split'] == 'train']
        validation = [entry for entry in entries if entry['split'] == 'val']
        initial = Path(initial_skill).read_text()
        step = output / 'steps/step_0001'
        adapter.rollout(validation, initial, output / 'selection_eval_baseline')
        adapter.rollout(train, initial, step / 'rollout', use_eval_feedback=True)
        if emit_candidate:
            (step / 'candidate_skill.md').write_text(initial)
        (output / 'best_skill.md').write_text(initial)
        action = 'reject' if emit_candidate else 'skip_no_patches'
        write_json(step / 'step_record.json', {'action': action})
        write_json(output / 'history.json', [{'step': 1, 'action': action}])
        return {'best_score': 0.0}

    def unchanged_evolve(self, sources, initial_skill_dir, output, environment, **kwargs):
        output = Path(output)
        output.mkdir()
        candidate = output / 'skill'
        shutil.copytree(initial_skill_dir, candidate)
        bridge = SharedExecutorBridge(environment)
        validation = []
        for entry in (load_tasks(kwargs['validation_manifest']) if kwargs.get('validation_manifest') else []):
            folder = output / 'validation' / entry['id']
            before = bridge.run(entry, '', folder / 'baseline', skill_dir=initial_skill_dir)
            after = bridge.run(entry, '', folder / 'candidate', skill_dir=candidate)
            validation.append({'id': entry['id'], 'baseline_archive': str(before),
                               'candidate_archive': str(after)})
        write_json(output / 'validation.json', validation)
        return {'status': 'completed', 'skill_dir': str(candidate), 'skill_changed': False,
                'analysis_records': len(sources), 'validation_status': 'executed'}

    def invoke_skillopt(self, *, train=None, name='skillopt', profile='experiment'):
        (self.root / name).mkdir()
        with patch('method_input_adapters.skillopt.training.train', side_effect=train or self.fake_train):
            return run_skillopt(self.inputs, self.configs, self.root / name, self.environment, profile=profile)

    def invoke_trace2skill(self, *, evolve=None, name='trace2skill', profile='experiment'):
        (self.root / name).mkdir()
        with patch('method_input_adapters.trace2skill.evolution.evolve', side_effect=evolve or self.fake_evolve):
            return run_trace2skill(self.inputs, self.configs, self.root / name, self.environment,
                                  max_output_tokens=8192, profile=profile)

    def test_experiment_configuration_uses_one_shared_batch_and_small_reflection_groups(self):
        import yaml

        config = yaml.safe_load(self.configs['skillopt'].read_text())
        self.assertEqual(config['train']['batch_size'], len(self.inputs['train_entries']))
        self.assertEqual(config['train']['num_epochs'], 1)
        self.assertEqual(config['train']['accumulation'], 1)
        self.assertEqual(config['gradient']['minibatch_size'], 1)
        self.assertEqual(config['gradient']['merge_batch_size'], 8)
        self.assertEqual(config['gradient']['analyst_workers'], 1)
        self.assertFalse(config['optimizer']['use_slow_update'])
        self.assertFalse(config['optimizer']['use_meta_skill'])
        self.assertEqual(config['evaluation'], {'use_gate': True, 'sel_env_num': 0, 'eval_test': False})
        smoke_inputs = prepare_inputs(self.manifest_path, self.root / 'smoke-inputs', dev_limit=1)
        smoke_configs = prepare_method_configs(smoke_inputs)
        smoke_config = yaml.safe_load(smoke_configs['skillopt'].read_text())
        self.assertEqual(smoke_config['gradient']['minibatch_size'], len(smoke_inputs['train_entries']))
        self.assertNotIn('merge_batch_size', smoke_config['gradient'])

    def test_unchanged_skillopt_is_completed_with_real_baseline_and_selected_source_evidence(self):
        result = self.invoke_skillopt(train=self.unchanged_train)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['skill_changed'])
        self.assertEqual(result['candidate_replays'], [])
        self.assertEqual(result['source_replays'], [])
        self.assertEqual(result['validation_phases'], ['baseline'])
        self.assertEqual(len(result['validation'][0]), 2)
        self.assertEqual(result['candidate_validation_status'], 'not_run_no_candidate')
        self.assertEqual(result['gate_action'], 'not_run_no_candidate')
        selected = result['selected_source_evaluation']
        self.assertEqual(selected['strategy'], 'reuse_frozen_baseline')
        self.assertEqual([row['archive'] for row in selected['results']],
                         [row['source'] for row in self.inputs['manifest']['rows']])
        self.assertEqual([task['split'] for task, _ in self.world.calls], ['dev', 'dev'])
        self.assertFalse((self.root / 'skillopt/source_replay').exists())

    def test_identical_skillopt_candidate_records_baseline_cache_without_fabricating_rerun(self):
        def train(*args, **kwargs):
            return self.unchanged_train(*args, emit_candidate=True, **kwargs)

        result = self.invoke_skillopt(train=train)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['skill_changed'])
        self.assertEqual(result['gate_action'], 'reject')
        self.assertEqual(result['candidate_validation_status'], 'reused_baseline_for_identical_skill')
        self.assertEqual(len(result['validation']), 1)
        self.assertEqual(len(self.world.calls), 2)

    def test_unchanged_skillopt_preserves_any_executed_candidate_validation(self):
        def train(manifest, initial_skill, output, environment, **kwargs):
            summary = self.unchanged_train(manifest, initial_skill, output, environment,
                                           emit_candidate=True, **kwargs)
            from method_input_adapters.skillopt.adapter import SharedExecutionAdapter
            entries = [row for row in load_tasks(manifest) if row['split'] == 'val']
            adapter = SharedExecutionAdapter(entries, SharedExecutorBridge(environment))
            adapter.rollout(entries, Path(initial_skill).read_text(),
                            Path(output) / 'steps/step_0001/selection_eval')
            return summary

        result = self.invoke_skillopt(train=train)
        self.assertEqual(result['candidate_validation_status'], 'executed')
        self.assertEqual(result['validation_phases'], ['baseline', 'step_0001'])
        self.assertEqual([len(rows) for rows in result['validation']], [2, 2])
        self.assertEqual(len(self.world.calls), 4)

    def test_rejected_candidate_replay_does_not_replace_selected_baseline_performance(self):
        result = self.invoke_skillopt()
        self.assertEqual(result['gate_action'], 'reject')
        self.assertTrue(result['skill_changed'])
        selected = result['selected_source_evaluation']
        self.assertEqual(selected['strategy'], 'reuse_frozen_baseline')
        self.assertEqual(result['candidate_replays'], result['source_replays'])
        self.assertTrue(all(row['role'] == 'candidate_diagnostic' for row in result['candidate_replays']))
        self.assertTrue(all(row['role'] == 'selected_skill' for row in selected['results']))
        self.assertTrue(set(row['archive'] for row in selected['results']).isdisjoint(
            row['archive'] for row in result['candidate_replays']))
        candidate = Path(result['candidate_skill']).read_text()
        for row in result['candidate_replays']:
            self.assertEqual(self.read(row['archive'])['retrieved_memory_text'], candidate)

    def test_selected_candidate_reuses_its_verified_source_replay(self):
        def train(*args, **kwargs):
            summary = self.fake_train(*args, **kwargs)
            output = Path(args[2])
            step = output / 'steps/step_0001'
            (output / 'best_skill.md').write_text((step / 'candidate_skill.md').read_text())
            write_json(step / 'step_record.json', {'action': 'accept_new_best', 'selection_hard': 0.0})
            return summary

        result = self.invoke_skillopt(train=train)
        self.assertEqual(result['gate_action'], 'accept_new_best')
        self.assertEqual(result['selected_source_evaluation']['strategy'], 'reuse_candidate_replay')
        self.assertEqual([row['archive'] for row in result['selected_source_evaluation']['results']],
                         [row['archive'] for row in result['candidate_replays']])

    def test_unchanged_trace2skill_experiment_skips_dev(self):
        result = self.invoke_trace2skill(evolve=self.unchanged_evolve)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['skill_changed'])
        self.assertEqual(result['acceptance'], 'not_automatically_activated')
        self.assertEqual(result['validation'], [])
        self.assertFalse(result['validation_split_used'])
        self.assertEqual(result['candidate_replays'], [])
        self.assertEqual(self.world.calls, [])
        self.assertFalse((self.root / 'trace2skill/source_replay').exists())

    def test_changed_trace2skill_reference_is_replayed_with_explicit_candidate_role(self):
        result = self.invoke_trace2skill()
        self.assertTrue(result['skill_changed'])
        self.assertEqual(result['acceptance'], 'not_automatically_activated')
        self.assertEqual(result['candidate_replays'], result['source_replays'])
        for row in result['candidate_replays']:
            self.assertEqual(row['role'], 'candidate_diagnostic')
            self.assertIn(self.reference, self.read(row['archive'])['retrieved_memory_text'])

    def test_no_change_does_not_bypass_invalid_official_dev_feedback(self):
        def invalid_train(*args, **kwargs):
            summary = self.unchanged_train(*args, **kwargs)
            rows = (Path(args[2]) / 'selection_eval_baseline/results.jsonl').read_text().splitlines()
            source = Path(json.loads(rows[0])['source_archive'])
            raw = self.read(source)
            raw['evaluation_summary']['num_tests'] = 0
            write_json(source, raw)
            return summary

        def invalid_evolve(*args, **kwargs):
            result = self.fake_evolve(*args, **kwargs)
            rows = self.read(Path(args[2]) / 'validation.json')
            source = Path(rows[0]['candidate_archive'])
            raw = self.read(source)
            raw['evaluation_summary']['num_tests'] = 0
            write_json(source, raw)
            return result

        with self.assertRaises(ValueError):
            self.invoke_skillopt(train=invalid_train)
        with self.assertRaises(ValueError):
            self.invoke_trace2skill(evolve=invalid_evolve, profile='smoke')

    def test_false_unchanged_trace_status_is_rejected(self):
        def mislabeled(*args, **kwargs):
            result = self.fake_evolve(*args, **kwargs)
            result['skill_changed'] = False
            return result

        with self.assertRaisesRegex(ValueError, 'change status'):
            self.invoke_trace2skill(evolve=mislabeled)
        self.assertFalse((self.root / 'trace2skill/source_replay').exists())

    def test_smoke_no_change_remains_incomplete(self):
        opt = self.invoke_skillopt(train=self.unchanged_train, profile='smoke')
        trace = self.invoke_trace2skill(evolve=self.unchanged_evolve, profile='smoke')
        self.assertEqual(opt['status'], 'incomplete')
        self.assertEqual(trace['status'], 'incomplete')
        self.assertNotIn('selected_source_evaluation', opt)

    def test_skillpatch_experiment_processes_all_tasks_and_all_issues(self):
        outcome = {'status': 'completed', 'candidate_count': 0, 'replayed_count': 0, 'repaired_count': 0}
        with patch('set_up.appworld_smoke.skillpatch', return_value=outcome) as method:
            result = run_skillpatch(self.inputs, self.configs, self.root / 'patch', self.environment,
                                   profile='experiment')
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(result['validation_split_used'])
        self.assertEqual(method.call_args.kwargs['profile'], 'experiment')
        self.assertEqual(method.call_args.kwargs['task_limit'], 0)
        self.assertEqual(method.call_args.kwargs['max_issues_per_task'], 0)
        with patch('set_up.appworld_smoke.skillpatch', return_value=outcome) as method:
            run_skillpatch(self.inputs, self.configs, self.root / 'patch-smoke', self.environment)
        self.assertEqual(method.call_args.kwargs['profile'], 'smoke')
        self.assertEqual(method.call_args.kwargs['max_issues_per_task'], 1)


if __name__ == '__main__':
    unittest.main()
