"""Frozen AppWorld training inputs with live SkillOpt validation and later rollout."""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import sha256_file, tree_hashes, write_json
from method_input_adapters.execution import SharedExecutorBridge, load_tasks
import test_appworld_trajectories as shared_fixtures


@unittest.skipUnless(importlib.util.find_spec('skillopt'), 'Use .venvs/methods for SkillOpt')
class SkillOptFrozenTrajectoryTests(unittest.TestCase):
    run_collection = shared_fixtures.SharedAppWorldTrajectoriesTests.run_collection

    def setUp(self):
        shared_fixtures.SharedAppWorldTrajectoriesTests.setUp(self)
        state, _ = self.run_collection()
        self.assertEqual(state['status'], 'completed', state)
        self.frozen_manifest = Path(state['manifest_path'])
        self.manifest_data = json.loads(self.frozen_manifest.read_text())
        self.method_manifest = self.root / 'method_tasks.json'
        self.train_entry = {'id': 'training', 'split': 'train', 'config': str(self.config),
                            'task_ref': {'dataset': 'appworld', 'task_id': 'fixture_2', 'split': 'train'}}
        self.val_entry = {'id': 'validation', 'split': 'val', 'config': str(self.config),
                          'task_ref': {'dataset': 'appworld', 'task_id': 'dev_1', 'split': 'dev'}}
        (self.data / 'datasets/dev.txt').write_text('dev_1\n')
        write_json(self.data / 'tasks/dev_1/specs.json', {})
        write_json(self.method_manifest, [self.train_entry, self.val_entry])
        self.live_calls = []
        outer = self

        class Bridge(SharedExecutorBridge):
            def run(self, entry, skill_content, output, **kwargs):
                outer.live_calls.append((entry['id'], skill_content))
                return super().run(entry, skill_content, output, **kwargs)

        self.bridge = Bridge(self.environment)

        class Runner:
            def run_task(self, task, bank):
                skill = bank.format_for_prompt({})
                success = 'Check date boundaries.' in skill
                return dict(dataset='appworld', task_id=task['task_id'], split=task['split'],
                            instruction='Compare requested dates.', success=success, score=int(success),
                            task_completed=success, steps=1, retrieved_memory_text=skill,
                            trajectory=[dict(action='Compare dates.', next_observation='Recorded.')],
                            evaluation_summary=dict(num_tests=1, passed_tests=int(success),
                                                    failed_tests=int(not success)))

            def close(self):
                pass

        runner_patch = patch('excutor.eviskill_runners.launcher.create_runner', side_effect=lambda *args: Runner())
        runner_patch.start()
        self.addCleanup(runner_patch.stop)

    def adapter(self, **kwargs):
        from method_input_adapters.skillopt.adapter import SharedExecutionAdapter
        return SharedExecutionAdapter(kwargs.pop('tasks', load_tasks(self.method_manifest)), self.bridge,
                                      frozen_trajectory_manifest=self.frozen_manifest,
                                      initial_skill=kwargs.pop('initial_skill', self.skill), **kwargs)

    def test_validation_candidate_and_later_initial_rollouts_are_live(self):
        before = tree_hashes(self.frozen_manifest.parent)
        adapter = self.adapter()
        skill = self.skill.read_text()
        adapter.rollout([self.val_entry], skill, self.root / 'baseline', use_eval_feedback=True)
        adapter.rollout([self.train_entry], skill + 'Check date boundaries.', self.root / 'candidate')
        self.assertEqual([entry[0] for entry in self.live_calls], ['validation', 'training'])
        rows = adapter.rollout([self.train_entry], skill, self.root / 'frozen', use_eval_feedback=True)
        self.assertEqual(rows[0]['original_task_id'], 'fixture_2')
        self.assertEqual(len(self.live_calls), 2)
        reuse = json.loads((self.root / 'frozen/frozen_trajectory_reuse.json').read_text())
        self.assertEqual(reuse['manifest_sha256'], sha256_file(self.frozen_manifest))
        self.assertEqual(reuse['rows'][0]['source'], self.manifest_data['rows'][1]['source'])
        adapter.rollout([self.train_entry], skill, self.root / 'later', use_eval_feedback=True)
        self.assertEqual(len(self.live_calls), 3)
        self.assertFalse((self.root / 'later/frozen_trajectory_reuse.json').exists())
        self.assertEqual(tree_hashes(self.frozen_manifest.parent), before)

    def test_upstream_trainer_uses_frozen_first_step_and_live_second_epoch(self):
        from method_input_adapters.skillopt.training import train
        settings = self.root / 'skillopt.yaml'
        settings.write_text('train:\n  num_epochs: 2\n  batch_size: 1\n  accumulation: 1\n'
                            'gradient:\n  minibatch_size: 1\n  analyst_workers: 1\n'
                            'optimizer:\n  use_slow_update: false\n  use_meta_skill: false\n'
                            '  learning_rate: 2\n  min_learning_rate: 1\n  lr_scheduler: constant\n'
                            'evaluation:\n  sel_env_num: 1\n  eval_test: false\n')
        patches = [{'source_type': 'failure', 'batch_size': 1,
                    'patch': {'reasoning': 'Missing precondition.',
                              'edits': [{'op': 'append', 'content': 'Check date boundaries.'}]}}]
        with patch('method_input_adapters.skillopt.training.SharedExecutorBridge', return_value=self.bridge), \
                patch('skillopt.gradient.reflect.run_minibatch_reflect', return_value=patches), \
                patch('skillopt.gradient.aggregate.chat_optimizer', side_effect=AssertionError('Unexpected LLM call')), \
                patch('skillopt.optimizer.clip.chat_optimizer', side_effect=AssertionError('Unexpected LLM call')):
            summary = train(self.method_manifest, self.skill, self.root / 'training', self.environment,
                            config=settings, model='fixture', frozen_trajectory_manifest=self.frozen_manifest)
        self.assertEqual(summary['best_selection_hard'], 1)
        self.assertEqual(self.live_calls[0], ('validation', self.skill.read_text()))
        training_calls = [entry for entry in self.live_calls if entry[0] == 'training']
        self.assertEqual(len(training_calls), 1)
        self.assertIn('Check date boundaries.', training_calls[0][1])
        self.assertTrue(any(entry[0] == 'validation' and 'Check date boundaries.' in entry[1]
                            for entry in self.live_calls))
        self.assertTrue((self.root / 'training/steps/step_0001/rollout/frozen_trajectory_reuse.json').exists())
        self.assertFalse((self.root / 'training/steps/step_0002/rollout/frozen_trajectory_reuse.json').exists())
        self.assertIn('Check date boundaries.', (self.root / 'training/best_skill.md').read_text())

    def test_changed_initial_skill_is_rejected_before_calls(self):
        other = self.root / 'other.md'
        other.write_text('Different instructions.')
        with self.assertRaisesRegex(ValueError, 'Initial Skill'):
            self.adapter(initial_skill=other)
        self.assertEqual(self.live_calls, [])

    def test_changed_model_budget_and_endpoint_are_rejected(self):
        original = self.config.read_text()
        for old, new in [('model="fixture"', 'model="another"'), ('max_steps=2', 'max_steps=3')]:
            with self.subTest(new=new):
                self.config.write_text(original.replace(old, new))
                with self.assertRaisesRegex(ValueError, 'settings differ'):
                    self.adapter()
                self.config.write_text(original)
        self.bridge.environment['DEEPSEEK_BASE_URL'] = 'https://another.invalid'
        with self.assertRaisesRegex(ValueError, 'endpoint differs'):
            self.adapter()
        self.assertEqual(self.live_calls, [])

    def test_wrong_task_split_identity_and_overlap_are_rejected(self):
        for change in [dict(split='dev'), dict(dataset='alfworld'), dict(task_id='dev_1'), dict(variant=1)]:
            with self.subTest(change=change):
                entry = {**self.train_entry, 'task_ref': {**self.train_entry['task_ref'], **change}}
                with self.assertRaises(ValueError):
                    self.adapter(tasks=[entry, self.val_entry])
        overlapping = {**self.val_entry, 'task_ref': self.train_entry['task_ref']}
        with self.assertRaisesRegex(ValueError, 'disjoint'):
            self.adapter(tasks=[self.train_entry, overlapping])
        duplicate = {**self.train_entry, 'id': 'another'}
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.adapter(tasks=[self.train_entry, duplicate, self.val_entry])
        self.assertEqual(self.live_calls, [])

    def test_tampered_frozen_source_and_incomplete_manifest_are_rejected(self):
        original = self.frozen_manifest.read_text()
        manifest = json.loads(original)
        manifest['rows'].pop(0)
        write_json(self.frozen_manifest, manifest)
        with self.assertRaisesRegex(ValueError, 'complete source task batch'):
            self.adapter()
        self.frozen_manifest.write_text(original)
        source = Path(self.manifest_data['rows'][1]['source'])
        source.write_text(source.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.adapter()
        self.assertEqual(self.live_calls, [])

    def test_mutation_after_binding_is_detected_before_reuse(self):
        adapter = self.adapter()
        self.frozen_manifest.write_text(self.frozen_manifest.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            adapter.rollout([self.train_entry], self.skill.read_text(), self.root / 'frozen', use_eval_feedback=True)
        self.assertEqual(self.live_calls, [])
        self.assertFalse((self.root / 'frozen').exists())

    def test_changed_training_configuration_after_binding_is_detected(self):
        adapter = self.adapter()
        self.config.write_text(self.config.read_text().replace('max_steps=2', 'max_steps=3'))
        with self.assertRaisesRegex(ValueError, 'settings differ'):
            adapter.rollout([self.train_entry], self.skill.read_text(), self.root / 'frozen', use_eval_feedback=True)
        self.assertEqual(self.live_calls, [])

    def test_noninitial_first_training_skill_cannot_consume_archive(self):
        adapter = self.adapter()
        with self.assertRaisesRegex(ValueError, 'exact frozen initial Skill'):
            adapter.rollout([self.train_entry], 'Changed Skill.', self.root / 'frozen', use_eval_feedback=True)
        self.assertEqual(self.live_calls, [])

    def test_output_cannot_modify_shared_archive(self):
        adapter = self.adapter()
        before = tree_hashes(self.frozen_manifest.parent)
        with self.assertRaisesRegex(ValueError, 'outside'):
            adapter.rollout([self.train_entry], self.skill.read_text(),
                            self.frozen_manifest.parent / 'method', use_eval_feedback=True)
        self.assertEqual(tree_hashes(self.frozen_manifest.parent), before)
        self.assertEqual(self.live_calls, [])


if __name__ == '__main__':
    unittest.main()
