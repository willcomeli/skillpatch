"""Consumer contracts, honest scores, split isolation and candidate reruns."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from method_input_adapters.archive import is_scored_agent_timeout, load_archive, read_json, write_json
from method_input_adapters.skillopt import export_results, create_adapter
from method_input_adapters.execution import load_tasks
from method_input_adapters.execution import SharedExecutorBridge


class ResultAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def record(self, dataset='alfworld', **changes):
        data = dict(dataset=dataset, task_id='task/one', instruction='Deliver the apple.',
                    success=False, score=0.0, retrieved_memory_text='Check the inventory.', steps=1,
                    trajectory=[dict(step=0, observation='An apple is here.', action='take apple',
                                     next_observation='Apple taken.', thought='I need to carry it.')])
        data.update(changes)
        path = self.root / (dataset + '.json')
        write_json(path, data)
        return path

    def test_jsonl_unicode_line_separators_preserve_all_saved_events(self):
        from test_diagnosis_adapter import DiscoveryContextTests
        from excutor.executor_adapter.storage import tree_hashes
        fixture = DiscoveryContextTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        text = 'Excel text\x85next cell\u2028next paragraph\u2029final'
        fixture.raw_events[1]['content'] = [{'type': 'text', 'text': text}]
        fixture.write_events()
        fixture.write(fixture.canonical, '\n'.join(json.dumps(e, ensure_ascii=False) for e in fixture.raw_events))
        fixture.write(fixture.events, '\n'.join(json.dumps(e, ensure_ascii=False) for e in fixture.normalized))
        before = tree_hashes(fixture.root)
        archive = load_archive(fixture.root)
        self.assertEqual(archive.conversation[1]['event'], fixture.raw_events[1])
        self.assertEqual(len(archive.conversation), len(fixture.raw_events) + 1)
        self.assertFalse(any('malformed' in item for item in archive.limitations))
        self.assertEqual(tree_hashes(fixture.root), before)

    def scored_timeout(self, score=0):
        return {'benchmark': 'skillsbench', 'task_id': 'one', 'status': 'timeout',
                'errors': [{'category': 'agent', 'code': 'timeout', 'message': 'Deadline exceeded'},
                           {'category': 'agent', 'code': 'agent_timeout_info',
                            'message': json.dumps({'reason': 'wall_clock_timeout', 'timeout_sec': 900})}],
                'execution': {'status': 'timeout', 'normalized_trajectory_path': 'events.jsonl'},
                'feedback': {'status': 'scored', 'metrics': {'reward': score}, 'issues': []}}

    def test_scored_timeout_preserves_official_score_and_original_status_for_both_methods(self):
        from method_input_adapters.trace2skill.results import export_results as export_trace
        source = self.root / 'timeout'
        source.mkdir()
        (source / 'events.jsonl').write_text(json.dumps({'event': {'type': 'user_message', 'text': 'Solve it'}}) + '\n')
        for score in (0, 0.5, 1):
            with self.subTest(score=score):
                raw = self.scored_timeout(score)
                write_json(source / 'result.json', raw)
                with patch('method_input_adapters.skillpatch.adapter.skillsbench.SkillsBenchDiagnosisAdapter.build_diagnosis_context',
                           return_value=SimpleNamespace(limitations=[])):
                    archive = load_archive(source)
                    opt = export_results([source], self.root / f'opt-{score}')
                    trace = export_trace([source], self.root / f'trace-{score}')
                self.assertEqual(archive.score, score)
                self.assertEqual(archive.success, score == 1)
                self.assertEqual(opt[0]['soft'], score)
                self.assertEqual(trace[0]['score'], score)
                self.assertEqual(read_json(source / 'result.json'), raw)

    def test_infrastructure_and_unscored_timeouts_are_not_accepted(self):
        for category, code in [('backend', 'execution_timeout'), ('agent', 'install_timeout'),
                               ('agent', 'connection_error'), ('verifier', 'verifier_timeout_info')]:
            raw = self.scored_timeout()
            raw['errors'].append({'category': category, 'code': code})
            self.assertFalse(is_scored_agent_timeout(raw))
        for score in (None, -1, 1.1, float('nan')):
            self.assertFalse(is_scored_agent_timeout(self.scored_timeout(score)))
        raw = self.scored_timeout()
        raw['errors'][1]['message'] = json.dumps({'reason': 'connection_error'})
        self.assertFalse(is_scored_agent_timeout(raw))

    def test_three_native_consumers_receive_successes_and_full_actions(self):
        sources = [self.record(name, success=True, score=1.0) for name in ('alfworld', 'scienceworld', 'appworld')]
        rows = export_results(sources, self.root / 'export')
        self.assertEqual([row['hard'] for row in rows], [1, 1, 1])
        self.assertEqual(len({row['id'] for row in rows}), 3)
        for row in rows:
            conv = read_json(self.root / 'export/predictions' / row['id'] / 'conversation.json')
            self.assertEqual(conv[2]['action'], 'take apple')
            self.assertEqual(conv[2]['env_feedback'], 'Apple taken.')
            self.assertEqual(conv[2]['reasoning'], 'I need to carry it.')

    def test_missing_unavailable_nonfinite_and_conflicting_scores_rejected(self):
        for changes in ({'score': None}, {'score': -1}, {'score': 1.1}, {'success': None},
                        {'score_available': False}, {'success': True, 'score': 0},
                        {'trajectory': [{'score_after': None}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                load_archive(self.record('scienceworld', **changes))
        path = self.record()
        data = read_json(path); del data['score']; write_json(path, data)
        with self.assertRaises(ValueError):
            load_archive(path)

    def test_duplicate_runs_need_explicit_ids(self):
        path = self.record()
        with self.assertRaises(ValueError):
            export_results([path, path], self.root / 'duplicate')
        rows = export_results([path, path], self.root / 'repeated', ids=['first', 'second'])
        self.assertEqual([row['id'] for row in rows], ['first', 'second'])
        with self.assertRaises(ValueError):
            export_results([path], self.root / 'unsafe', ids=['../escape'])

    def test_manifest_rejects_task_overlap_even_with_different_ids(self):
        tasks = self.root / 'tasks.json'; write_json(tasks, ['task-one'])
        config = self.root / 'run.toml'
        config.write_text('[task]\nbenchmark="alfworld"\npath="tasks.json"\nskill_mode="no-skill"\n[agent]\nmodel="fixture"\n')
        manifest = self.root / 'manifest.json'
        write_json(manifest, [dict(id='a', split='train', config='run.toml'),
                              dict(id='b', split='val', config='run.toml')])
        with self.assertRaises(ValueError):
            load_tasks(manifest)


@unittest.skipUnless(importlib.util.find_spec('skillopt'), 'Use .venvs/methods for upstream SkillOpt integration')
class SkillOptIntegrationTests(unittest.TestCase):
    setUp = ResultAdapterTests.setUp

    def test_manifest_epochs_cover_all_tasks_and_resume_exact_batches(self):
        tasks = [dict(id=f'train-{i}', split='train') for i in range(96)]
        tasks += [dict(id=f'{split}-{i}', split=split)
                  for split, count in [('val', 48), ('test', 211)] for i in range(count)]
        adapter = create_adapter(tasks, None)
        adapter.setup({})
        loader = adapter.get_dataloader()
        self.assertEqual(loader.get_train_size(), 96)
        orders = []
        for epoch in range(1, 5):
            kwargs = dict(epoch=epoch, steps_per_epoch=3, accumulation=1,
                          batch_size=40, seed=42)
            batches = loader.plan_train_epoch(**kwargs)
            self.assertEqual([b.batch_size for b in batches], [40, 40, 16])
            rows = [row for b in batches for row in adapter.build_env_from_batch(b)]
            ids = [row['id'] for row in rows]
            self.assertEqual(len(set(ids)), 96)
            self.assertEqual(set(ids), {f'train-{i}' for i in range(96)})
            orders.append(ids)
            # A new process/adapter at a step checkpoint must plan the same
            # remaining payloads, not draw a fresh subset using batch.seed.
            resumed = create_adapter(tasks, None)
            resumed.setup({})
            replay = resumed.get_dataloader().plan_train_epoch(**kwargs)
            self.assertEqual(batches, replay)
            for original, restored in zip(batches[1:], replay[1:]):
                self.assertEqual(adapter.build_env_from_batch(original),
                                 resumed.build_env_from_batch(restored))
        self.assertNotEqual(orders[0], orders[1])
        for split, canonical, count in [('selection', 'val', 48),
                                         ('valid_seen', 'val', 48),
                                         ('valid_unseen', 'test', 211)]:
            batch = loader.build_eval_batch(env_num=count, split=split, seed=42)
            rows = adapter.build_env_from_batch(batch)
            self.assertEqual(len(rows), count)
            self.assertTrue(all(row['split'] == canonical for row in rows))

    def test_trainer_routes_candidate_and_heldout_validation_to_bridge(self):
        from skillopt.config import load_config, flatten_config
        from skillopt.engine.trainer import ReflACTTrainer
        root = Path(__file__).resolve().parents[1]
        cfg = flatten_config(load_config(str(root / 'method/skillopt/configs/_base_/default.yaml')))
        skill = self.root / 'initial.md'; skill.write_text('# Procedure\nDo the task.\n')
        cfg.update(out_root=str(self.root / 'train'), skill_init=str(skill), train_size=1,
                   num_epochs=1, batch_size=1, accumulation=1, merge_batch_size=2,
                   analyst_workers=1, minibatch_size=1, model_backend='openai_compatible',
                   optimizer_backend='openai_compatible', target_backend='openai_compatible',
                   use_slow_update=False, use_meta_skill=False, eval_test=False, sel_env_num=1,
                   edit_budget=2, min_edit_budget=1, lr_scheduler='constant')
        calls = []
        class Bridge:
            def run(self, entry, text, output):
                output = Path(output); output.mkdir(parents=True, exist_ok=False)
                calls.append((entry['id'], text))
                passed = 'Check inventory' in text
                path = output / 'result.json'
                write_json(path, dict(dataset='alfworld', task_id=entry['id'], instruction='Deliver apple.',
                                     success=passed, score=float(passed), trajectory=[], steps=0))
                return path
        tasks = [dict(id='train', split='train'), dict(id='heldout', split='val'), dict(id='test', split='test')]
        adapter = create_adapter(tasks, Bridge())
        self.assertEqual(adapter.build_eval_env(1, 'valid_unseen', 42)[0]['id'], 'test')
        patches = [{'source_type': 'failure', 'batch_size': 1,
                    'patch': {'reasoning': 'Missing precondition', 'edits': [{'op': 'append', 'content': 'Check inventory'}]}}]
        with patch.object(adapter, 'reflect', return_value=patches):
            summary = ReflACTTrainer(cfg, adapter).train()
        self.assertEqual(summary['best_selection_hard'], 1)
        self.assertTrue(any(tid == 'heldout' and 'Check inventory' in text for tid, text in calls))
        self.assertTrue(any(tid == 'train' and 'Check inventory' not in text for tid, text in calls))
        self.assertFalse(any(tid == 'test' for tid, _ in calls))
        self.assertIn('Check inventory', (self.root / 'train/best_skill.md').read_text())

    def test_trainer_executes_each_manifest_task_once_per_epoch(self):
        import contextlib
        import io
        from skillopt.config import load_config, flatten_config
        from skillopt.engine.trainer import ReflACTTrainer
        root = Path(__file__).resolve().parents[1]
        cfg = flatten_config(load_config(str(root / 'method/skillopt/configs/_base_/default.yaml')))
        skill = self.root / 'initial.md'
        skill.write_text('')
        cfg.update(out_root=str(self.root / 'train'), skill_init=str(skill), train_size=96,
                   num_epochs=4, batch_size=40, accumulation=1, model_backend='openai_compatible',
                   optimizer_backend='openai_compatible', target_backend='openai_compatible',
                   use_slow_update=False, use_meta_skill=False, eval_test=False, sel_env_num=1)
        calls = []

        class Bridge:
            def run(self, entry, text, output):
                output = Path(output)
                output.mkdir(parents=True, exist_ok=False)
                calls.append((entry['id'], entry['split'], output))
                path = output / 'result.json'
                write_json(path, dict(dataset='alfworld', task_id=entry['id'], instruction='Fixture task.',
                                     success=False, score=0, trajectory=[], steps=0))
                return path

        tasks = [dict(id=f'train-{i}', split='train') for i in range(96)]
        tasks.append(dict(id='heldout', split='val'))
        adapter = create_adapter(tasks, Bridge())
        # Exercise real trainer -> loader -> adapter -> executor -> result
        # conversion without calling paid model APIs for reflection.
        with patch.object(adapter, 'reflect', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            ReflACTTrainer(cfg, adapter).train()
        train_calls = [row for row in calls if row[1] == 'train']
        self.assertEqual(len(train_calls), 4 * 96)
        for epoch in range(4):
            rows = train_calls[epoch * 96:(epoch + 1) * 96]
            self.assertEqual({row[0] for row in rows}, {f'train-{i}' for i in range(96)})
            batches = {}
            for _, _, output in rows:
                step = next(part for part in output.parts if part.startswith('step_'))
                batches[step] = batches.get(step, 0) + 1
            self.assertEqual(list(batches.values()), [40, 40, 16])


class Trace2SkillResultTests(unittest.TestCase):
    setUp = ResultAdapterTests.setUp
    record = ResultAdapterTests.record

    def test_logs_retain_long_trajectories_and_outcome_filename(self):
        from method_input_adapters.trace2skill import export_results as trace_export
        text = 'long observation ' * 50000
        source = self.record('appworld', success=True, score=1.,
                             trajectory=[dict(step=0, observation='initial', action='run code', next_observation=text)])
        row = trace_export([source], self.root / 'trace')[0]
        self.assertTrue(Path(row['log_path']).name.endswith('_SUCCEED.md'))
        self.assertIn(text, Path(row['log_path']).read_text())
        self.assertEqual(row['task_id'], 'task/one')

    def test_failed_science_score_preserved(self):
        from method_input_adapters.trace2skill import export_results as trace_export
        row = trace_export([self.record('scienceworld', score=0.5)], self.root / 'trace')[0]
        self.assertEqual(row['score'], .5)
        self.assertTrue(Path(row['log_path']).name.endswith('_FAILED.md'))

    def test_skillsbench_uses_original_events_and_rejects_modified_archive(self):
        from test_diagnosis_adapter import DiscoveryContextTests
        fixture = DiscoveryContextTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        archive = load_archive(fixture.root)
        self.assertEqual(archive.benchmark, 'skillsbench')
        self.assertEqual(archive.conversation[0]['event'], fixture.raw_events[0])
        self.assertEqual(archive.score, 0)
        fixture.events.write_text(fixture.events.read_text().replace('Actual user task', 'altered task'))
        with self.assertRaises(ValueError):
            load_archive(fixture.root)


class SharedExecutionBridgeTests(unittest.TestCase):
    setUp = ResultAdapterTests.setUp

    def test_skillsbench_replaces_only_target_and_keeps_resources(self):
        task = self.root / 'task'
        base = task / 'environment/skills'
        write_json(base / 'pdf/data.json', {'preserve': True})
        (base / 'pdf/SKILL.md').write_text('original')
        (base / 'other').mkdir(); (base / 'other/SKILL.md').write_text('other skill')
        config = self.root / 'execution.toml'
        config.write_text(f'[task]\nbenchmark="skillsbench"\npath="{task}"\n[agent]\nname="openhands"\nmodel="fixed-target"\n')
        calls = []
        class Executor:
            def __init__(self, *args): pass
            def run(self, request, output, environment):
                calls.append(request)
                output.mkdir()
                return SimpleNamespace(status='completed')
        bridge = SharedExecutorBridge({})
        with patch('excutor.excutors.Executor', Executor):
            bridge.run(dict(config=str(config), skill_target='pdf/SKILL.md'), 'candidate', self.root / 'run')
        request = calls[0]
        self.assertEqual(request.model, 'fixed-target')
        self.assertEqual((request.skills_dir / 'pdf/SKILL.md').read_text(), 'candidate')
        self.assertEqual(read_json(request.skills_dir / 'pdf/data.json'), {'preserve': True})
        self.assertEqual((request.skills_dir / 'other/SKILL.md').read_text(), 'other skill')
        self.assertEqual((base / 'pdf/SKILL.md').read_text(), 'original')
        with self.assertRaises(ValueError), patch('excutor.excutors.Executor', Executor):
            bridge.run(dict(config=str(config)), 'candidate', self.root / 'ambiguous')

    def test_native_candidates_and_text_reference_bundles_reach_all_three_runners(self):
        tasks = self.root / 'tasks.json'; write_json(tasks, ['task-one'])
        bundle = self.root / 'bundle'; (bundle / 'references').mkdir(parents=True)
        (bundle / 'SKILL.md').write_text('Use the reference.')
        (bundle / 'references/guide.md').write_text('Check the environment after acting.')
        for dataset in ('alfworld', 'scienceworld', 'appworld'):
            config = self.root / (dataset + '.toml')
            config.write_text(f'[task]\nbenchmark="{dataset}"\npath="tasks.json"\nskill_mode="no-skill"\n[agent]\nmodel="fixed-target"\n')
            def native(cfg, output, env):
                self.assertEqual(cfg.model, 'fixed-target')
                self.assertEqual(read_json(cfg.tasks_path), ['task-one'])
                self.assertIn('Check the environment', cfg.skill_path.read_text())
                return {'status': 'completed', 'task_results': [str(output / 'tasks/one.json')]}
            with patch('excutor.runner_common.launcher.run_native', native):
                SharedExecutorBridge({}).run(dict(config=str(config), task_ref='task-one'), '',
                                             self.root / dataset, skill_dir=bundle)
