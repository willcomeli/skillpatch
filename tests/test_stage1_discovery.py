"""Stage1 archive-to-report wiring, isolated from model and environment execution."""

import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from method_input_adapters.skillpatch.models import DiagnosisContext, ContextItem
from harness.agent_client import AgentConfig, AgentResult
from excutor.executor_adapter.storage import write_json
from method.skillpatch.patch_diagnosis.config import DiscoveryConfig
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from set_up.cli import main


class FakeClient:
    def __init__(self,payload=None,status='completed',mutate=False):
        self.payload=payload or {'summary':'No supported improvements.','improvements':[]}
        self.status=status;self.mutate=mutate;self.calls=0
    def run(self,config,prompt,materials,output,environment,**kwargs):
        self.calls+=1
        workspace=output/'workspace';workspace.mkdir(parents=True)
        path=workspace/'report.json';write_json(path,self.payload)
        if self.mutate:materials[0].write_text('{}')
        return AgentResult(self.status,output,path,return_code=0)


class Stage1Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'native.json';self.source.write_text('{}')
        prompt=self.root/'prompt.md';prompt.write_text('Analyze observed skill use.')
        self.config=DiscoveryConfig(AgentConfig('fixture','https://example.invalid'),prompt)
        self.context=DiagnosisContext('run','appworld','fixture_1','task',None,None,[
            ContextItem('Q0001','task','Do the task.',str(self.source)),
            ContextItem('S0001','skill','Check dates.',str(self.source)),
            ContextItem('T000001','trajectory','Used local dates.',str(self.source)),
            ContextItem('F0001','feedback','Date check failed.',str(self.source)),
        ],[], '', evolution_eligibility='eligible', eligibility_reason='fixture_failure')
    def run_stage(self,client=None,**kwargs):
        return run_discovery(self.source,self.config,self.root/'output',{},client=client,
                             input_builder=lambda _:copy.deepcopy(self.context),**kwargs)

    def test_prepare_does_not_call_agent_or_enforce_character_limit(self):
        self.context.items[0]=ContextItem('Q0001','task','x'*700000,str(self.source))
        client=FakeClient();state=self.run_stage(client,prepare_only=True)
        self.assertEqual(state['status'],'prepared');self.assertEqual(client.calls,0)
        self.assertGreater((self.root/'output/input/context.json').stat().st_size,600000)

    def test_empty_report_is_valid_and_marked_candidate_only(self):
        state=self.run_stage(FakeClient())
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['semantic_status'],'candidate_diagnosis')
        self.assertTrue((self.root/'output/report.md').is_file())

    def test_native_report_preserves_model_output_and_snapshots_input(self):
        client=FakeClient({'summary':'Date interpretation needs clarification.','improvements':[{
            'target':{'skill_ref':'S0001','quote':'Check dates.'},
            'diagnosis':'Local dates were used despite the requirement.',
            'suggestion':'Normalize dates before comparison.',
            'evidence':[{'evidence_id':'T000001','start_char':0,'end_char':17}],
        }]})
        state=self.run_stage(client)
        self.assertEqual(state['status'],'completed',state)
        import json
        report=json.loads((self.root/'output/report.json').read_text())
        self.assertEqual(report, client.payload)
        self.assertEqual((self.root/'output/input/delivered_skills/delivered_S0001/SKILL.md').read_text(),
                         'Check dates.')

    def test_agent_failure_or_input_tampering_cannot_produce_report(self):
        for mode in ('error','tamper'):
            with self.subTest(mode=mode):
                output=self.root/mode
                state=run_discovery(self.source,self.config,output,{},
                    client=FakeClient(status='failed' if mode=='error' else 'completed',mutate=mode=='tamper'),
                    input_builder=lambda _:copy.deepcopy(self.context))
                self.assertIn(state['status'],{'failed'})
                self.assertFalse((output/'report.json').exists())

    def test_bad_reference_is_saved_without_validation(self):
        client=FakeClient({'summary':'Issue','improvements':[{'target':{'skill_ref':'S0001','quote':'absent'},
            'diagnosis':'Issue','suggestion':'Fix','evidence':[{'evidence_id':'T000001'}]}]})
        state=self.run_stage(client)
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['report_validation'],'disabled')
        self.assertFalse((self.root/'output/validation.json').exists())
        import json
        self.assertEqual(json.loads((self.root/'output/report.json').read_text()), client.payload)

    def test_cli_stage1_alias_routes_to_discovery(self):
        with patch('method.skillpatch.patch_diagnosis.config.load_discovery_config',return_value=self.config), \
             patch('method.skillpatch.patch_diagnosis.discovery.run_discovery',return_value={'status':'prepared'}) as run, \
             patch('builtins.print'):
            code=main(['stage1','--run-dir',str(self.source),'--config','fixture.toml','--prepare-only'])
        self.assertEqual(code,0);self.assertTrue(run.call_args.kwargs['prepare_only'])

    def test_success_and_invalid_are_skipped_without_calling_agent(self):
        for eligibility in ('succeeded','invalid'):
            context=copy.deepcopy(self.context)
            context.evolution_eligibility=eligibility
            context.eligibility_reason='fixture_'+eligibility
            client=FakeClient()
            state=run_discovery(self.source,self.config,self.root/eligibility,{},client=client,
                                input_builder=lambda _:context)
            self.assertEqual(state['status'],'skipped',state)
            self.assertEqual(state['reason'],eligibility)
            self.assertEqual(state['eligibility_reason'],'fixture_'+eligibility)
            self.assertEqual(client.calls,0)
            self.assertFalse((self.root/eligibility/'agent').exists())
            self.assertIn('finished_at',state)

    def test_prepare_only_also_obeys_eligibility(self):
        self.context.evolution_eligibility='invalid'
        client=FakeClient();state=self.run_stage(client,prepare_only=True)
        self.assertEqual(state['status'],'skipped')
        self.assertEqual(client.calls,0)

    def test_cli_skip_is_successful_command(self):
        with patch('method.skillpatch.patch_diagnosis.config.load_discovery_config',return_value=self.config), \
             patch('method.skillpatch.patch_diagnosis.discovery.run_discovery',return_value={'status':'skipped'}), \
             patch('builtins.print'):
            code=main(['stage1','--run-dir',str(self.source),'--config','fixture.toml'])
        self.assertEqual(code,0)

    def test_duplicate_target_passage_with_offsets_is_saved_unchanged(self):
        self.context.items[1] = replace(self.context.items[1], text='Check dates. Check dates.')
        client = FakeClient({'summary': 'Repeated anchor', 'improvements': [{
            'target': {'skill_ref': 'S0001', 'start_char': 0, 'end_char': 12},
            'diagnosis': 'Issue', 'suggestion': 'Fix', 'evidence': []}]})
        state = self.run_stage(client)
        self.assertEqual(state['status'], 'completed', state)
        import json
        self.assertEqual(json.loads((self.root/'output/report.json').read_text()), client.payload)
