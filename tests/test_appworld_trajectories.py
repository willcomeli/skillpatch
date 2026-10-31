"""Shared archives use the real launcher and a synthetic environment runner."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import sha256_file, tree_hashes, write_json
from set_up.appworld_trajectories import collect, load_manifest


class Runner:
    def __init__(self, alter=None, fail_at=None):
        self.alter = alter
        self.fail_at = fail_at
        self.calls = []
        self.closed = False

    def run_task(self, task, bank, **kwargs):
        self.calls.append((task, kwargs))
        if len(self.calls) == self.fail_at:
            raise RuntimeError('fixture transport failure')
        success = task['task_id'] == 'fixture_1'
        row = {'dataset': 'appworld', 'task_id': task['task_id'], 'split': task['split'],
               'instruction': 'Compare requested dates.', 'success': success, 'score': int(success),
               'task_completed': success, 'steps': 1,
               'retrieved_memory_text': bank.format_for_prompt({}),
               'trajectory': [{'action': 'Compare dates.', 'next_observation': 'Recorded result.'}],
               'evaluation_summary': {'num_tests': 1, 'passed_tests': int(success),
                                      'failed_tests': int(not success)}}
        if self.alter:
            self.alter(row)
        return row

    def close(self):
        self.closed = True


class SharedAppWorldTrajectoriesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / 'data'
        (self.data / 'datasets').mkdir(parents=True)
        (self.data / 'datasets/train.txt').write_text('fixture_1\nfixture_2\n')
        for task_id in ('fixture_1', 'fixture_2', 'test_1'):
            path = self.data / 'tasks' / task_id / 'specs.json'
            write_json(path, {})
        self.tasks = self.root / 'tasks.json'
        write_json(self.tasks, [{'dataset': 'appworld', 'task_id': task_id, 'split': 'train'}
                               for task_id in ('fixture_1', 'fixture_2')])
        self.skill = self.root / 'SKILL.md'
        self.skill.write_text('Normalize dates before comparing.\n')
        self.config = self.root / 'execution.toml'
        self.config.write_text('[task]\nbenchmark="appworld"\npath="tasks.json"\nskill_path="SKILL.md"\n'
                               '[agent]\nname="native"\nmodel="fixture"\n'
                               'api_key_env="DEEPSEEK_API_KEY"\nbase_url_env="DEEPSEEK_BASE_URL"\n'
                               '[runtime]\nbackend="eviskill"\n[runtime.runner_options]\n'
                               'data_dir="data"\nauto_server=true\nmax_steps=2\n')
        self.environment = {'DEEPSEEK_API_KEY': 'fixture-secret-value',
                            'DEEPSEEK_BASE_URL': 'https://fixture.example/v1'}

    def run_collection(self, output='collection', runner=None, **kwargs):
        runner = runner or Runner()
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=runner):
            result = collect(self.config, self.root / output, self.environment, **kwargs)
        return result, runner

    def test_freezes_same_skill_and_order_with_successes_and_failures(self):
        state, runner = self.run_collection()
        self.assertEqual(state['status'], 'completed', state)
        manifest = load_manifest(Path(state['manifest_path']))
        self.assertEqual([row['task_id'] for row in manifest['rows']], ['fixture_1', 'fixture_2'])
        self.assertEqual([row['success'] for row in manifest['rows']], [True, False])
        self.assertEqual((state['successes'], state['failures']), (1, 1))
        self.assertTrue(runner.closed)
        self.assertTrue(all(not kwargs for _, kwargs in runner.calls))
        config = json.loads(Path(manifest['execution_config']['path']).read_text())
        self.assertEqual(config['base_url'], self.environment['DEEPSEEK_BASE_URL'])
        for path in (self.root / 'collection').rglob('*'):
            if path.is_file():
                self.assertNotIn(self.environment['DEEPSEEK_API_KEY'], path.read_text())

    def test_limit_keeps_input_order_and_prepare_never_emits_frozen_manifest(self):
        output = self.root / 'prepare'
        with patch('excutor.eviskill_runners.launcher.create_runner') as create:
            state = collect(self.config, output, self.environment, limit=1, prepare_only=True)
        self.assertEqual(state['status'], 'prepared', state)
        create.assert_not_called()
        self.assertEqual(json.loads((output / 'execution/input/tasks.json').read_text())[0]['task_id'], 'fixture_1')
        self.assertFalse((output / 'trajectory_manifest.json').exists())
        self.assertEqual(state['task_count'], 1)
        state, runner = self.run_collection(limit=1)
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(load_manifest(Path(state['manifest_path']))['rows']), 1)

    def test_frozen_archive_survives_original_skill_and_task_edits(self):
        state, _ = self.run_collection()
        output = self.root / 'collection'
        before = tree_hashes(output)
        self.skill.write_text('A different original skill.')
        write_json(self.tasks, [])
        self.config.write_text('changed original config')
        manifest = load_manifest(Path(state['manifest_path']))
        self.assertEqual(len(manifest['rows']), 2)
        self.assertEqual(tree_hashes(output), before)

    def test_each_indexed_input_hash_is_verified(self):
        for key in ('initial_skill', 'execution_config', 'source_run', 'tasks_snapshot', 'row'):
            with self.subTest(key=key):
                state, _ = self.run_collection(output=key)
                manifest_path = Path(state['manifest_path'])
                manifest = load_manifest(manifest_path)
                path = Path(manifest['rows'][0]['source'] if key == 'row' else manifest[key]['path'])
                path.write_text(path.read_text() + '\n')
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    load_manifest(manifest_path)

    def test_manifest_cannot_reorder_omit_duplicate_or_rescore_rows(self):
        state, _ = self.run_collection()
        manifest_path = Path(state['manifest_path'])
        original = manifest_path.read_text()
        alterations = [lambda m: m['rows'].reverse(), lambda m: m['rows'].pop(),
                       lambda m: m['rows'].__setitem__(1, m['rows'][0]),
                       lambda m: m['rows'][1].update(success=True, score=1)]
        for alter in alterations:
            with self.subTest(alter=alter):
                manifest = json.loads(original)
                alter(manifest)
                write_json(manifest_path, manifest)
                with self.assertRaises(ValueError):
                    load_manifest(manifest_path)

    def test_partial_batch_never_freezes_completed_subset(self):
        state, runner = self.run_collection(runner=Runner(fail_at=2))
        self.assertEqual(state['status'], 'failed', state)
        self.assertFalse((self.root / 'collection/trajectory_manifest.json').exists())
        self.assertTrue((self.root / 'collection/execution/tasks/task_000001.json').is_file())
        self.assertTrue(runner.closed)

    def test_invalid_feedback_mixed_skill_or_patched_records_cannot_freeze(self):
        alterations = {
            'invalid_feedback': lambda row: row['evaluation_summary'].update(num_tests=0),
            'missing_evaluation': lambda row: row.pop('evaluation_summary'),
            'missing_counts': lambda row: row['evaluation_summary'].pop('num_tests'),
            'missing_score': lambda row: row.pop('score'),
            'changed_skill': lambda row: row.update(retrieved_memory_text='Changed Skill.'),
            'patched': lambda row: row.update(patch_deployment={'patch_id': 'p1'}),
            'wrong_task': lambda row: row.update(task_id='test_1'),
            'wrong_split': lambda row: row.update(split='dev'),
        }
        for name, alter in alterations.items():
            with self.subTest(name=name):
                state, _ = self.run_collection(output=name, runner=Runner(alter=alter))
                self.assertEqual(state['status'], 'failed', state)
                self.assertFalse((self.root / name / 'trajectory_manifest.json').exists())

    def test_bad_train_refs_rejected_before_runner_or_output_creation(self):
        cases = [[], [{'task_id': 'fixture_1', 'split': 'dev'}],
                 [{'task_id': 'test_1', 'split': 'train'}],
                 [{'task_id': 'fixture_1', 'split': 'train'}] * 2,
                 ['fixture_1'], [{'task_id': 'fixture_1', 'split': 'train', 'variant': 2}]]
        for tasks in cases:
            with self.subTest(tasks=tasks):
                write_json(self.tasks, tasks)
                with patch('excutor.eviskill_runners.launcher.create_runner') as create:
                    with self.assertRaises(ValueError):
                        collect(self.config, self.root / 'bad', self.environment, limit=1)
                create.assert_not_called()
                self.assertFalse((self.root / 'bad').exists())

    def test_fresh_output_and_known_secret_in_snapshot(self):
        state, _ = self.run_collection()
        before = tree_hashes(self.root / 'collection')
        with self.assertRaises(FileExistsError):
            self.run_collection()
        self.assertEqual(tree_hashes(self.root / 'collection'), before)
        self.skill.write_text(self.environment['DEEPSEEK_API_KEY'])
        with self.assertRaisesRegex(ValueError, 'secret'):
            self.run_collection(output='secret')
        self.assertFalse((self.root / 'secret').exists())

    def test_rehashed_invalid_batch_or_snapshot_is_still_rejected(self):
        state, _ = self.run_collection()
        manifest_path = Path(state['manifest_path'])
        manifest = load_manifest(manifest_path)
        batch_path = Path(manifest['source_run']['path'])
        batch = json.loads(batch_path.read_text())
        batch['task_results'].reverse()
        write_json(batch_path, batch)
        manifest['source_run']['sha256'] = sha256_file(batch_path)
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, 'task_results'):
            load_manifest(manifest_path)


if __name__ == '__main__':
    unittest.main()
