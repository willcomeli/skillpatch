"""Eligibility distinguishes scored failures from unusable outcomes."""
import unittest
import tempfile
from pathlib import Path
from method_input_adapters.skillpatch.eligibility import reward_eligibility, native_eligibility
from method_input_adapters.skillpatch import build_diagnosis_context
from excutor.executor_adapter.storage import write_json


class EligibilityTests(unittest.TestCase):
    def test_legacy_reward_policy_accepts_partial_and_nonbinary_rewards(self):
        for reward,expected in ((1,'succeeded'),(0,'eligible'),(0.5,'eligible'),(-1,'eligible'),(2,'eligible')):
            self.assertEqual(reward_eligibility({'status':'scored','metrics':{'reward':reward}}),expected)

    def test_missing_nonfinite_and_non_numeric_rewards_are_invalid(self):
        for reward in (None,True,False,'0',float('nan'),float('inf'),10**1000):
            self.assertEqual(reward_eligibility({'status':'scored','metrics':{'reward':reward}}),'invalid')
        for feedback in ({},{'status':'error','metrics':{'reward':0}},{'status':'scored','metrics':[]},None):
            self.assertEqual(reward_eligibility(feedback),'invalid')

    def test_native_official_outcomes_and_conflicts(self):
        for benchmark in ('alfworld','scienceworld','appworld'):
            for record,expected in (({'success':False,'score':0},'eligible'),
                                    ({'success':True,'score':1},'succeeded'),
                                    ({'score':0},'invalid'),
                                    ({'success':True,'score':0},'invalid'),
                                    ({'success':False,'score':'0'},'invalid'),
                                    ({'success':False,'error':'timeout'},'invalid')):
                self.assertEqual(native_eligibility(record,benchmark)[0],expected)
        self.assertEqual(native_eligibility({'success':False,'score':1},'scienceworld')[0],'eligible')
        self.assertEqual(native_eligibility({'success':True,'score':1,'task_completed':False},'appworld')[0],'invalid')
        for record in ({'success':False,'score':0,'score_available':False},
                       {'success':False,'score':0,'trajectory':[{'score_after':None}]}):
            self.assertEqual(native_eligibility(record,'scienceworld')[0],'invalid')

    def test_appworld_missing_or_contradictory_test_results_are_invalid(self):
        for summary in ({'num_tests':0,'passed_tests':0,'failed_tests':0},
                        {'num_tests':1,'passed_tests':1,'failed_tests':0}):
            self.assertEqual(native_eligibility({'success':False,'score':0,
                'evaluation_summary':summary},'appworld')[0],'invalid')
        self.assertEqual(native_eligibility({'success':False,'score':0,
            'evaluation_summary':{'num_tests':2,'passed_tests':1,'failed_tests':1}},'appworld')[0],'eligible')

    def test_native_adapters_record_eligibility_and_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'task.json'
            for benchmark in ('alfworld','scienceworld','appworld'):
                for success,expected in ((False,'eligible'),(True,'succeeded'),(None,'invalid')):
                    write_json(path,{'dataset':benchmark,'task_id':'task','instruction':'Do task.',
                        'trajectory':[],'success':success,'score':int(bool(success))})
                    context=build_diagnosis_context(path)
                    self.assertEqual(context.evolution_eligibility,expected)
                    self.assertTrue(context.eligibility_reason)

    def test_skillsbench_adapter_uses_reward_instead_of_passed_flag(self):
        import test_diagnosis_adapter
        fixture=test_diagnosis_adapter.DiscoveryContextTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        for reward,expected in ((1,'succeeded'),(0,'eligible'),(0.5,'eligible'),(None,'invalid'),('0','invalid')):
            fixture.result['feedback']['metrics']={'reward':reward}
            fixture.result['feedback']['passed']=None
            fixture.save_result()
            self.assertEqual(build_diagnosis_context(fixture.root).evolution_eligibility,expected)
        fixture.result['feedback']['metrics']={'reward':0}
        fixture.result['feedback']['issues']=['Verifier error']
        fixture.save_result()
        self.assertEqual(build_diagnosis_context(fixture.root).evolution_eligibility,'invalid')

    def test_old_configuration_switch_is_rejected(self):
        from method.skillpatch.patch_diagnosis.config import load_discovery_config
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.toml'
            path.write_text('[diagnosis]\nmodel="test"\nbase_url="https://example.invalid"\n'
                'prompt_path="'+str(root/'prompts/templates/skillpatch/discovery.md')+'"\n'
                'include_successful_runs=true\n')
            with self.assertRaises(ValueError):load_discovery_config(path)

if __name__=='__main__':unittest.main()
