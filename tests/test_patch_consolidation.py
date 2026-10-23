"""Consolidation proposals from real generation archives with a fake harness client."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from harness.agent_client import AgentResult
from excutor.executor_adapter.storage import write_json, tree_hashes
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from method.skillpatch.patch_generation.config import Stage2Config
from method.skillpatch.patch_generation.pipeline import run_stage2
from method.skillpatch.patch_consolidation.config import ConsolidationConfig, load_consolidation_config
from method.skillpatch.patch_consolidation.pipeline import run_consolidation


class Client:
    def __init__(self, payload, mutate=False, status='completed'):
        self.payload=payload; self.calls=[]; self.mutate=mutate; self.status=status
    def run(self, config, prompt, materials, output, environment, **kwargs):
        self.calls.append([json.loads(path.read_text()) for path in materials])
        workspace=output/'workspace'; workspace.mkdir(parents=True)
        write_json(workspace/'report.json', self.payload)
        if self.mutate: materials[-1].write_text('{}')
        return AgentResult(self.status,output,workspace/'report.json')


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.project=Path(__file__).resolve().parents[1]
        self.config=ConsolidationConfig(self.project/'configs/skillpatch-consolidation.toml')
        self.first=self.make_patch('one')
        self.second=self.make_patch('two')
        self.merge={'action':'merge','reason':'Both clarify date boundaries.',
            'when_to_use':'When comparing dates from different zones.',
            'how_to_interpret':'Normalize to the requested zone and check day boundaries.'}

    def make_patch(self, name, text='Normalize dates before comparing.', quote=None):
        source=self.root/(name+'.json')
        write_json(source,{'dataset':'appworld','task_id':name,'instruction':'Check dates.',
            'retrieved_memory_text':text,'success':False,'steps':1,
            'trajectory':[{'action':'Compared local dates.'}]})
        report={'summary':'Date handling issue.','improvements':[{
            'target':{'skill_ref':'S0001','quote':quote or text},'diagnosis':'Local date used.',
            'suggestion':'Normalize dates.',
            'evidence':[{'evidence_id':'T000001','quote':'Compared local dates.'}]}]}
        discovery=self.root/(name+'-diagnosis')
        cfg=load_discovery_config(self.project/'configs/skillpatch-generation.toml')
        result=run_discovery(source,cfg,discovery,{},client=Client(report))
        self.assertEqual(result['status'],'completed',result)
        result=run_stage2(discovery,'I000001',Stage2Config(self.project/'configs/skillpatch-generation.toml'),
            self.root/(name+'-generation'),{},client=Client({'status':'candidate','reason':'Date handling.',
                'when_to_use':'When comparing dates.','how_to_interpret':'Normalize dates. '+name}))
        self.assertEqual(result['status'],'generated',result)
        source.unlink()  # Only unified archives are needed by consolidation.
        return Path(result['patch_path'])

    def run_proposal(self, client, incoming=None):
        return run_consolidation([self.first],incoming or self.second,'date-skill',self.config,
                                 self.root/'out',{},client=client)

    def test_merge_preserves_separate_context_namespaces_and_inputs(self):
        before={p:tree_hashes(p) for p in self.root.glob('*-generation')}
        client=Client(self.merge); result=self.run_proposal(client)
        self.assertEqual(result['status'],'proposed',result)
        self.assertEqual(result['action'],'merge')
        self.assertFalse(result['ready_for_activation'])
        self.assertEqual(len(client.calls),1)
        request, first, second=client.calls[0]
        self.assertEqual(first['task_id'],'one'); self.assertEqual(second['task_id'],'two')
        self.assertEqual(first['items'][0]['context_id'],second['items'][0]['context_id'])
        self.assertEqual(request['existing'][0]['collection'],'source_0')
        self.assertEqual(request['incoming']['collection'],'source_1')
        for path, hashes in before.items(): self.assertEqual(tree_hashes(path),hashes)
        record=json.loads(Path(result['proposal_path']).read_text())
        self.assertEqual(record['proposal'],self.merge)
        self.assertEqual(len(record['source_patches']),2)

    def test_keep_and_defer_have_no_replacement_text(self):
        for action in ('keep','defer'):
            with self.subTest(action=action):
                result=run_consolidation([self.first],self.second,'date-skill',self.config,
                    self.root/action,{},client=Client({'action':action,'reason':'Retain both.',
                    'when_to_use':None,'how_to_interpret':None}))
                self.assertEqual(result['status'],'proposed',result)
                self.assertEqual(result['action'],action)
                self.assertFalse(result['ready_for_activation'])

    def test_different_skill_text_fails_before_model(self):
        other=self.make_patch('other','Check timestamps before comparing.')
        client=Client(self.merge); result=self.run_proposal(client,other)
        self.assertEqual(result['status'],'failed',result); self.assertEqual(client.calls,[])

    def test_different_anchor_fails_before_model(self):
        other=self.make_patch('other','Normalize dates before comparing.','Normalize dates')
        client=Client(self.merge); result=self.run_proposal(client,other)
        self.assertEqual(result['status'],'failed',result); self.assertEqual(client.calls,[])

    def test_tampered_patch_is_rejected(self):
        raw=json.loads(self.second.read_text()); raw['how_to_interpret']='Tampered'
        write_json(self.second,raw)
        with self.assertRaises(ValueError):self.run_proposal(Client(self.merge))

    def test_duplicate_patch_is_rejected(self):
        client=Client(self.merge); result=self.run_proposal(client,self.first)
        self.assertEqual(result['status'],'failed',result); self.assertEqual(client.calls,[])

    def test_invalid_proposal_and_agent_failure_are_not_successful(self):
        for name,client in [('invalid',Client({**self.merge,'action':'keep'})),
                            ('error',Client(self.merge,status='failed')),
                            ('mutate',Client(self.merge,mutate=True))]:
            result=run_consolidation([self.first],self.second,'date-skill',self.config,
                                     self.root/name,{},client=client)
            self.assertEqual(result['status'],'failed',result)
            self.assertFalse(result['ready_for_activation'])
            self.assertFalse((self.root/name/'proposal/proposal.json').exists())

    def test_output_inside_source_is_rejected(self):
        with self.assertRaises(ValueError):
            run_consolidation([self.first],self.second,'date-skill',self.config,
                              self.first.parent/'out',{},client=Client(self.merge))

    def test_config_and_cli(self):
        from set_up.cli import main
        self.assertEqual(load_consolidation_config(self.project/'configs/skillpatch-stage3.toml'),self.config)
        with patch('method.skillpatch.patch_consolidation.pipeline.run_consolidation',
                   return_value={'status':'proposed'}) as run, patch('builtins.print'):
            code=main(['stage3','--existing',str(self.first),'--incoming',str(self.second),
                '--skill-key','date-skill','--config',str(self.project/'configs/skillpatch-stage3.toml'),
                '--output',str(self.root/'out')])
        self.assertEqual(code,0); self.assertEqual(run.call_args.args[0],[self.first])

if __name__=='__main__':unittest.main()
