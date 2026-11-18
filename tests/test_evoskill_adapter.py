"""Exercise the actual upstream loop with shared-executor fixtures, no model API."""

import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from method_input_adapters.archive import write_json
from method_input_adapters.evoskill import SharedExecutionAdapter, archive_trace, export_results
from method_input_adapters.evoskill.upstream import load_module
from method_input_adapters.evoskill.adapter import native_skill_text
from excutor.executor_adapter.storage import tree_hashes


class EvoSkillAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'program'
        self.workspace.mkdir()
        self.config = self.root / 'execution.toml'
        self.config.write_text('[task]\nbenchmark="scienceworld"\n')
        self.tasks = [dict(id=split, split=split, config=str(self.config),
                           task_type='family', task_ref={'task_id': split})
                      for split in ('train', 'val', 'test')]

    def archive(self, path, task='train', score=0.0):
        write_json(path, dict(dataset='scienceworld', task_id=task, instruction='Inspect the sample.',
                             success=score == 1, score=score, steps=1, trajectory_complete=None,
                             retrieved_memory_text='', trajectory=[dict(step=0, observation='Sample visible.',
                             action='look at sample', thought='Inspect first.', next_observation='A sample.',
                             score_before=0, score_after=score, done=True)]))
        return path

    def adapter(self, bridge, **kwargs):
        return SharedExecutionAdapter(self.tasks, bridge, self.workspace, self.root / 'executions',
                                      model='fixture', score_mode=kwargs.get('score_mode', 'hard'), concurrency=2)

    def test_export_uses_real_upstream_trace_preserves_steps_and_official_feedback(self):
        source = self.archive(self.root / 'source.json', score=0.4)
        before = source.read_bytes()
        rows = export_results([source], self.root / 'export')
        raw = json.loads(Path(rows[0]['trace_path']).read_text())
        cls = load_module('harness').AgentTrace[load_module('schemas').AgentResponse]
        trace = cls.model_validate(raw)
        result = json.loads(trace.result)
        self.assertEqual(result['score'], 0.4)
        self.assertEqual(result['conversation'][2]['action'], 'look at sample')
        self.assertEqual(result['conversation'][2]['env_feedback'], 'A sample.')
        self.assertNotIn('trajectory_complete', result)
        self.assertFalse(any('completeness' in line for line in result['limitations']))
        self.assertEqual(source.read_bytes(), before)
        with self.assertRaises(ValueError):
            export_results([source, source], self.root / 'duplicate')

    def test_invalid_feedback_is_not_converted_to_zero(self):
        source = self.archive(self.root / 'bad.json')
        raw = json.loads(source.read_text()); raw['score'] = None
        write_json(source, raw)
        with self.assertRaises(ValueError):
            archive_trace(source)

    def test_live_candidate_changes_rerun_executor_and_score_receipts_cannot_be_forged(self):
        calls = []
        def run(entry, content, output):
            calls.append((entry['id'], content))
            return self.archive(output / 'task.json', entry['id'], score=0.4)
        adapter = self.adapter(SimpleNamespace(run=run), score_mode='soft')
        skill = self.workspace / '.claude/skills/inspect/SKILL.md'
        skill.parent.mkdir(parents=True); skill.write_text('Inspect first.')
        async def check():
            first = await adapter.run('train')
            skill.write_text('Inspect twice.')
            second = await adapter.run('train')
            self.assertNotEqual(first.uuid, second.uuid)
            self.assertEqual(adapter.scorer('train', second.output.final_answer, 'official_executor_feedback'), 0.4)
            with self.assertRaises(ValueError):
                adapter.scorer('val', second.output.final_answer, 'official_executor_feedback')
            with self.assertRaises(ValueError):
                adapter.scorer('train', '1.0', 'official_executor_feedback')
        asyncio.run(check())
        self.assertIn('Inspect first.', calls[0][1])
        self.assertIn('Inspect twice.', calls[1][1])
        train, val, test = adapter.data()
        self.assertEqual(train['family'][0][0], 'train')
        self.assertEqual(val[0][0], 'val'); self.assertEqual(test[0][0], 'test')

    def test_skillsbench_delivers_whole_pack_native_rejects_binary_resources(self):
        for name in ('first', 'second'):
            skill = self.workspace / '.claude/skills' / name / 'SKILL.md'
            skill.parent.mkdir(parents=True); skill.write_text(name)
        resource = skill.parent / 'tool.py'; resource.write_text('print(1)')
        with self.assertRaises(ValueError):
            native_skill_text(self.workspace / '.claude/skills')
        calls = []
        def run(entry, content, output, *, skill_pack):
            calls.append(tree_hashes(skill_pack))
            return self.archive(output / 'task.json', entry['id'])
        adapter = self.adapter(SimpleNamespace(run=run))
        with patch('method_input_adapters.evoskill.adapter.read_benchmark', return_value='skillsbench'):
            asyncio.run(adapter.run('train'))
        self.assertIn('first/SKILL.md', calls[0])
        self.assertIn('second/tool.py', calls[0])

    def test_execution_errors_propagate_instead_of_becoming_task_failure(self):
        def fail(*args, **kwargs):
            raise ConnectionError('fixture connection lost')
        adapter = self.adapter(SimpleNamespace(run=fail))
        with self.assertRaises(ConnectionError):
            asyncio.run(adapter.run('train'))

    def test_whole_pack_bridge_preserves_new_skills_and_removes_absent_old_skills(self):
        from method_input_adapters.execution import SharedExecutorBridge
        task = self.root / 'task'
        old = task / 'environment/skills/old/SKILL.md'
        old.parent.mkdir(parents=True); old.write_text('Original')
        pack = self.root / 'candidate'
        for name in ('one', 'two'):
            path = pack / name / 'SKILL.md'
            path.parent.mkdir(parents=True); path.write_text(name)
        (pack / 'two/tool.py').write_text('print(1)')
        config = self.root / 'skillsbench.toml'
        config.write_text(f'[task]\nbenchmark="skillsbench"\npath="{task}"\n[agent]\nname="openhands"\nmodel="fixture"\n')
        calls = []
        class Executor:
            def __init__(self, *args): pass
            def run(self, request, output, environment):
                calls.append(request)
                output.mkdir()
                return SimpleNamespace(status='completed')
        before = tree_hashes(pack)
        with patch('excutor.excutors.Executor', Executor):
            SharedExecutorBridge({}).run(dict(config=str(config)), '', self.root / 'run', skill_pack=pack)
        delivered = calls[0].skills_dir
        self.assertFalse((delivered / 'old').exists())
        self.assertEqual((delivered / 'two/tool.py').read_text(), 'print(1)')
        self.assertEqual(tree_hashes(pack), before)
        self.assertEqual(old.read_text(), 'Original')

    def test_original_loop_samples_proposes_generates_selects_without_running_test_split(self):
        # A private real Git repo exercises upstream ProgramManager branch/tag logic.
        for args in (['init', '-q'], ['config', 'user.name', 'Fixture'],
                     ['config', 'user.email', 'fixture@example.invalid']):
            subprocess.run(['git', '-C', str(self.workspace), *args], check=True, capture_output=True)
        (self.workspace / '.gitignore').write_text('.evoskill/\n.cache/\n')
        subprocess.run(['git', '-C', str(self.workspace), 'add', '.gitignore'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'commit', '-qm', 'Initialize fixture'], check=True)
        calls, queries = [], []
        def execute(entry, content, output):
            calls.append((entry['id'], content))
            return self.archive(output / 'task.json', entry['id'], score=float('Verify identity' in content))
        adapter = self.adapter(SimpleNamespace(run=execute))
        schemas = load_module('schemas')
        harness = load_module('harness')
        upstream = load_module('loop')
        def trace(output):
            return harness.AgentTrace(duration_ms=0, total_cost_usd=0, num_turns=1,
                                      usage={}, result=str(output), is_error=False, output=output, messages=[])
        class Proposer:
            async def run(self, question):
                queries.append(question)
                return trace(schemas.SkillProposerResponse(proposed_skill='Verify identity before acting.',
                                                           justification='The saved action failed.'))
        class Generator:
            async def run(inner, question):
                path = self.workspace / '.claude/skills/verify/SKILL.md'
                path.parent.mkdir(parents=True)
                path.write_text('---\nname: verify\ndescription: Verify objects\n---\nVerify identity before acting.')
                return trace(schemas.ToolGeneratorResponse(generated_skill='verify', reasoning='Fixture'))
        agents = upstream.LoopAgents(base=None, skill_proposer=Proposer(), skill_generator=Generator(),
                                    prompt_proposer=None, prompt_generator=None)
        config = upstream.LoopConfig(max_iterations=1, samples_per_category=1, cache_enabled=True)
        manager = load_module('registry').ProgramManager(cwd=self.workspace)
        loop = adapter.build_loop(config, agents, manager)
        self.assertFalse(loop.config.cache_enabled)
        with patch('src.loop.runner.evaluate_agent_parallel', side_effect=AssertionError('Upstream timed wrapper used')):
            result = asyncio.run(loop.run())
        self.assertEqual(result.best_score, 1.0)
        self.assertEqual(result.best_program, 'iter-skill-1')
        manager.switch_to('base')
        self.assertEqual(manager.get_current().metadata['execution_backend'], 'shared_executor')
        self.assertEqual([q for q, _ in calls], ['val', 'train', 'val'])
        self.assertIn('look at sample', queries[0])
        self.assertIn('Official verifier feedback', queries[0])


if __name__ == '__main__':
    unittest.main()
