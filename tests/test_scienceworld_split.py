"""Build executable cohorts from the pinned public SkillNet task identities."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from excutor.scienceworld_runners.runner import _task_ref


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'scienceworld_split_builder', ROOT / 'data/scienceworld/build_scienceworld_split.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ScienceWorldSplitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = ROOT / 'data/scienceworld/raw/skillnet'

    def args(self, **overrides):
        values = dict(source_dir=self.source, output_dir=self.root / 'output',
                      source_format='skillnet-indices', simplification='easy',
                      step_budget='skillnet', allow_source_mismatch=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_pinned_cohorts_produce_executable_records_and_disjoint_splits(self):
        result = builder.build(self.args())
        self.assertEqual(result['counts'], {'train': 96, 'val': 48, 'test': 211})
        self.assertTrue(result['source_hashes_verified'])
        self.assertEqual(result['reconstruction']['status'], 'reconstructed')
        self.assertFalse(result['reconstruction']['eviskill_wrapper_hashes_verified'])
        train = builder.load_json(self.root / 'output/train/items.json')
        val = builder.load_json(self.root / 'output/val/items.json')
        test = builder.load_json(self.root / 'output/test/items.json')
        self.assertEqual([row['skillnet_index'] for row in train], builder.MAIN_TRAIN_INDICES)
        self.assertEqual([row['skillnet_index'] for row in val], builder.MAIN_VAL_INDICES)
        self.assertEqual((train[0]['task'], train[0]['variation_idx']),
                         ('task-6-lifespan-(shortest-lived)', 6))
        self.assertEqual((val[0]['task'], val[0]['variation_idx']), ('task-4-grow-fruit', 80))
        self.assertEqual([[row['task'], row['variation_idx']] for row in test],
                         builder.load_json(self.source / 'test_indices.json'))
        identities = [{(row['task'], row['variation_idx']) for row in rows}
                      for rows in (train, val, test)]
        self.assertFalse(identities[0] & identities[1])
        self.assertFalse(identities[0] & identities[2])
        self.assertFalse(identities[1] & identities[2])
        for row in [*train, *val, *test]:
            ref = _task_ref(row)
            self.assertEqual(ref['task'], row['task'])
            self.assertEqual(ref['variation_idx'], row['variation_idx'])
            self.assertEqual(ref['simplification'], 'easy')
            self.assertGreater(ref['recommended_max_steps'], 0)

    def test_runner_budget_leaves_per_task_override_unset(self):
        result = builder.build(self.args(step_budget='runner', simplification=''))
        train = builder.load_json(self.root / 'output/train/items.json')
        self.assertEqual(result['reconstruction']['step_budget'], 'runner')
        self.assertNotIn('max_steps', result['source_sha256'])
        self.assertNotIn('recommended_max_steps', train[0])
        self.assertEqual(_task_ref(train[0])['simplification'], '')

    def test_modified_sources_are_rejected_before_output(self):
        source = self.root / 'modified'
        shutil.copytree(self.source, source)
        path = source / 'train_indices.json'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            builder.build(self.args(source_dir=source))
        self.assertFalse((self.root / 'output').exists())

    def test_cross_split_overlap_is_rejected_even_when_source_change_is_allowed(self):
        source = self.root / 'modified'
        shutil.copytree(self.source, source)
        train = builder.load_json(source / 'train_indices.json')
        dev = builder.load_json(source / 'valid_indices.json')
        repeated = train[builder.MAIN_TRAIN_INDICES[0]]
        index = next(i for i in builder.MAIN_VAL_INDICES if dev[i][0] == repeated[0])
        dev[index] = repeated
        (source / 'valid_indices.json').write_text(json.dumps(dev))
        with self.assertRaisesRegex(ValueError, 'overlap'):
            builder.build(self.args(source_dir=source, allow_source_mismatch=True))
        self.assertFalse((self.root / 'output').exists())

    def test_reconstruction_requires_explicit_execution_settings(self):
        with self.assertRaisesRegex(ValueError, 'explicit'):
            builder.build(self.args(simplification=None))
        self.assertFalse((self.root / 'output').exists())

    def test_legacy_manifest_mode_preserves_existing_task_records(self):
        builder.build(self.args())
        source = self.root / 'legacy'
        source.mkdir()
        for name, split in [('train', 'train'), ('dev', 'val'), ('test', 'test')]:
            shutil.copyfile(self.root / f'output/{split}/items.json', source / f'{name}.json')
        output = self.root / 'legacy-output'
        result = builder.build(self.args(source_dir=source, output_dir=output,
            source_format='eviskill-manifests', simplification=None, step_budget=None,
            allow_source_mismatch=True))
        self.assertNotIn('reconstruction', result)
        self.assertFalse(result['source_hashes_verified'])
        for split in ('train', 'val', 'test'):
            self.assertEqual((output / split / 'items.json').read_bytes(),
                             (self.root / 'output' / split / 'items.json').read_bytes())
