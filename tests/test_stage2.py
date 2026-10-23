"""Stage2 generation consumes unified Stage1 archives without benchmark execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import write_json, tree_hashes
from harness.agent_client import AgentResult
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from method.skillpatch.patch_generation.config import Stage2Config, load_stage2_config
from method.skillpatch.patch_generation.source import load_source
from method.skillpatch.patch_generation.pipeline import run_stage2


class Client:
    def __init__(self, payloads, mutate=False):
        self.payloads = list(payloads)
        self.calls = []
        self.mutate = mutate
    def run(self, config, prompt, materials, output, environment, **kwargs):
        self.calls.append((config, prompt, materials))
        workspace = output / 'workspace'; workspace.mkdir(parents=True)
        write_json(workspace / 'report.json', self.payloads.pop(0))
        if self.mutate:
            materials[0].write_text('{}')
        return AgentResult('completed', output, workspace / 'report.json')


class Stage2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'original.json'
        self.row = {'dataset':'appworld','task_id':'task_1','instruction':'Check dates.',
            'retrieved_memory_text':'Normalize dates before comparing.', 'success':False,
            'trajectory':[{'action':'Compared local dates.','next_observation':'Wrong day.'}],'steps':1}
        write_json(self.original, self.row)
        project = Path(__file__).resolve().parents[1]
        self.generation = project / 'configs/skillpatch-generation.toml'
        self.config = Stage2Config(self.generation)
        self.report = {'summary':'Date handling needs clarification.','improvements':[{
            'target':{'skill_ref':'S0001','quote':self.row['retrieved_memory_text']},
            'diagnosis':'Local dates were compared.', 'suggestion':'Normalize dates.',
            'evidence':[{'evidence_id':'T000001','quote':'Compared local dates.'}]}]}
        self.diagnosis = self.root / 'diagnosis'
        state = run_discovery(self.original, load_discovery_config(self.generation), self.diagnosis,
                              {}, client=Client([self.report]))
        self.assertEqual(state['status'], 'completed', state)
        self.patch_payload = {'status':'candidate','reason':'Normalize the compared dates.',
            'when_to_use':'When comparing dates across time zones.',
            'how_to_interpret':'Convert both dates to the requested time zone before comparing.'}
    def test_generation_uses_only_stage1_archive_and_preserves_it(self):
        before = tree_hashes(self.diagnosis)
        self.original.unlink()
        client=Client([self.patch_payload])
        state=run_stage2(self.diagnosis,'I000001',self.config,self.root/'out',{},client=client)
        self.assertEqual(state['status'],'generated',state)
        self.assertEqual(state['semantic_status'],'candidate_patch')
        self.assertEqual(tree_hashes(self.diagnosis),before)
        generated=json.loads(Path(state['patch_path']).read_text())
        self.assertEqual(generated['target']['relative_path'],'delivered_S0001/SKILL.md')
        self.assertEqual(generated['when_to_use'],self.patch_payload['when_to_use'])
        self.assertEqual(len(client.calls),1)
        self.assertEqual(len(client.calls[0][2]),2)
        self.assertFalse((self.root/'out/cases').exists())

    def test_no_patch_has_no_patch_artifact(self):
        for status in ('unpatchable','insufficient_evidence'):
            state=run_stage2(self.diagnosis,'I000001',self.config,self.root/status,{},
                client=Client([{'status':status,'reason':'More information needed.',
                               'when_to_use':None,'how_to_interpret':None}]))
            self.assertEqual(state['status'],status,state)
            self.assertNotIn('patch_path',state)
            self.assertFalse((self.root/status/'generation/patch.json').exists())

    def test_bad_target_reference_fails_before_agent(self):
        bad=copy.deepcopy(self.report);bad['improvements'][0]['target']['quote']='not present'
        write_json(self.diagnosis/'report.json',bad)
        client=Client([])
        state=run_stage2(self.diagnosis,'I000001',self.config,self.root/'out',{},client=client)
        self.assertEqual(state['status'],'failed',state)
        self.assertEqual(client.calls,[])

    def test_context_tampering_fails_before_agent(self):
        path=self.diagnosis/'input/context.json'
        raw=json.loads(path.read_text());raw['items'][0]['text']='Changed task'
        write_json(path,raw)
        client=Client([])
        state=run_stage2(self.diagnosis,'I000001',self.config,self.root/'out',{},client=client)
        self.assertEqual(state['status'],'failed',state)
        self.assertEqual(client.calls,[])

    def test_older_context_without_optional_success_field(self):
        import hashlib
        path=self.diagnosis/'input/context.json'
        raw=json.loads(path.read_text())
        raw.pop('task_succeeded')
        encoded=json.dumps({key:value for key,value in raw.items() if key!='input_digest'},
                           ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        raw['input_digest']='sha256:'+hashlib.sha256(encoded).hexdigest()
        write_json(path,raw)
        result_path=self.diagnosis/'result.json'
        state=json.loads(result_path.read_text());state['input_digest']=raw['input_digest']
        write_json(result_path,state)
        source=load_source(self.diagnosis,'I000001')
        self.assertEqual(source.context.input_digest,raw['input_digest'])
        self.assertIsNone(source.context.task_succeeded)

    def test_agent_material_mutation_cannot_produce_patch(self):
        state=run_stage2(self.diagnosis,'I000001',self.config,self.root/'out',{},
                         client=Client([self.patch_payload],mutate=True))
        self.assertEqual(state['status'],'failed',state)
        self.assertFalse((self.root/'out/generation/patch.json').exists())

    def test_malformed_patch_is_not_saved(self):
        bad={**self.patch_payload,'when_to_use':None}
        state=run_stage2(self.diagnosis,'I000001',self.config,self.root/'out',{},client=Client([bad]))
        self.assertEqual(state['status'],'failed',state)
        self.assertFalse((self.root/'out/generation/patch.json').exists())

    def test_unknown_issue_and_output_inside_archive_are_rejected(self):
        with self.assertRaises(ValueError):load_source(self.diagnosis,'missing')
        with self.assertRaises(ValueError):run_stage2(self.diagnosis,'I000001',self.config,self.diagnosis/'out',{})

    def test_all_benchmarks_share_the_same_generation_path(self):
        from context import refresh_input_digest
        from method_input_adapters.skillpatch.models import DiagnosisContext, ContextItem
        for benchmark in ('skillsbench','alfworld','scienceworld','appworld'):
            with self.subTest(benchmark=benchmark):
                archive=self.root/benchmark
                raw=json.loads((self.diagnosis/'input/context.json').read_text())
                raw['benchmark']=benchmark
                context=DiagnosisContext(**{**raw,'items':[ContextItem(**item) for item in raw['items']]})
                refresh_input_digest(context)
                write_json(archive/'input/context.json',context)
                write_json(archive/'report.json',self.report)
                write_json(archive/'result.json',{'status':'completed','input_digest':context.input_digest,
                                                'source':'/unavailable/original'})
                state=run_stage2(archive,'I000001',self.config,self.root/(benchmark+'-out'),{},
                                 client=Client([self.patch_payload]))
                self.assertEqual(state['status'],'generated',state)

    def test_config_does_not_require_execution_settings(self):
        path=self.root/'stage2.toml'
        path.write_text('[stage2]\ngeneration_config = "'+str(self.generation)+'"\n')
        self.assertEqual(load_stage2_config(path),self.config)
        path.write_text(path.read_text()+'execution_config="execution.toml"\n')
        with self.assertRaises(ValueError):load_stage2_config(path)

    def test_cli_dispatch_generates_by_default(self):
        from set_up.cli import main
        with patch('method.skillpatch.patch_generation.config.load_stage2_config',return_value=self.config), \
             patch('method.skillpatch.patch_generation.pipeline.run_stage2',return_value={'status':'generated'}) as run, \
             patch('builtins.print'):
            code=main(['stage2','--diagnosis-dir',str(self.diagnosis),'--issue-id','I000001',
                '--config','unused','--output',str(self.root/'out')])
        self.assertEqual(code,0)
        self.assertNotIn('generate_only',run.call_args.kwargs)

if __name__=='__main__':unittest.main()
