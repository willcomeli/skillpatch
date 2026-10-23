"""SkillsBench validation with the real executor/adapter and synthetic backend."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from excutor.excutors import Executor
from excutor.executor_adapter.skillsbench import SkillsBenchExecutionAdapter
from excutor.executor_adapter.models import RunRequest, BackendRun, EvaluationFeedback
from excutor.executor_adapter.storage import write_json, tree_hashes
from harness.agent_client import AgentResult
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from method.skillpatch.patch_generation.config import Stage2Config
from method.skillpatch.patch_generation.pipeline import run_stage2
from method.skillpatch.patch_validation.skillsbench_patch_validation.config import ValidationConfig, load_validation_config
from method.skillpatch.patch_validation.skillsbench_patch_validation.pipeline import run_validation
from method.skillpatch.patch_validation.skillsbench_patch_validation.comparison import compare_feedback


class Client:
    def __init__(self,payload,status='completed'):
        self.payload=payload;self.status=status;self.calls=0
    def run(self,config,prompt,materials,output,environment,**kwargs):
        self.calls+=1
        workspace=output/'workspace';workspace.mkdir(parents=True)
        write_json(workspace/'report.json',self.payload)
        return AgentResult(self.status,output,workspace/'report.json')


class Backend:
    def __init__(self,reward=None,mutate=False,version='fixture-1',change_task=False):
        self.calls=0;self.reward=reward;self.mutate=mutate;self.version=version;self.change_task=change_task
    def run(self,request,task,run_dir,environment):
        self.calls+=1
        patched=bool(list(task.skills_dir.rglob('SKILL_PATCHES.md')))
        reward=self.reward if self.reward is not None else int(patched)
        trajectory=run_dir/'jobs/trial/trajectory/acp_trajectory.jsonl'
        trajectory.parent.mkdir(parents=True)
        # No prompt augmentation in this synthetic backend, so task messages stay identical.
        events=[{'type':'user_message','text':'Inspect dates.' if not self.change_task else 'Different task.'},
                {'type':'agent_message','text':'Normalized dates.' if patched else 'Compared local dates.'}]
        trajectory.write_text('\n'.join(json.dumps(event) for event in events)+'\n')
        normalized=run_dir/'backend/events.jsonl';normalized.parent.mkdir(parents=True)
        normalized.write_text('\n'.join(json.dumps({'event_id':f'T{i:06d}',
            'source':{'path':str(trajectory),'line':i},'event':event}) for i,event in enumerate(events,1))+'\n')
        verifier=run_dir/'jobs/trial/verifier';verifier.mkdir()
        (verifier/'reward.txt').write_text(str(reward))
        result=run_dir/'jobs/trial/result.json';write_json(result,{})
        if self.mutate:(task.task_dir/'task.md').write_text('Altered task')
        return BackendRun('completed',backend_version=self.version,result_path=result,
            trajectory_path=trajectory,normalized_trajectory_path=normalized,verifier_dir=verifier,
            summary={'rewards':{'reward':reward}},trajectory_complete=True,trajectory_events=len(events))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=Path(__file__).resolve().parents[1]
        task=self.root/'task'
        for name,text in {'task.md':'Inspect dates.','environment/Dockerfile':'FROM fixture',
            'environment/skills/dates/SKILL.md':'Normalize dates before comparing.',
            'verifier/test.sh':'fixture only'}.items():
            path=task/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        request=RunRequest('skillsbench',task,'openhands','fixture',('fixture-bench',))
        self.source=self.root/'source-run'
        result=Executor(SkillsBenchExecutionAdapter(),Backend()).run(request,self.source,{})
        self.assertEqual(result.status,'completed',result)
        report={'summary':'Dates need normalization.','improvements':[{
            'target':{'skill_ref':'S000001','quote':'Normalize dates before comparing.'},
            'diagnosis':'Local dates compared.','suggestion':'Normalize time zones.',
            'evidence':[{'evidence_id':'T000002','quote':'Compared local dates.'}]}]}
        # Get the generated Skill identifier rather than assume adapter numbering.
        from method_input_adapters.skillpatch import build_diagnosis_context
        context=build_diagnosis_context(self.source)
        report['improvements'][0]['target']['skill_ref']=next(i.context_id for i in context.items if i.kind=='skill')
        self.diagnosis=self.root/'diagnosis'
        gen=self.project/'configs/skillpatch-generation.toml'
        state=run_discovery(self.source,load_discovery_config(gen),self.diagnosis,{},client=Client(report))
        self.assertEqual(state['status'],'completed',state)
        state=run_stage2(self.diagnosis,'I000001',Stage2Config(gen),self.root/'generation',{},client=Client({
            'status':'candidate','reason':'Clarify date comparison.','when_to_use':'When comparing dates.',
            'how_to_interpret':'Normalize to the requested time zone.'}))
        self.assertEqual(state['status'],'generated',state)
        self.patch=Path(state['patch_path'])
        self.config=ValidationConfig(self.project/'configs/skillpatch-evaluation.toml')
        self.assessment={'baseline_status':'present','target_status':'repaired','explanation':'Dates normalized.',
            'baseline_evidence':[{'collection':'baseline','evidence_id':'T000002'}],
            'candidate_evidence':[{'collection':'candidate','evidence_id':'T000002'}],
            'reverse_conflicts':[],'revision_action':'none'}

    def test_source_baseline_reused_and_exact_patch_accepted(self):
        before=tree_hashes(self.source);backend=Backend();client=Client(self.assessment)
        state=run_validation(self.patch,self.config,self.root/'out',{},client=client,backend_factory=lambda:backend)
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual(state['decision'],'accepted',state)
        self.assertEqual(backend.calls,1);self.assertEqual(client.calls,1)
        self.assertEqual(tree_hashes(self.source),before)
        self.assertTrue((self.root/'out/stage3_queue.json').exists())
        self.assertFalse(state['active_version_changed'])
        self.assertEqual(state['cases'][0]['baseline_origin'],'source_execution')

    def test_prepare_runs_no_backend_or_agent(self):
        backend=Backend();client=Client(self.assessment)
        state=run_validation(self.patch,self.config,self.root/'out',{},prepare_only=True,
                             client=client,backend_factory=lambda:backend)
        self.assertEqual(state['status'],'prepared',state)
        self.assertEqual(backend.calls,0);self.assertEqual(client.calls,0)
        self.assertTrue((self.root/'out/packs/source/dates/SKILL_PATCHES.md').exists())
        self.assertFalse(state['ready_for_stage3'])

    def test_unchanged_score_and_model_repair_do_not_accept(self):
        state=run_validation(self.patch,self.config,self.root/'out',{},client=Client(self.assessment),
                             backend_factory=lambda:Backend(reward=0))
        self.assertEqual(state['decision'],'pending',state)
        self.assertFalse((self.root/'out/stage3_queue.json').exists())

    def test_objective_gain_survives_evaluator_failure_as_in_old_code(self):
        state=run_validation(self.patch,self.config,self.root/'out',{},client=Client(self.assessment,status='failed'),
                             backend_factory=Backend)
        self.assertEqual(state['decision'],'accepted',state)
        self.assertIn('evaluation_warning',state['cases'][0])

    def test_protected_mutation_or_backend_or_task_change_blocks_acceptance(self):
        for name,backend in [('mutate',Backend(mutate=True)),('version',Backend(version='different')),
                             ('task-change',Backend(change_task=True))]:
            state=run_validation(self.patch,self.config,self.root/name,{},client=Client(self.assessment),
                                 backend_factory=lambda:backend)
            self.assertEqual(state['decision'],'pending',state)
            self.assertFalse(state['ready_for_stage3'])

    def test_repetitions_share_one_historical_baseline(self):
        backend=Backend()
        state=run_validation(self.patch,replace(self.config,repetitions=2),self.root/'out',{},
                             client=Client(self.assessment),backend_factory=lambda:backend)
        self.assertEqual(state['decision'],'accepted',state)
        self.assertEqual(backend.calls,2)
        self.assertEqual({case['baseline_dir'] for case in state['cases']},{str(self.source)})

    def test_provider_settings_differ_before_any_execution(self):
        backend=Backend()
        state=run_validation(self.patch,self.config,self.root/'out',{'OPENAI_BASE_URL':'https://different.invalid'},
                             backend_factory=lambda:backend)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(backend.calls,0)

    def test_output_and_source_tampering_are_rejected(self):
        with self.assertRaises(ValueError):run_validation(self.patch,self.config,self.source/'out',{})
        (self.source/'input/tasks/task/task.md').write_text('Tampered')
        with self.assertRaises(ValueError):run_validation(self.patch,self.config,self.root/'out',{})

    def test_feedback_regression_and_incomplete_feedback(self):
        self.assertEqual(compare_feedback(EvaluationFeedback('scored',{'reward':1},True),
            EvaluationFeedback('scored',{'reward':0},False)).status,'regressed')
        self.assertEqual(compare_feedback(EvaluationFeedback('scored'),EvaluationFeedback('scored')).status,'unavailable')

    def test_regression_pair_is_fresh_and_scored(self):
        path=self.root/'regression.toml'
        task=self.root/'task'
        path.write_text('[task]\nbenchmark="skillsbench"\npath="'+str(task)+'"\n'
                        '[agent]\nname="openhands"\nmodel="fixture"\n'
                        '[runtime]\nbench_command=["fixture-bench"]\n')
        backend=Backend(reward=1)
        state=run_validation(self.patch,replace(self.config,regression_configs=(path,)),
            self.root/'out',{},client=Client(self.assessment),backend_factory=lambda:backend)
        self.assertEqual(state['decision'],'accepted',state)
        self.assertEqual(backend.calls,3)
        self.assertEqual(state['regression_coverage']['status'],'complete')
        self.assertEqual(state['cases'][1]['baseline_origin'],'fresh_execution')

    def test_wrong_regression_target_fails_during_preflight(self):
        (self.root/'task/environment/skills/dates/SKILL.md').write_text('Different Skill')
        path=self.root/'regression.toml'
        path.write_text('[task]\nbenchmark="skillsbench"\npath="'+str(self.root/'task')+'"\n'
                        '[agent]\nname="openhands"\nmodel="fixture"\n')
        backend=Backend()
        state=run_validation(self.patch,replace(self.config,regression_configs=(path,)),
            self.root/'out',{},client=Client(self.assessment),backend_factory=lambda:backend)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(backend.calls,0)

    def test_cli_and_config(self):
        from set_up.cli import main
        self.assertEqual(load_validation_config(self.project/'configs/skillpatch-validation.toml'),self.config)
        with patch('method.skillpatch.patch_validation.skillsbench_patch_validation.pipeline.run_validation',return_value={'status':'prepared'}) as run,\
             patch('builtins.print'):
            code=main(['validate-patch','--patch',str(self.patch),'--config',str(self.project/'configs/skillpatch-validation.toml'),
                       '--output',str(self.root/'out'),'--prepare-only'])
        self.assertEqual(code,0);self.assertTrue(run.call_args.kwargs['prepare_only'])

if __name__=='__main__':unittest.main()
