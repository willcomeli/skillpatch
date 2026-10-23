"""Native result mappings preserve environment-specific observations and feedback."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from method_input_adapters.skillpatch import build_diagnosis_context, get_diagnosis_adapter, register_diagnosis_adapter
from context import render_diagnosis_context


class NativeDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = {
            'dataset': 'appworld', 'task_id': 'fixture_1', 'instruction': 'Create the report.',
            'success': False, 'score': 0.0, 'steps': 1,
            'retrieved_memory_text': 'Original Skill\nSupplement: check dates.',
            'retrieved_skill_ids': ['skill-a'],
            'trajectory': [{'step': 0, 'observation': 'start', 'thought': 'inspect',
                            'action': 'print(42)', 'next_observation': '42', 'done': True}],
            'task_completed': True, 'evaluation_summary': {'passed': False},
        }

    def save(self, row=None, name='task.json'):
        path = self.root / name
        path.write_text(json.dumps(self.row if row is None else row))
        return path

    def test_all_three_map_to_the_same_context_structure(self):
        for dataset in ('alfworld', 'scienceworld', 'appworld'):
            with self.subTest(dataset=dataset):
                row = {**self.row, 'dataset': dataset}
                path = self.save(row)
                context = build_diagnosis_context(path)
                self.assertEqual(context.benchmark, dataset)
                self.assertEqual([i.kind for i in context.items], ['task', 'skill', 'trajectory', 'feedback'])
                self.assertEqual(context.items[1].text, row['retrieved_memory_text'])
                self.assertEqual(json.loads(context.items[2].text), row['trajectory'][0])
                self.assertEqual(context.items[2].observation['json_pointer'], '/trajectory/0')
                self.assertIsNone(context.trajectory_complete)
                self.assertIn('Create the report.', render_diagnosis_context(context))

    def test_directory_and_file_inputs_match(self):
        path = self.save(name='result.json')
        self.assertEqual(build_diagnosis_context(path), build_diagnosis_context(self.root))

    def test_scienceworld_score_availability_is_preserved(self):
        row = copy.deepcopy(self.row)
        row['dataset'] = 'scienceworld'
        row['trajectory'][0].update(score_delta=None, score_available=False)
        context = build_diagnosis_context(self.save(row))
        step = json.loads(context.items[2].text)
        self.assertIsNone(step['score_delta'])
        self.assertFalse(step['score_available'])

    def test_appworld_completion_is_distinct_from_success(self):
        context = build_diagnosis_context(self.save())
        feedback = json.loads(context.items[-1].text)
        self.assertTrue(feedback['task_completed'])
        self.assertFalse(feedback['success'])

    def test_missing_skill_and_score_are_not_fabricated(self):
        row = copy.deepcopy(self.row)
        del row['score'], row['retrieved_memory_text']
        context = build_diagnosis_context(self.save(row))
        self.assertIsNone(context.skill_digest)
        self.assertFalse(any(i.kind == 'skill' for i in context.items))
        self.assertNotIn('score', json.loads(context.items[-1].text))
        self.assertTrue(any('No final score' in line for line in context.limitations))

    def test_wrong_dataset_and_invalid_records_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Expected dataset'):
            get_diagnosis_adapter('alfworld').build_diagnosis_context(self.save())
        for changes in ({'steps': 2}, {'trajectory': 'text'}, {'instruction': ''},
                        {'status': 'in_progress'}, {'trajectory_complete': 'yes'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_diagnosis_context(self.save({**self.row, **changes}))

    def test_batches_and_nonfinite_numbers_are_rejected(self):
        for data in ([self.row], {**self.row, 'score': float('nan')},
                     {**self.row, 'score': float('inf')}):
            with self.subTest(data=type(data)), self.assertRaises(ValueError):
                build_diagnosis_context(self.save(data))
        path = self.save()
        path.write_text(path.read_text().replace('"score": 0.0', '"score": 1e999'))
        with self.assertRaises(ValueError):
            build_diagnosis_context(path)

    def test_content_changes_update_digest_and_sources(self):
        path = self.save()
        before = build_diagnosis_context(path)
        row = copy.deepcopy(self.row)
        row['trajectory'][0]['next_observation'] = 'different'
        after = build_diagnosis_context(self.save(row))
        self.assertNotEqual(before.input_digest, after.input_digest)
        self.assertNotEqual(before.items[0].version, after.items[0].version)
        self.assertEqual(before.task_digest, after.task_digest)

    def test_builtin_names_cannot_be_overwritten(self):
        for name in ('alfworld', 'scienceworld', 'appworld'):
            with self.assertRaises(ValueError):
                register_diagnosis_adapter(name, lambda: None)

    def test_success_flag_retains_unknown_and_conflicting_outcomes(self):
        for success,score,expected in ((True,1,True),(False,0,False),(True,0,None),(None,0,None)):
            row={**self.row,'success':success,'score':score}
            context=build_diagnosis_context(self.save(row))
            self.assertIs(context.task_succeeded,expected)
