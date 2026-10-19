"""Stage3 orchestration against archived foreign-benchmark fixtures only."""

import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skillpatch.discovery_validation import validate_report
from skillpatch.executor import Executor
from skillpatch.models import BackendRun
from skillpatch.patch_sets import PatchSetStore
from skillpatch.patch_set_rendering import render_patch_set
from skillpatch.patch_targets import bind_target
from skillpatch.patches import render_patch, tree_hashes, validate_generation
from skillpatch.stage3_config import Stage3Config
from skillpatch.stages.consolidation import _binding, run_stage3
from skillpatch.stages.patch_validation import run_stage2
from skillpatch.storage import write_json
import test_stage2 as stage2_fixtures
from test_discovery import compact_payload_fixture
from test_discovery_validation import make_payload
from test_stage2 import RecordedClient, TableBackend, candidate, assessment


def fixture_skill_text(task):
    unit = task.skills_dir / "table"
    return "\n".join(path.read_text() for path in (unit / "SKILL.md", unit / "SKILL_PATCHES.md")
                     if path.is_file())


class FollowupBackend(TableBackend):
    """A second source task needs another condition; old fixes still matter."""
    def run(self, request, task, run_dir, environment):
        if 'additional scenario' not in task.task_document.read_text():
            return super().run(request, task, run_dir, environment)
        self.calls.append((request, task, run_dir))
        text = fixture_skill_text(task)
        good = 'preserve duplicates' in text and 'additional condition' in text
        behavior = 'Preserved duplicate rows.' if good else 'Removed duplicate rows.'
        events = run_dir / 'backend/events.jsonl'
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text('Read the table.\n' + behavior + '\nSaved output.\n')
        return BackendRun('completed', return_code=0, normalized_trajectory_path=events,
                          trajectory_complete=True, trajectory_events=3,
                          summary={'loss': 0 if good else 1, 'behavior': behavior})


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixture preparation without inheriting and rerunning Stage2 tests.
        stage2_fixtures.Stage2Fixture.setUp(self)
        project = Path(__file__).resolve().parents[1]
        for name, prompt in [('consolidation', 'consolidation.md'),
                             ('preservation', 'consolidation_evaluation.md')]:
            (self.root / (name + '.toml')).write_text(
                '[diagnosis]\nmodel="fixture"\nbase_url="https://not-called.invalid"\n'
                'api_key_env="FIXTURE_KEY"\nprompt_path=' + json.dumps(str(project / 'prompts' / prompt)) + '\n')
        self.stage3_config = Stage3Config(self.root / 'consolidation.toml', self.root / 'preservation.toml',
                                         self.root / 'evaluation.toml')
        self.registry = self.root / 'registry'
        self.key = 'foreign/table-original'
        self.environment = {'FIXTURE_KEY': 'fixture-secret-value'}
        self.original_hashes = tree_hashes(self.source.prepared.skills_dir)

    def queue(self, name='episode', *, body=None, diagnosis=None):
        client = RecordedClient([body or candidate(), assessment()])
        result = run_stage2(diagnosis or self.diagnosis, 'C000001', self.config, self.root / name,
                            self.environment, client=client, adapter_factory=lambda _: self.adapter,
                            backend_factory=lambda _: self.backend)
        self.assertEqual(result.get('decision'), 'accepted', result)
        return self.root / name / 'stage3_queue.json'

    def integrate(self, queue, name='stage3', outputs=(), **kwargs):
        self.backend.calls.clear()
        client = RecordedClient(outputs)
        result = run_stage3(queue, self.key, self.registry, self.stage3_config, self.root / name,
                            self.environment, client=client, adapter_factory=lambda _: self.adapter,
                            backend_factory=lambda _: self.backend, **kwargs)
        return result, client

    def new_diagnosis(self, task, name):
        request = replace(self.request, task_dir=task)
        directory = self.root / name
        Executor(self.adapter, self.backend).run(request, directory, {})
        context = self.adapter.collect_evidence(directory)
        report = validate_report(compact_payload_fixture(make_payload()), context)
        diagnosis = directory / 'discovery/fixture'
        write_json(diagnosis / 'input.json', context)
        write_json(diagnosis / 'report.json', report)
        write_json(diagnosis / 'result.json', {'status': 'completed', 'run_id': directory.name})
        return diagnosis

    def test_fresh_queue_registers_exact_presentation_without_new_model_or_task(self):
        queue = self.queue()
        result, client = self.integrate(queue)
        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(result['reason'], 'stage2_inheritance')
        self.assertTrue(result['active_set_changed'])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])
        store = PatchSetStore(self.registry, self.key)
        current = store.current()
        self.assertEqual(current['version_id'], result['version_id'])
        queued = json.loads(queue.read_text())
        self.assertEqual(tree_hashes(store.skill_dir()),
                         tree_hashes(Path(queued['candidate_pack']) / 'table'))
        self.assertEqual(current['entries'][0]['entry_id'], queued['patch']['patch_id'])
        self.assertEqual(result['presentation_mode'], 'sidecar_v1')
        self.assertEqual((store.skill_dir() / 'SKILL.md').read_bytes(),
                         (store.original_dir / 'SKILL.md').read_bytes())
        self.assertTrue((store.skill_dir() / 'SKILL_PATCHES.md').is_file())
        self.assertEqual(tree_hashes(self.source.prepared.skills_dir), self.original_hashes)
        self.assertEqual((store.original_dir / 'helper.py').read_text(), 'original helper\n')

    def test_prepare_only_does_not_initialize_registry_or_call_models(self):
        queue = self.queue()
        result, client = self.integrate(queue, prepare_only=True)
        self.assertEqual(result['status'], 'prepared', result)
        self.assertEqual(result['decision'], 'prepared')
        self.assertFalse(self.registry.exists())
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])
        self.assertTrue((self.root / 'stage3/target_binding.json').is_file())

    def test_inline_acceptance_requires_new_validation_before_sidecar_registration(self):
        def render_legacy(source, candidate, destination):
            return render_patch(source, candidate, destination, presentation_mode="inline_v1")
        with patch('skillpatch.stages.patch_validation.render_patch', side_effect=render_legacy):
            queue = self.queue('legacy-inline-episode')

        result, client = self.integrate(queue, outputs=[assessment()])

        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(result['reason'], 'combination_validation')
        self.assertEqual(result['presentation_mode'], 'sidecar_v1')
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(len(client.calls), 1)
        store = PatchSetStore(self.registry, self.key)
        self.assertEqual((store.skill_dir() / 'SKILL.md').read_bytes(),
                         (store.original_dir / 'SKILL.md').read_bytes())

    def test_target_inside_historical_inline_patch_rebinds_to_same_sidecar_object(self):
        original = self.source.prepared.skills_dir / 'table'
        initial = validate_generation(candidate(), self.source, 0)
        binding = bind_target(original, self.key, 'SKILL.md', 'substep', initial.target.quote,
                              presentation_mode='inline_v1')
        entry = {'entry_id': initial.patch_id, 'target': binding,
                 'when_to_use': initial.when_to_use, 'how_to_interpret': initial.how_to_interpret,
                 'supports': [{'path': 'fixture', 'sha256': 'fixture'}]}
        deployed = self.root / 'legacy-pack/table'
        rendering = render_patch_set(original, [entry], deployed, presentation_mode='inline_v1')
        accepted = SimpleNamespace(
            patch=replace(initial, target=replace(initial.target, quote=initial.when_to_use)),
            source=replace(self.source, prepared=replace(self.source.prepared, skills_dir=deployed.parent)))
        version = {'version_id': 'legacy-fixture', 'skill_digest': rendering['digest'], 'rendering': rendering}

        target, provenance = _binding(accepted, original, self.key, [version])

        self.assertEqual(provenance['kind'], 'patch')
        self.assertEqual(target['target_id'], binding['target_id'])
        self.assertEqual(target['schema_version'], 2)
        self.assertNotIn('insertion_offset', target)
        render_patch_set(original, [{**entry, 'target': target}], self.root / 'new-sidecar')

    def test_output_inside_original_is_rejected_before_any_file_is_created(self):
        queue = self.queue()
        output = self.source.prepared.skills_dir / 'table/forbidden-stage3'
        before = tree_hashes(self.run_dir)
        with self.assertRaisesRegex(ValueError, 'outside original'):
            self.integrate(queue, name=str(output))
        self.assertFalse(output.exists())
        self.assertFalse(self.registry.exists())
        self.assertEqual(tree_hashes(self.run_dir), before)
        self.assertEqual(self.backend.calls, [])

    def test_new_regression_configuration_cannot_be_skipped_by_direct_inheritance(self):
        queue = self.queue()
        task = self.root / 'regression-task'
        shutil.copytree(self.request.task_dir, task)
        (task / 'instructions.txt').write_text('Preserve duplicate records for this regression task.')
        config = self.root / 'regression.toml'
        config.write_text('[task]\nbenchmark="tables"\npath="regression-task"\n'
                          '[agent]\nname="fixture-agent"\nmodel="fixture-model"\n'
                          '[runtime]\nbackend="table-engine"\n')
        self.stage3_config = replace(self.stage3_config, regression_configs=(config,))
        result, client = self.integrate(queue, outputs=[assessment(regression=True), assessment(regression=True)])
        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(result['reason'], 'requested_coverage_validation')
        self.assertEqual(len(self.backend.calls), 4)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result['regression_coverage']['configured_tasks'], 1)
        self.assertEqual(result['regression_coverage']['checked_pairs'], 1)

    def test_processed_queue_with_requested_regressions_checks_coverage_without_duplicate_support(self):
        queue = self.queue()
        first, _ = self.integrate(queue)
        store = PatchSetStore(self.registry, self.key)
        entries_before = store.current()['entries']
        task = self.root / 'regression-task'
        shutil.copytree(self.request.task_dir, task)
        (task / 'instructions.txt').write_text('Preserve duplicate records for this regression task.')
        config = self.root / 'regression.toml'
        config.write_text('[task]\nbenchmark="tables"\npath="regression-task"\n'
                          '[agent]\nname="fixture-agent"\nmodel="fixture-model"\n'
                          '[runtime]\nbackend="table-engine"\n')
        self.stage3_config = replace(self.stage3_config, regression_configs=(config,))
        result, client = self.integrate(queue, 'stage3-more-coverage',
                                        [assessment(regression=True), assessment(regression=True)])
        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(result['reason'], 'requested_coverage_validation')
        self.assertNotEqual(result['version_id'], first['version_id'])
        self.assertEqual(len(self.backend.calls), 4)
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn('proposal', result)
        self.assertEqual(store.current()['entries'], entries_before)
        self.assertEqual(result['regression_coverage']['checked_pairs'], 1)

    def test_repeated_queue_is_unchanged_without_duplicate_support(self):
        queue = self.queue()
        first, _ = self.integrate(queue)
        second, client = self.integrate(queue, 'stage3-repeat')
        self.assertEqual(second['decision'], 'unchanged', second)
        self.assertEqual(second['version_id'], first['version_id'])
        self.assertFalse(second['active_set_changed'])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(PatchSetStore(self.registry, self.key).current()['entries'][0]['supports']), 1)

    def test_same_body_separate_accepted_queue_adds_evidence_without_reinsertion(self):
        first_queue = self.queue()
        first, _ = self.integrate(first_queue)
        original_presentation = tree_hashes(PatchSetStore(self.registry, self.key).skill_dir())
        second_queue = self.queue('second-episode')
        second, client = self.integrate(second_queue, 'stage3-second')
        self.assertEqual(second['decision'], 'accepted', second)
        self.assertEqual(second['action'], 'deduplicate')
        self.assertNotEqual(first['version_id'], second['version_id'])
        current = PatchSetStore(self.registry, self.key).current()
        self.assertEqual(len(current['entries']), 1)
        self.assertEqual(len(current['entries'][0]['supports']), 2)
        self.assertEqual(tree_hashes(PatchSetStore(self.registry, self.key).skill_dir()), original_presentation)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(tree_hashes(self.source.prepared.skills_dir), self.original_hashes)

    def test_different_original_skill_version_remains_pending(self):
        initial_queue = self.queue()
        first, _ = self.integrate(initial_queue)
        task = self.root / 'foreign-task'
        shutil.copytree(self.request.task_dir, task)
        with (task / 'skills/table/SKILL.md').open('a') as stream:
            stream.write('\nDifferent original Skill instructions.\n')
        diagnosis = self.new_diagnosis(task, 'foreign-source')
        queue = self.queue('foreign-episode', diagnosis=diagnosis)
        result, client = self.integrate(queue, 'stage3-foreign')
        self.assertEqual(result['decision'], 'pending', result)
        self.assertIn('registered original', result['error'])
        self.assertEqual(PatchSetStore(self.registry, self.key).current()['version_id'], first['version_id'])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_old_base_queue_requires_combination_validation_and_cannot_reuse_its_old_improvement(self):
        first_queue = self.queue()
        first, _ = self.integrate(first_queue)
        old_queue = self.queue('old-base-episode', body=candidate('preserve duplicates for requested record preservation'))
        result, client = self.integrate(old_queue, 'stage3-old-base', [assessment(regression=True)])
        self.assertEqual(result['decision'], 'pending', result)
        self.assertEqual(result['reason'], 'new_combination_not_accepted')
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(PatchSetStore(self.registry, self.key).current()['version_id'], first['version_id'])
        self.assertNotIn('proposal', result)

    def test_merge_is_independently_checked_on_all_sources_and_can_keep_same_score(self):
        first_queue = self.queue()
        first, _ = self.integrate(first_queue)
        task = self.root / 'followup-task'
        shutil.copytree(self.request.task_dir, task)
        shutil.rmtree(task / 'skills/table')
        shutil.copytree(PatchSetStore(self.registry, self.key).skill_dir(), task / 'skills/table')
        (task / 'instructions.txt').write_text('Preserve duplicate records for this task. An additional scenario.')
        self.backend = FollowupBackend()
        diagnosis = self.new_diagnosis(task, 'followup-source')
        queue = self.queue('followup-episode', diagnosis=diagnosis,
                           body=candidate('preserve duplicates under the additional condition'))
        proposal = {'action': 'merge', 'reason': 'Preserve conditions required by both supporting sources.',
                    'when_to_use': 'preserve duplicates when preservation is requested, including the additional condition',
                    'how_to_interpret': 'Only deduplicate when uniqueness is required.'}
        result, client = self.integrate(queue, 'stage3-merge', [assessment(), assessment(regression=True),
                                                              proposal, assessment(regression=True),
                                                              assessment(regression=True)])
        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(result['action'], 'merge')
        self.assertEqual(result['reason'], 'preservation_validation')
        self.assertEqual(len(self.backend.calls), 8)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(result['combination_validation']['decision'], 'accepted')
        self.assertEqual({case['comparison']['status'] for case in result['preservation_validation']['cases']},
                         {'unchanged'})
        current = PatchSetStore(self.registry, self.key).current()
        self.assertEqual(len(current['entries']), 1)
        self.assertEqual(len(current['entries'][0]['replaces']), 2)
        self.assertEqual(len(current['entries'][0]['supports']), 2)
        self.assertEqual(current['parent_version'], first['version_id'])
        self.assertEqual(tree_hashes(self.source.prepared.skills_dir), self.original_hashes)


if __name__ == '__main__':
    unittest.main()
