import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from method.skillaligner.core import adapt, messages_for, retrieve, validate_guide


class SkillAlignerTests(unittest.TestCase):
    def test_bm25_and_fixed_candidates(self):
        library = [{'id': 'a', 'name': 'wash', 'text': 'wash apple'},
                   {'id': 'b', 'name': 'heat', 'text': 'heat soup'}]
        self.assertEqual(retrieve('soup', library, 1)[0]['id'], 'b')
        self.assertNotIn('retrieval_score', library[1])
        self.assertEqual(retrieve('soup', [], 5), [])

    def test_schema(self):
        valid = dict.fromkeys(('Primary', 'Checks', 'Avoid', 'Fallback'), [])
        self.assertEqual(validate_guide(valid), valid)
        for invalid in ({}, dict(valid, Checks='check'), dict(valid, Avoid=[1]), dict(valid, extra=[])):
            with self.assertRaises(ValueError):
                validate_guide(invalid)

    def test_exactly_one_call_and_saved_usage(self):
        guide = {'Primary': ['Search'], 'Checks': [], 'Avoid': [], 'Fallback': []}
        response = Mock()
        response.model_dump.return_value = {'choices': []}
        response.usage.model_dump.return_value = {'total_tokens': 30}
        response.choices = [SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(guide)))]
        client = Mock()
        client.chat.completions.create.return_value = response
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'adapt'
            self.assertEqual(adapt('task', 'search tool', [], out, client, model='test'), guide)
            self.assertEqual(json.loads((out / 'result.json').read_text())['usage']['total_tokens'], 30)
            client.chat.completions.create.assert_called_once()
            response.choices[0].finish_reason = 'length'
            with self.assertRaises(ValueError):
                adapt('task', 'search tool', [], Path(tmp) / 'bad', client, model='test')
            self.assertFalse((Path(tmp) / 'bad/guide.json').exists())

    def test_prompt_and_literal_substitution(self):
        m = messages_for('<TASK_QUERY>', 'tools', [])
        self.assertIn('Task query: <TASK_QUERY>', m[1]['content'])
        self.assertIn('Task grounding:', m[1]['content'])
        self.assertIn('Skill composition:', m[1]['content'])
        self.assertNotIn('<FOR EACH CANDIDATE:', m[1]['content'])

    def test_cli_uses_guide_only_pack(self):
        from unittest.mock import patch
        from method_input_adapters.skillaligner.__main__ import main
        guide = {'Primary': ['Find apple'], 'Checks': [], 'Avoid': [], 'Fallback': []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / 'tasks.json'
            row = {'id': 'task', 'config': 'unused.toml', 'split': 'test',
                   'query': 'find apple', 'execution_specification': 'look',
                   'retrieved_skills': [{'id': 'raw', 'name': 'raw', 'text': 'RAW SECRET SKILL'}]}
            manifest.write_text(json.dumps([row]))
            with patch('method_input_adapters.execution.load_tasks', return_value=[row]), \
                 patch('set_up.environment.load_environment', return_value={'METHOD_API_KEY': 'test', 'METHOD_BASE_URL': 'http://unused'}), \
                 patch('openai.OpenAI'), \
                 patch('method_input_adapters.skillaligner.__main__.adapt', return_value=guide) as adaptation, \
                 patch('method_input_adapters.skillaligner.adapter.read_benchmark', return_value='skillsbench'), \
                 patch('method_input_adapters.execution.SharedExecutorBridge') as bridge, \
                 patch('method_input_adapters.archive.load_archive', return_value=SimpleNamespace(success=True, score=1)):
                bridge.return_value.run.return_value = root / 'archive'
                main(['--manifest', str(manifest), '--output', str(root / 'out'), '--model', 'test'])
                adaptation.assert_called_once()
                kwargs = bridge.return_value.run.call_args.kwargs
                pack = kwargs['skill_pack']
                files = list(pack.rglob('*'))
                self.assertEqual([p.name for p in files if p.is_file()], ['SKILL.md'])
                content = (pack / 'execution-guide/SKILL.md').read_text()
                self.assertIn('Find apple', content)
                self.assertNotIn('RAW SECRET SKILL', content)
