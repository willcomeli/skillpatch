"""Stage 2 behavioral tests with a foreign benchmark and no model/network/Docker."""

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from skillpatch.adapters.skillsbench import SkillsBenchAdapter
from skillpatch.context import refresh_input_digest
from skillpatch.discovery_validation import validate_report
from skillpatch.executor import Executor
from skillpatch.models import BackendRun, DiscoveryInput, EvidenceItem, EvaluationFeedback, PreparedTask, RunRequest
from skillpatch.patch_models import FeedbackComparison, SkillPatch, Stage2Config, TargetEvaluation
from skillpatch.patch_budget import PatchBudget
from skillpatch.patch_sources import load_patch_source, read_object
from skillpatch.patches import render_patch, tree_hashes, validate_generation
from skillpatch.stage2_config import load_stage2_config
from skillpatch.stages.patch_evaluation import decide_validation, validate_evaluation
from skillpatch.stages.patch_validation import run_stage2, _protected_inputs_equal
from skillpatch.storage import hash_manifest, jsonable, write_json
from test_discovery import compact_payload_fixture
from test_discovery_validation import make_context, make_payload


SKILL = ('---\nname: table\ndescription: "Table cleanup"\n---\n# Table cleanup\n'
         'Remove duplicate records when uniqueness is required.\n')


def fixture_skill_text(skill_dir):
    text = (skill_dir / 'SKILL.md').read_text()
    companion = skill_dir / 'SKILL_PATCHES.md'
    return text + ('\n' + companion.read_text() if companion.is_file() else '')


class TableAdapter:
    """Lower loss is better; native inputs and records differ from SkillsBench."""
    def prepare(self, request, destination):
        before = tree_hashes(request.task_dir)
        task = destination / 'tables' / request.task_dir.name
        shutil.copytree(request.task_dir, task)
        if request.skills_dir is not None:
            shutil.rmtree(task / 'skills')
            shutil.copytree(request.skills_dir, task / 'skills')
        hashes = tree_hashes(task)
        skills = task / 'skills'
        manifest = destination / 'table-manifest.json'
        write_json(manifest, {'source_hashes': before, 'prepared_hashes': hashes})
        return PreparedTask('tables', request.task_dir.name, task, task / 'instructions.txt',
                            skills, manifest, hash_manifest(hashes), hash_manifest(tree_hashes(skills)), hashes)

    def evaluate(self, execution):
        loss = execution.summary['loss']
        return EvaluationFeedback('scored', {'loss': loss, 'reward': 1 if loss == 0 else 0}, loss == 0,
                                  [{'name': 'rows', 'status': 'passed' if loss == 0 else 'failed'}])

    def compare_feedback(self, baseline, candidate, *, target=None):
        a, b = baseline.metrics['loss'], candidate.metrics['loss']
        return FeedbackComparison('improved' if b < a else 'regressed' if b > a else 'unchanged',
                                  [{'metric': 'loss', 'baseline': a, 'candidate': b}])

    def collect_evidence(self, run_dir, *, record_path=None):
        record = read_object(record_path or run_dir / 'result.json')
        prepared = record['prepared']
        task, pack = Path(prepared['task_dir']), Path(prepared['skills_dir'])
        skill = pack / 'table/SKILL.md'
        t = Path(record['execution']['normalized_trajectory_path'])
        context = DiscoveryInput(run_dir.name, 'tables', prepared['task_id'], prepared['task_digest'],
             prepared['skill_digest'], True, [
             EvidenceItem('Q000001', 'task', (task/'instructions.txt').read_text(), str(task/'instructions.txt')),
             EvidenceItem('S000001', 'skill', skill.read_text(), str(skill), skill_id='table',
                          relative_path='table/SKILL.md', version=prepared['file_hashes']['skills/table/SKILL.md']),
             EvidenceItem('T000001', 'trajectory', 'Read the table.', str(t), source_line=1),
             EvidenceItem('T000002', 'trajectory', record['execution']['summary']['behavior'], str(t), source_line=2),
             EvidenceItem('T000003', 'trajectory', 'Saved output.', str(t), source_line=3),
             EvidenceItem('F000001', 'feedback', 'Row count test failed.' if record['feedback']['metrics']['loss'] != 0
                          else 'Row count test passed.', str(run_dir/'result.json'))], [], '')
        refresh_input_digest(context)
        return context


class TableBackend:
    supports_live_diagnosis = False
    def __init__(self):
        self.calls = []
    def run(self, request, task, run_dir, environment):
        self.calls.append((request, task, run_dir))
        text = fixture_skill_text(task.skills_dir/'table')
        good = ('preserve duplicates' in text or 'regression' in task.task_document.read_text())
        events = run_dir/'backend/events.jsonl'
        events.parent.mkdir(parents=True, exist_ok=True)
        behavior = 'Preserved duplicate rows.' if good else 'Removed duplicate rows.'
        events.write_text('Read the table.\n'+behavior+'\nSaved output.\n')
        return BackendRun('completed', return_code=0, normalized_trajectory_path=events,
                          trajectory_complete=True, trajectory_events=3,
                          summary={'loss': 0 if good else 1, 'behavior': behavior})


class RecordedClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []
    def complete(self, config, messages, environment, output):
        self.calls.append((config, copy.deepcopy(messages), output))
        write_json(output/'model_call.json', {'status':'completed', 'usage':{'total_tokens':10}})
        item = next(self.outputs)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)


def candidate(text='preserve duplicates when the task requests preservation'):
    return {'status':'candidate', 'reason':'Clarify the original clause scope.',
            'when_to_use':text, 'how_to_interpret':'Only deduplicate when uniqueness is required.'}


def assessment(repaired=True, *, regression=False):
    return {'baseline_status':'absent' if regression else 'present',
            'target_status':'repaired' if repaired else 'not_repaired', 'explanation':'The judgment is based on actual row-processing events.',
            'baseline_evidence':[{'evidence_id':'T000002', 'quote':'Preserved duplicate rows.' if regression else 'Removed duplicate rows.'}],
            'candidate_evidence':[{'evidence_id':'T000002', 'quote':'Preserved duplicate rows.' if repaired else 'Removed duplicate rows.'}],
            'reverse_conflicts':[], 'revision_action':'none' if repaired else 'revise_patch'}


class Stage2Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        task = self.root/'table-task'
        (task/'skills/table').mkdir(parents=True)
        (task/'skills/table/SKILL.md').write_text(SKILL)
        (task/'skills/table/helper.py').write_text('original helper\n')
        (task/'instructions.txt').write_text('Preserve duplicate records for this task.')
        self.request = RunRequest('tables', task, 'fixture-agent', 'fixture-model', (), backend='table-engine')
        self.adapter = TableAdapter()
        self.backend = TableBackend()
        self.run_dir = self.root/'fresh-source'
        self.source_result = Executor(self.adapter, self.backend).run(self.request, self.run_dir, {})
        self.context = self.adapter.collect_evidence(self.run_dir)
        payload = compact_payload_fixture(make_payload())
        self.report = validate_report(payload, self.context)
        self.diagnosis = self.run_dir/'discovery/fixture'
        write_json(self.diagnosis/'input.json', self.context)
        write_json(self.diagnosis/'report.json', self.report)
        write_json(self.diagnosis/'result.json', {'status':'completed', 'run_id':self.run_dir.name})
        self.source = load_patch_source(self.diagnosis, 'C000001', adapter=self.adapter)
        project = Path(__file__).resolve().parents[1]
        for name, prompt in [('generation','induction.md'),('evaluation','patch_evaluation.md')]:
            (self.root/(name+'.toml')).write_text('[diagnosis]\nmodel="fixture"\nbase_url="https://not-called.invalid"\n'
                'api_key_env="FIXTURE_KEY"\nprompt_path='+json.dumps(str(project/'prompts'/prompt))+'\n')
        self.config = Stage2Config(self.root/'generation.toml', self.root/'evaluation.toml')
        self.backend.calls.clear()

    def execute(self, outputs, *, config=None, **kwargs):
        client = RecordedClient(outputs)
        result = run_stage2(self.diagnosis, 'C000001', config or self.config, self.root/'episode',
                           {'FIXTURE_KEY':'fixture-secret-value'}, client=client,
                           adapter_factory=lambda _: self.adapter, backend_factory=lambda _: self.backend, **kwargs)
        return result, client

    def legacy_report(self):
        report = make_payload()
        report.update(schema_version=1, run_id=self.context.run_id,
                      input_digest=self.context.input_digest, semantic_status='candidate_diagnosis')
        conflict = report['conflicts'][0]
        conflict['conflict_id'] = 'C1'
        skill = next(item for item in self.context.evidence
                     if item.evidence_id == conflict['target']['skill_ref'])
        conflict['target'].update(relative_path=skill.relative_path, version=skill.version,
                                  source_path=skill.source_path)
        return report

    def test_complete_foreign_benchmark_loop_reuses_source_and_runs_one_candidate(self):
        original = (self.run_dir/'result.json').read_bytes()
        result, client = self.execute([candidate(), assessment()])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['decision'], 'accepted')
        self.assertTrue(result['ready_for_stage3'])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertIn('candidate', self.backend.calls[0][2].name)
        case = result['attempts'][0]['cases'][0]
        self.assertEqual(case['baseline_origin'], 'source')
        self.assertEqual(case['baseline_run_id'], self.run_dir.name)
        self.assertEqual(case['baseline_input_digest'], self.source.context.input_digest)
        self.assertFalse((self.root/'episode/baselines').exists())
        self.assertEqual((self.run_dir/'result.json').read_bytes(), original)
        self.assertEqual(len(client.calls), 2)
        self.assertNotEqual(client.calls[0][1][0]['content'], client.calls[1][1][0]['content'])
        self.assertEqual(read_object(self.diagnosis/'report.json')['schema_version'], 2)
        self.assertIn(self.source.conflict.diagnosis, client.calls[0][1][1]['content'])
        for request, prepared, run_dir in self.backend.calls:
            self.assertIsNone(request.diagnosis_config)
            self.assertEqual(request.model, self.request.model)
            self.assertEqual(prepared.task_document.read_text(), 'Preserve duplicate records for this task.')
            self.assertNotIn('Clarify the original clause scope.', prepared.task_document.read_text())
        self.assertEqual((self.source.prepared.skills_dir/'table/SKILL.md').read_text(), SKILL)
        self.assertTrue((self.root/'episode/stage3_queue.json').is_file())
        self.assertTrue(result['coverage_notes'])
        self.assertEqual(result['regression_coverage']['status'],'not_configured')
        self.assertEqual(read_object(self.root/'episode/stage3_queue.json')['regression_coverage']['checked_pairs'],0)

    def test_prepare_only_never_calls_model_or_execution(self):
        result, client = self.execute([], prepare_only=True)
        self.assertEqual(result['status'], 'prepared', result)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(client.calls, [])
        self.assertEqual(read_object(self.root/'episode/plan.json')['max_backend_runs'], 3)

    def test_prepare_plan_distinguishes_candidate_sessions_and_model_calls(self):
        config = replace(self.config, model_session_timeout_sec=321)
        result, client = self.execute([], prepare_only=True, config=config)
        self.assertEqual(result['status'], 'prepared', result)
        plan = read_object(self.root/'episode/plan.json')
        self.assertEqual(plan['max_generation_sessions'], 3)
        self.assertEqual(plan['max_evaluation_sessions'], 3)
        self.assertEqual(plan['max_generation_calls'], 3 * (50 + 2))
        self.assertEqual(plan['max_evaluation_calls'], 3 * (50 + 2))
        self.assertEqual(plan['max_generation_tool_calls'], 300)
        self.assertEqual(plan['max_evaluation_tool_calls'], 300)
        self.assertEqual(plan['model_session_timeout_sec'], 321)
        self.assertEqual(plan['generation_session_timeout_sec'], 321)
        self.assertEqual(plan['evaluation_session_timeout_sec'], 321)
        self.assertFalse(plan['generation_session_budget']['time_budget_only'])
        self.assertEqual(plan['max_backend_runs'], 3)
        self.assertEqual(plan['max_fresh_baseline_runs'], 0)
        self.assertEqual(plan['max_candidate_runs'], 3)
        self.assertEqual(plan['source_baseline']['run_id'], self.run_dir.name)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_generation_deadline_is_separate_from_evaluation(self):
        from skillpatch.stages import patch_validation
        config = replace(self.config, generation_session_timeout_sec=1800,
                         model_session_timeout_sec=900)
        with patch.object(patch_validation, 'generate_patch', wraps=patch_validation.generate_patch) as generate, \
                patch.object(patch_validation, 'evaluate_target', wraps=patch_validation.evaluate_target) as evaluate:
            result, _ = self.execute([candidate(), assessment()], config=config)
        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(generate.call_args.kwargs['session_timeout_sec'], 1800)
        self.assertEqual(evaluate.call_args.kwargs['session_timeout_sec'], 900)
        plan = read_object(self.root/'episode/plan.json')
        self.assertEqual(plan['generation_session_timeout_sec'], 1800)
        self.assertEqual(plan['evaluation_session_timeout_sec'], 900)

    def test_time_only_generation_plan_has_no_call_limits_but_keeps_candidate_budget(self):
        self.config.generation_config.write_text(self.config.generation_config.read_text() +
                                               'time_budget_only=true\n')
        config = replace(self.config, generation_session_timeout_sec=1800)
        result, client = self.execute([], prepare_only=True, config=config)
        self.assertEqual(result['status'], 'prepared', result)
        plan = read_object(self.root/'episode/plan.json')
        self.assertIsNone(plan['max_generation_calls'])
        self.assertIsNone(plan['max_generation_tool_calls'])
        budget = plan['generation_session_budget']
        self.assertTrue(budget['time_budget_only'])
        self.assertEqual(budget['session_timeout_sec'], 1800)
        for name in ('max_rounds', 'max_tool_calls', 'max_report_repairs'):
            self.assertIsNone(budget[name])
        self.assertEqual(plan['max_generation_sessions'], 3)
        self.assertEqual(plan['max_candidates'], 3)
        self.assertEqual(plan['max_candidate_runs'], 3)
        self.assertEqual(plan['max_evaluation_calls'], 3 * (50 + 2))
        self.assertEqual(plan['evaluation_session_budget']['session_timeout_sec'], 900)
        self.assertFalse(plan['evaluation_session_budget']['time_budget_only'])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_session_deadline_config_and_invalid_values(self):
        path = self.root/'stage2.toml'
        base = '[stage2]\ngeneration_config="generation.toml"\nevaluation_config="evaluation.toml"\n'
        path.write_text(base)
        self.assertEqual(load_stage2_config(path).model_session_timeout_sec, 900)
        self.assertIsNone(load_stage2_config(path).generation_session_timeout_sec)
        path.write_text(base + 'model_session_timeout_sec=123\n')
        self.assertEqual(load_stage2_config(path).model_session_timeout_sec, 123)
        path.write_text(base + 'generation_session_timeout_sec=1800\nmodel_session_timeout_sec=900\n')
        config = load_stage2_config(path)
        self.assertEqual(config.generation_session_timeout_sec, 1800)
        self.assertEqual(config.model_session_timeout_sec, 900)
        for name in ('model_session_timeout_sec', 'generation_session_timeout_sec'):
            for value in ('0', '-1', 'true', '1.5', '"900"'):
                with self.subTest(name=name, value=value):
                    path.write_text(base + name + '=' + value + '\n')
                    with self.assertRaisesRegex(ValueError, name):
                        load_stage2_config(path)

    def test_generate_only_is_unvalidated_and_does_not_execute(self):
        result, client = self.execute([candidate()], generate_only=True)
        self.assertEqual(result['decision'], 'unvalidated', result)
        self.assertFalse(result['ready_for_stage3'])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(client.calls), 1)
        self.assertFalse((self.root/'episode/stage3_queue.json').exists())

    def test_revisions_are_bounded_and_share_the_source_baseline(self):
        outputs = [x for _ in range(3) for x in (candidate('ambiguous instruction'), assessment(False))]
        result, client = self.execute(outputs)
        self.assertEqual(result['decision'], 'rejected', result)
        self.assertEqual(len(result['attempts']), 3)
        self.assertEqual(len(self.backend.calls), 3)
        self.assertEqual(len(client.calls), 6)
        self.assertEqual(sum('baseline' in p.name for _,_,p in self.backend.calls), 0)
        self.assertEqual({a['cases'][0]['baseline_run_id'] for a in result['attempts']}, {self.run_dir.name})
        self.assertFalse((self.root/'episode/stage3_queue.json').exists())

    def test_insufficient_generation_does_not_render_or_execute(self):
        result, client = self.execute([{'status':'insufficient_evidence','reason':'The applicability prerequisite is unsupported.',
                                      'when_to_use':None,'how_to_interpret':None}])
        self.assertEqual(result['decision'], 'pending')
        self.assertEqual(self.backend.calls, [])

    def test_invalid_model_output_is_saved_and_not_retried(self):
        result, client = self.execute([{'status':'candidate'}])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(client.calls), 1)
        self.assertTrue((self.root/'episode/attempts/000/generation/model_output.json').exists())
        self.assertEqual(self.backend.calls, [])

    def test_unsupported_or_stale_regression_is_rejected_before_calls(self):
        task = self.root/'regression-task'
        shutil.copytree(self.request.task_dir, task)
        (task/'skills/table/SKILL.md').write_text(SKILL+'changed\n')
        cfg = self.root/'regression.toml'
        cfg.write_text('[task]\nbenchmark="tables"\npath="regression-task"\n'
                       '[agent]\nname="fixture"\nmodel="fixture"\n[runtime]\nbackend="table-engine"\n')
        result, client = self.execute([], config=replace(self.config, regression_configs=(cfg,)))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_related_success_case_is_checked_with_its_other_resources_preserved(self):
        task = self.root/'regression-task'
        shutil.copytree(self.request.task_dir, task)
        (task/'instructions.txt').write_text('Preserve duplicate records for this regression task.')
        (task/'skills/table/extra.txt').write_text('regression resource')
        cfg = self.root/'regression.toml'
        cfg.write_text('[task]\nbenchmark="tables"\npath="regression-task"\n'
                       '[agent]\nname="fixture"\nmodel="fixture"\n[runtime]\nbackend="table-engine"\n')
        result, client = self.execute([candidate(), assessment(), assessment(regression=True)],
                                      config=replace(self.config, regression_configs=(cfg,)))
        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(len(self.backend.calls), 3)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(result['regression_coverage'],{'configured_tasks':1,'checked_pairs':1,'status':'complete'})
        self.assertEqual(sum('baseline' in p.name for _,_,p in self.backend.calls), 1)
        self.assertEqual(read_object(self.root/'episode/plan.json')['max_backend_runs'], 7)
        for _, prepared, _ in self.backend.calls[1:]:
            self.assertEqual((prepared.skills_dir/'table/extra.txt').read_text(), 'regression resource')

    def test_harness_added_task_hint_cannot_be_mistaken_for_patch_effect(self):
        original = self.adapter.collect_evidence
        def changed(run_dir, *, record_path=None):
            context = original(run_dir,record_path=record_path)
            if 'candidate' in run_dir.name:
                context.evidence[0] = replace(context.evidence[0],text=context.evidence[0].text+' Additional answer hint.')
                refresh_input_digest(context)
            return context
        self.adapter.collect_evidence = changed
        result,client = self.execute([candidate()])
        self.assertEqual(result['decision'],'pending',result)
        self.assertFalse(result['ready_for_stage3'])
        self.assertEqual(len(client.calls),1)

    def test_successful_source_is_not_automatically_evolved(self):
        record = read_object(self.run_dir/'result.json')
        record['feedback']['passed'] = True
        record['feedback']['metrics']['reward'] = 1
        write_json(self.run_dir/'result.json', record)
        result, client = self.execute([])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(client.calls, [])

    def test_partial_reward_with_unknown_passed_enters_stage2_preflight(self):
        record = read_object(self.run_dir/'result.json')
        record['feedback']['passed'] = None
        record['feedback']['metrics']['reward'] = 0.85
        write_json(self.run_dir/'result.json', record)
        source = load_patch_source(self.diagnosis, 'C000001', adapter=self.adapter)
        self.assertEqual(source.conflict.conflict_id, 'C000001')
        result, client = self.execute([], prepare_only=True)
        self.assertEqual(result['status'], 'prepared', result)
        self.assertEqual(client.calls, [])

    def test_tampered_stage1_evidence_is_rejected(self):
        data = read_object(self.diagnosis/'input.json')
        data['evidence'][2]['text'] = 'invented behavior'
        write_json(self.diagnosis/'input.json', data)
        with self.assertRaisesRegex(ValueError, 'digest'):
            load_patch_source(self.diagnosis, 'C000001', adapter=self.adapter)

    def test_original_v1_report_preserves_conflict_id_and_archive_bytes(self):
        report = self.legacy_report()
        path = self.diagnosis/'report.json'
        write_json(path, report)
        before = path.read_bytes()

        source = load_patch_source(self.diagnosis, 'C1', adapter=self.adapter)

        self.assertEqual(source.conflict.conflict_id, 'C1')
        self.assertEqual(source.conflict.target.version, self.source.conflict.target.version)
        for field in ('applicability_reason', 'observed_behavior', 'expected_behavior', 'feedback_assessment'):
            self.assertIn(report['conflicts'][0][field], source.conflict.diagnosis)
        self.assertEqual(source.conflict.trajectory_range, report['conflicts'][0]['trajectory_range'])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'existing Stage 1 conflict'):
            load_patch_source(self.diagnosis, 'C000001', adapter=self.adapter)

    def test_v1_and_v2_reports_reject_tampered_target_bindings_and_citations(self):
        originals = [self.legacy_report(), read_object(self.diagnosis/'report.json')]
        for original in originals:
            identifier = original['conflicts'][0]['conflict_id']
            for field in ('skill_id', 'relative_path', 'version', 'source_path', 'behavior_quote'):
                with self.subTest(schema=original['schema_version'], field=field):
                    report = copy.deepcopy(original)
                    conflict = report['conflicts'][0]
                    if field == 'behavior_quote':
                        conflict['behavior_evidence'][0]['quote'] = 'Invented execution.'
                    else:
                        conflict['target'][field] = 'invented'
                    write_json(self.diagnosis/'report.json', report)
                    with self.assertRaises(ValueError):
                        load_patch_source(self.diagnosis, identifier, adapter=self.adapter)

    def test_old_text_only_input_digest_is_verified_before_optional_defaults(self):
        data = read_object(self.diagnosis/'input.json')
        for item in data['evidence']:
            for key in ('availability','source_role','observation'):
                del item[key]
        del data['input_digest']
        data['input_digest'] = 'sha256:'+hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                   separators=(',',':')).encode()).hexdigest()
        report = read_object(self.diagnosis/'report.json')
        report['input_digest'] = data['input_digest']
        write_json(self.diagnosis/'input.json', data)
        write_json(self.diagnosis/'report.json', report)
        source = load_patch_source(self.diagnosis,'C000001',adapter=self.adapter)
        self.assertEqual(source.context.input_digest, data['input_digest'])

    def test_generation_budget_cannot_be_reset_with_another_output_directory(self):
        config = replace(self.config, max_revisions=0)
        result, client = self.execute([candidate()], config=config, generate_only=True)
        self.assertEqual(result['decision'], 'unvalidated')
        second_client = RecordedClient([])
        second = run_stage2(self.diagnosis,'C000001',config,self.root/'another-episode',
                            {'FIXTURE_KEY':'fixture-secret-value'},client=second_client,
                            adapter_factory=lambda _:self.adapter,backend_factory=lambda _:self.backend)
        self.assertEqual(second['reason'],'source_conflict_budget_exhausted',second)
        self.assertEqual(second_client.calls,[])
        self.assertEqual(self.backend.calls,[])

    def test_existing_patch_revalidation_does_not_generate_or_consume_revision(self):
        self.config.generation_config.write_text(self.config.generation_config.read_text() +
                                               'time_budget_only=true\n')
        patch = validate_generation(candidate(), self.source, 0)
        path = self.root/'archived-patch.json'
        write_json(path, patch)
        budget = PatchBudget(self.source, 1)
        budget.acquire()
        budget.reserve(self.root/'old-episode')
        budget.release()
        before = budget.path.read_bytes()
        result, client = self.execute([assessment()], patch_file=path,
                                      config=replace(self.config, max_revisions=0))
        self.assertEqual(result['decision'], 'accepted', result)
        self.assertEqual(result['patch_origin'], 'existing')
        self.assertEqual(result['attempts'][0]['patch'], jsonable(patch))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(budget.path.read_bytes(), before)
        plan = read_object(self.root/'episode/plan.json')
        self.assertEqual(plan['max_generation_calls'], 0)
        self.assertEqual(plan['max_generation_tool_calls'], 0)
        self.assertTrue(plan['generation_session_budget']['time_budget_only'])
        self.assertEqual(plan['max_candidate_runs'], 1)

    def test_existing_patch_with_another_source_cannot_execute(self):
        patch = jsonable(validate_generation(candidate(), self.source, 0))
        patch['source_input_digest'] = 'sha256:stale'
        path = self.root/'archived-patch.json'
        write_json(path, patch)
        result, client = self.execute([], patch_file=path)
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('does not match', result['error'])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_existing_patch_rejection_never_generates_a_revision(self):
        patch = validate_generation(candidate('ambiguous'), self.source, 0)
        path = self.root/'archived-patch.json'
        write_json(path, patch)
        result, client = self.execute([assessment(False)], patch_file=path)
        self.assertEqual(result['decision'], 'rejected', result)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(result['attempts']), 1)

    def test_cancelled_candidate_stops_other_runs_and_releases_budget_lock(self):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt
        self.backend.run = interrupt
        result, client = self.execute([candidate()])
        self.assertEqual(result['status'],'cancelled',result)
        self.assertEqual(len(client.calls),1)
        self.assertEqual(list((self.run_dir/'stage2/budgets').glob('*.lock')),[])

    def test_concurrent_source_conflict_budget_is_rejected(self):
        first = PatchBudget(self.source,3)
        first.acquire()
        try:
            result, client = self.execute([])
            self.assertEqual(result['status'],'failed')
            self.assertIn('locked',result['error'])
            self.assertEqual(client.calls,[])
            self.assertTrue(first.lock.exists())
        finally:
            first.release()

    def test_evaluation_with_invented_quote_does_not_override_objective_gain(self):
        value = assessment()
        value['candidate_evidence'][0]['quote'] = 'invented completed behavior'
        result, client = self.execute([candidate(),value])
        self.assertEqual(result['status'],'completed')
        self.assertTrue(result['ready_for_stage3'])
        self.assertTrue((self.root/'episode/stage3_queue.json').exists())
        case = result['attempts'][0]['cases'][0]
        self.assertEqual(case['evaluation']['target_status'], 'uncertain')
        self.assertEqual(case['evaluation']['candidate_evidence'], [])
        self.assertIn('evaluation_warning', case)
        self.assertEqual(len(client.calls),2)

    def test_return_to_stage1_is_recorded_and_not_retried_as_stronger_patch(self):
        value = assessment(False)
        value['target_status'] = 'invalid_diagnosis'
        value['revision_action'] = 'revisit_diagnosis'
        result, client = self.execute([candidate('ambiguous'),value])
        self.assertEqual(result['next_action'],'return_to_stage1')
        self.assertEqual(len(result['attempts']),1)
        self.assertEqual(len(client.calls),2)

    def test_two_candidate_repetitions_share_the_first_execution_as_baseline(self):
        result,client = self.execute([candidate(),assessment(),assessment()],
                                     config=replace(self.config,repetitions=2))
        self.assertEqual(result['decision'],'accepted',result)
        self.assertEqual(len(self.backend.calls),2)
        self.assertEqual(len(client.calls),3)
        self.assertEqual(len({p.name for _,_,p in self.backend.calls}),2)
        self.assertEqual({case['baseline_run_id'] for case in result['attempts'][0]['cases']}, {self.run_dir.name})
        self.assertEqual(read_object(self.root/'episode/plan.json')['max_backend_runs'], 6)

    def test_original_input_mutation_is_rejected_before_generation(self):
        (self.source.prepared.task_dir/'instructions.txt').write_text('Changed source task.')
        result, client = self.execute([])
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_changed_provider_settings_are_rejected_before_generation(self):
        runtime = read_object(self.run_dir/'runtime_environment.json')
        runtime['provider_settings'] = {'DEEPSEEK_THINKING': 'disabled'}
        write_json(self.run_dir/'runtime_environment.json', runtime)
        result, client = self.execute([])
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('provider settings differ', result['error'])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_different_candidate_backend_version_cannot_claim_repair(self):
        run = self.backend.run
        def changed(*args, **kwargs):
            result = run(*args, **kwargs)
            result.backend_version = 'different-version'
            return result
        self.backend.run = changed
        result, client = self.execute([candidate()])
        self.assertEqual(result['decision'], 'pending', result)
        self.assertIn('backend versions', result['attempts'][0]['cases'][0]['error'])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(client.calls), 1)

    def test_secret_model_output_is_not_rendered_or_exposed_in_state(self):
        value = candidate('fixture-secret-value')
        result,client = self.execute([value])
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('fixture-secret-value',json.dumps(result))
        self.assertNotIn('fixture-secret-value',(self.root/'episode/attempts/000/generation/model_output.txt').read_text())
        self.assertEqual(self.backend.calls,[])

    def test_ambiguous_or_stale_target_cannot_be_applied(self):
        patch = validate_generation(candidate(), self.source, 0)
        source = self.source.prepared.skills_dir
        path = source/'table/SKILL.md'
        path.write_text(SKILL+'changed')
        with self.assertRaisesRegex(ValueError, 'version'):
            render_patch(source, patch, self.root/'rendered')

    def test_renderer_preserves_all_original_bytes_and_adds_independent_patch(self):
        patch = validate_generation(candidate(), self.source, 0)
        output = self.root/'rendered'
        result = render_patch(self.source.prepared.skills_dir, patch, output)
        rendered = (output/'table/SKILL.md').read_text()
        self.assertEqual(rendered, SKILL)
        supplement = (output/'table/SKILL_PATCHES.md').read_text()
        self.assertIn('### When to use', supplement)
        self.assertIn(patch.target.quote, supplement)
        self.assertEqual(result['presentation_mode'], 'sidecar_v1')
        self.assertEqual(set(result['candidate_hashes']) - set(result['base_hashes']),
                         {'table/SKILL_PATCHES.md', 'table/SKILL_PATCHES.json'})
        self.assertTrue(all(result['candidate_hashes'][k] == v for k, v in result['base_hashes'].items()))
        self.assertEqual((output/'table/helper.py').read_text(), 'original helper\n')
        self.assertNotEqual(result['base_digest'], result['candidate_digest'])

    def test_sidecar_protection_checks_resources_outside_the_patch_pair(self):
        patch = validate_generation(candidate(), self.source, 0)
        pack = self.root/'rendered'
        render_patch(self.source.prepared.skills_dir, patch, pack)
        request = replace(self.request, skills_dir=pack)
        prepared = self.adapter.prepare(request, self.root/'candidate-input')
        after = replace(self.source_result, prepared=prepared)
        self.assertTrue(_protected_inputs_equal(self.source_result, after, patch))
        helper = prepared.skills_dir/'table/helper.py'
        helper.write_text('tampered implementation\n')
        prepared.file_hashes = tree_hashes(prepared.task_dir)
        self.assertFalse(_protected_inputs_equal(self.source_result, after, patch))

    def test_sidecar_protection_rejects_extra_candidate_entry(self):
        from skillpatch.patch_sidecars import load_sidecars, write_sidecars
        patch = validate_generation(candidate(), self.source, 0)
        pack = self.root/'rendered'
        render_patch(self.source.prepared.skills_dir, patch, pack)
        unit = pack/'table'
        payload = load_sidecars(unit)
        extra = copy.deepcopy(payload['entries'][0])
        extra['entry_id'] = 'unvalidated-extra-patch'
        for name in ('SKILL_PATCHES.md', 'SKILL_PATCHES.json'):
            (unit/name).unlink()
        write_sidecars(unit, payload['entries'] + [extra])
        prepared = self.adapter.prepare(replace(self.request, skills_dir=pack), self.root/'candidate-input')
        self.assertFalse(_protected_inputs_equal(self.source_result,
                                                replace(self.source_result, prepared=prepared), patch))

    def test_whole_skill_sidecar_preserves_selection_description_and_body(self):
        patch = replace(validate_generation(candidate(), self.source, 0),
                        target=replace(self.source.conflict.target, scope='whole_skill'))
        result = render_patch(self.source.prepared.skills_dir, patch, self.root/'rendered')
        self.assertFalse(result['selection_description_updated'])
        text = (self.root/'rendered/table/SKILL.md').read_text()
        self.assertEqual(text, SKILL)
        self.assertIn('whole_skill', (self.root/'rendered/table/SKILL_PATCHES.md').read_text())

    def test_legacy_inline_renderer_still_reproduces_old_description_and_body(self):
        patch = replace(validate_generation(candidate(), self.source, 0),
                        target=replace(self.source.conflict.target, scope='whole_skill'))
        result = render_patch(self.source.prepared.skills_dir, patch, self.root/'rendered',
                              presentation_mode='inline_v1')
        self.assertTrue(result['selection_description_updated'])
        text = (self.root/'rendered/table/SKILL.md').read_text()
        self.assertIn('When to use:', text.split('---')[1])
        self.assertLess(text.index('### When to use'), text.index('# Table cleanup'))

    def test_linked_skill_resource_is_rejected_before_copying(self):
        pack = self.source.prepared.skills_dir
        (pack/'table/escape.txt').symlink_to(self.root/'table-task/instructions.txt')
        with self.assertRaisesRegex(ValueError,'links'):
            render_patch(pack, validate_generation(candidate(),self.source,0), self.root/'rendered')

    def test_original_newline_bytes_are_preserved(self):
        pack = self.source.prepared.skills_dir
        path = pack/'table/SKILL.md'
        path.write_bytes(SKILL.replace('\n','\r\n').encode())
        from skillpatch.storage import sha256_file
        patch = validate_generation(candidate(),self.source,0)
        patch = replace(patch,target=replace(patch.target,version=sha256_file(path)))
        render_patch(pack,patch,self.root/'rendered')
        raw = (self.root/'rendered/table/SKILL.md').read_bytes()
        self.assertNotIn(b'\n',raw.replace(b'\r\n',b''))


class Stage2PolicyTests(unittest.TestCase):
    def evaluation(self, **kwargs):
        return replace(TargetEvaluation('present','repaired','checked',[],[],[],'none'),**kwargs)

    def test_model_only_repair_with_binary_failure_is_pending(self):
        result = decide_validation([self.evaluation()], [FeedbackComparison('unchanged')], [])
        self.assertEqual(result['decision'], 'pending')

    def test_partial_objective_improvement_can_be_accepted(self):
        result = decide_validation([self.evaluation()], [FeedbackComparison('improved')], [])
        self.assertTrue(result['ready_for_stage3'])

    def test_objective_gain_is_retained_despite_model_or_regression(self):
        for evaluation in [self.evaluation(baseline_status='absent'),
                           self.evaluation(target_status='uncertain'),
                           self.evaluation(target_status='not_repaired'),
                           self.evaluation(reverse_conflicts=[{'type':'under_use'}])]:
            result = decide_validation([evaluation], [FeedbackComparison('improved')],
                                       [FeedbackComparison('regressed')])
            self.assertEqual(result['decision'], 'accepted')
            self.assertEqual(result['reason'], 'objective_improvement')

    def test_mixed_test_improvement_and_regression_retains_patch(self):
        comparison = FeedbackComparison('regressed', [
            {'direction':'improved','test':'fixed'}, {'direction':'regressed','test':'broken'}])
        self.assertTrue(decide_validation([], [comparison], [])['ready_for_stage3'])
        self.assertFalse(decide_validation([], [comparison], [], executions_ok=False)['ready_for_stage3'])

    def test_skillsbench_named_tests_prove_partial_repair_when_reward_stays_zero(self):
        a = EvaluationFeedback('scored', {'reward':0}, False,
            [{'name':'target','status':'failed'},{'name':'other','status':'failed'}])
        b = EvaluationFeedback('scored', {'reward':0}, False,
            [{'name':'target','status':'passed'},{'name':'other','status':'failed'}])
        self.assertEqual(SkillsBenchAdapter().compare_feedback(a,b).status, 'improved')

    def test_skillsbench_mixed_test_gain_and_loss_is_regression(self):
        a = EvaluationFeedback('scored', {'reward':0}, False,
            [{'name':'target','status':'failed'},{'name':'other','status':'passed'}])
        b = EvaluationFeedback('scored', {'reward':1}, True,
            [{'name':'target','status':'passed'},{'name':'other','status':'failed'}])
        self.assertEqual(SkillsBenchAdapter().compare_feedback(a,b).status, 'regressed')

    def test_missing_or_duplicate_tests_do_not_establish_improvement(self):
        a = EvaluationFeedback('scored', {'reward':0}, False, [{'name':'target','status':'failed'}])
        for tests in [[], [{'name':'target','status':'passed'}]*2]:
            b = EvaluationFeedback('scored', {'reward':1}, True, tests)
            self.assertEqual(SkillsBenchAdapter().compare_feedback(a,b).status, 'unavailable')

    def test_unrelated_subtest_improvement_cannot_prove_local_target_repair(self):
        from skillpatch.models import EvidenceRef
        source = validate_report(make_payload(),make_context()).conflicts[0]
        source.feedback_evidence = [EvidenceRef('F000001','test_target failed')]
        a = EvaluationFeedback('scored',{'reward':0},False,
            [{'name':'test_target','status':'failed'},{'name':'test_other','status':'failed'}])
        b = EvaluationFeedback('scored',{'reward':0},False,
            [{'name':'test_target','status':'failed'},{'name':'test_other','status':'passed'}])
        self.assertEqual(SkillsBenchAdapter().compare_feedback(a,b,target=source).status,'unchanged')
        b.tests = [{'name':'test_target','status':'passed'},{'name':'test_other','status':'failed'}]
        self.assertEqual(SkillsBenchAdapter().compare_feedback(a,b,target=source).status,'improved')

    def test_stage2_config_has_explicit_models_and_bounded_revisions(self):
        project = Path(__file__).resolve().parents[1]
        config = load_stage2_config(project/'configs/stage2.toml')
        self.assertEqual(config.max_revisions,2)
        self.assertEqual(config.repetitions,1)


if __name__ == '__main__':
    unittest.main()
