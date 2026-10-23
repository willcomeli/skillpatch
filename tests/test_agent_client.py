"""Exercise the real harness subprocess boundary without a model service."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from harness.agent_client import AgentClient, AgentConfig

SCRIPT = '''
import json,os,pathlib,re,sys,time
prompt=pathlib.Path(sys.argv[sys.argv.index('--file')+1]).read_text()
report=pathlib.Path(re.search(r'Write the final JSON object to: (.+)',prompt).group(1))
mode=os.environ.get('FAKE_MODE','ok')
if mode=='timeout':time.sleep(10)
if mode!='missing':report.write_text('{"summary":"fixture","improvements":[]}')
if mode=='invalid':report.write_text('{broken')
print(json.dumps({'kind':'Condensation','forgotten_event_ids':['one']}))
if mode=='error':print(json.dumps({'kind':'ConversationErrorEvent','code':'TypeError','detail':'fixture error'}))
print(json.dumps({'kind':'ActionEvent','action':{'kind':'FinishAction','message':'done'}}))
'''


class AgentClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.script=self.root/'fake.py';self.script.write_text(SCRIPT)
        self.material=self.root/'context.json';self.material.write_text('{}')
        self.config=AgentConfig('fixture','https://example.invalid',command=(sys.executable,str(self.script)),timeout_sec=1)

    def run_mode(self,mode):
        return AgentClient().run(self.config,'Analyze material',[self.material],self.root/mode,
                                 {'DEEPSEEK_API_KEY':'fixture-secret-key','FAKE_MODE':mode})

    def test_report_and_terminal_event_are_required(self):
        result=self.run_mode('ok')
        self.assertEqual(result.status,'completed')
        self.assertTrue(result.report_path.is_file())
        self.assertEqual(result.final_text,'done')
        self.assertNotIn('fixture-secret-key',(self.root/'ok/request.json').read_text())

    def test_zero_exit_with_conversation_error_is_failure(self):
        result=self.run_mode('error')
        self.assertEqual(result.return_code,0)
        self.assertEqual(result.status,'failed')
        self.assertTrue(any('TypeError' in e for e in result.errors))

    def test_missing_or_invalid_report_fails(self):
        for mode in ('missing','invalid'):
            with self.subTest(mode=mode):self.assertEqual(self.run_mode(mode).status,'failed')

    def test_timeout_stops_child_and_saves_status(self):
        self.assertEqual(self.run_mode('timeout').status,'timeout')
        self.assertEqual(json.loads((self.root/'timeout/result.json').read_text())['status'],'timeout')
