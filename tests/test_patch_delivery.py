"""Optional patch delivery preserves baseline execution and version binding."""

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from excutor.executor_adapter.models import RunRequest, BackendRun
from excutor.executor_adapter.skillsbench import SkillsBenchExecutionAdapter
from excutor.executor_adapter.storage import sha256_file
from excutor.excutor_runners.benchflow import BenchFlowBackend
from excutor.excutor_runners.patch_sidecars import write_sidecars
from excutor.excutors import Executor
from excutor.patch_delivery.skill_bank import PatchedSkillBank, optional_skill_patches
from excutor.eviskill_runners import AlfWorldRunner, ScienceWorldRunner, AppWorldRunner
from set_up.cli import main


class PatchDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / 'task'
        for name, text in {
            'task.md': 'Do the task.', 'environment/Dockerfile': 'FROM fixture',
            'verifier/test.sh': 'fixture',
            'environment/skills/example/SKILL.md': '---\nname: example\n---\nCheck dates.',
        }.items():
            path = self.task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.pack = self.root / 'patched'
        shutil.copytree(self.task / 'environment/skills', self.pack)
        self.skill = self.pack / 'example'
        write_sidecars(self.skill, [{
            'entry_id': 'p1', 'target': {'relative_path': 'SKILL.md', 'scope': 'substep',
                                       'quote': 'Check dates.', 'version': sha256_file(self.skill / 'SKILL.md')},
            'when_to_use': 'When dates have time zones.',
            'how_to_interpret': 'Compare dates in the same time zone.',
        }])
        self.request = RunRequest('skillsbench', self.task, 'openhands', 'fixture', ('bench',))
        self.executor = Executor(SkillsBenchExecutionAdapter(), BenchFlowBackend())

    def test_skillsbench_patch_and_baseline_are_isolated(self):
        original = (self.skill / 'SKILL.md').read_bytes()
        baseline = self.executor.run(self.request, self.root / 'baseline', {}, prepare_only=True)
        candidate = self.executor.run(self.request, self.root / 'candidate', {}, prepare_only=True,
                                      patch_skills_dir=self.pack)
        self.assertEqual((baseline.status, candidate.status), ('prepared', 'prepared'))
        self.assertNotIn('SKILL_PATCHES.md', (self.root / 'baseline/prompt/effective.md').read_text())
        self.assertIn('SKILL_PATCHES.md', (self.root / 'candidate/prompt/effective.md').read_text())
        self.assertEqual((candidate.prepared.skills_dir / 'example/SKILL.md').read_bytes(), original)
        self.assertFalse((baseline.prepared.skills_dir / 'example/SKILL_PATCHES.md').exists())
        self.assertTrue((self.root / 'candidate/patch_deployment.json').is_file())
        self.assertFalse((self.root / 'baseline/patch_deployment.json').exists())
        self.assertEqual(json.loads((self.root / 'candidate/skill_deployment_verification.json').read_text())['status'], 'verified')

    def test_cli_patch_option_previews_delivery_without_launch(self):
        config = self.root / 'run.toml'
        config.write_text('[task]\nbenchmark="skillsbench"\npath="task"\n[agent]\nname="openhands"\nmodel="fixture"\n')
        with patch('builtins.print'), patch.object(BenchFlowBackend, 'run') as run:
            code = main(['prepare', '--config', str(config), '--output', str(self.root / 'cli'),
                         '--patch-skills-dir', str(self.pack)])
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn('SKILL_PATCHES.md', (self.root / 'cli/prompt/effective.md').read_text())

    def test_bad_sidecars_and_changed_originals_fail_before_launch(self):
        for index, change in enumerate(('sidecar', 'original', 'disabled')):
            with self.subTest(change=change), patch.object(self.executor.backend, 'run') as run:
                request = self.request
                path = self.skill / ('SKILL_PATCHES.md' if change == 'sidecar' else 'SKILL.md')
                old = path.read_bytes()
                if change == 'disabled':
                    request = replace(request, explicit_skill_read=False)
                else:
                    path.write_text('tampered')
                result = self.executor.run(request, self.root / f'bad{index}', {}, patch_skills_dir=self.pack)
                path.write_bytes(old)
                self.assertEqual(result.status, 'preparation_error')
                run.assert_not_called()

    def bank(self):
        bank = Mock()
        bank.format_for_prompt.return_value = (self.skill / 'SKILL.md').read_text()
        bank.retrieve.return_value = {'general_skills': []}
        bank.retrieved_skill_ids.return_value = ['example']
        return bank

    def test_native_optional_entry_preserves_baseline_and_delivers_patch_once(self):
        @optional_skill_patches
        def run_task(skill_bank):
            memory = skill_bank.retrieve('query')
            return {'retrieved_memory_text': skill_bank.format_for_prompt(memory)}
        bank = self.bank()
        baseline = run_task(bank)
        candidate = run_task(bank, patch_skill_dir=self.skill)
        self.assertEqual(baseline['retrieved_memory_text'], bank.format_for_prompt.return_value)
        self.assertNotIn('patch_deployment', baseline)
        self.assertIn('Compare dates in the same time zone.', candidate['retrieved_memory_text'])
        self.assertEqual(candidate['retrieved_memory_text'].count('## Skill interpretation supplements'), 1)
        self.assertEqual(run_task(bank), baseline)
        self.assertEqual(candidate['patch_deployment']['presentation_mode'], 'skill_text_with_sidecar')
        for runner in (AlfWorldRunner, ScienceWorldRunner, AppWorldRunner):
            self.assertTrue(hasattr(runner.run_task, '__wrapped__'))
            self.assertTrue(hasattr(runner.run_task_set, '__wrapped__'))

    def test_native_rejects_mismatched_skill_and_patch_drift(self):
        bank = self.bank()
        wrapped = PatchedSkillBank(bank, self.skill)
        bank.format_for_prompt.return_value = 'a different skill'
        with self.assertRaisesRegex(ValueError, 'match'):
            wrapped.format_for_prompt({})
        bank.format_for_prompt.return_value = wrapped.original
        (self.skill / 'SKILL_PATCHES.md').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            wrapped.format_for_prompt({})

    def test_appworld_real_runner_loop_receives_optional_supplement(self):
        runner = AppWorldRunner.__new__(AppWorldRunner)
        runner.action_agent = Mock()
        runner.action_agent.act.return_value = {'action': 'print(42)'}
        runner.app_descriptions = []
        runner.max_steps = 1
        runner.action_history_length = 8
        runner.top_k_general_skills = runner.top_k_task_specific_skills = 1
        runner.general_skill_retrieval_threshold = runner.task_specific_skill_retrieval_threshold = 0
        runner.task_specific_retrieval_scope = 'detected'
        runner.env = Mock()
        runner.env.initialize.return_value = {'instruction': 'fixture task'}
        runner.env.execute.return_value = '42'
        runner.env.task_completed.return_value = True
        runner.env.evaluate.return_value = (True, {}, [])
        result = runner.run_task({'task_id': 'fixture_1'}, self.bank(), patch_skill_dir=self.skill)
        self.assertIn('Compare dates in the same time zone.', runner.action_agent.act.call_args.args[3])
        self.assertEqual(result['retrieved_memory_text'], runner.action_agent.act.call_args.args[3])
        self.assertTrue(result['success'])

    def test_alfworld_and_scienceworld_loops_deliver_patch_only_when_requested(self):
        for cls in (AlfWorldRunner, ScienceWorldRunner):
            with self.subTest(runner=cls.__name__):
                runner = cls.__new__(cls)
                runner.action_agent = Mock()
                runner.action_agent.act.return_value = {'action': 'look'}
                runner.max_steps = 1
                runner.action_history_length = 8
                runner.top_k_general_skills = runner.top_k_task_specific_skills = 1
                runner.general_skill_retrieval_threshold = runner.task_specific_skill_retrieval_threshold = 0
                runner.task_specific_retrieval_scope = 'detected'
                task = {'task_id': 'fixture', 'task': 'fixture', 'task_family': 'fixture'}
                if cls is AlfWorldRunner:
                    runner._task_from_id = Mock(return_value=('fixture', 'fixture', None, 'fixture'))
                    runner._reset_to_task = Mock(return_value=('start', {}, 'Do the task.'))
                    runner._admissible = Mock(return_value=['look'])
                    runner._step = Mock(return_value=('finished', 1.0, True, {}))
                else:
                    runner.use_admissible_actions = False
                    runner.env = Mock()
                    runner.env.reset.return_value = ('start', {'score': 0}, 'Do the task.')
                    runner.env.step.return_value = ('finished', 1.0, True, {})
                base = runner.run_task(task, self.bank())
                self.assertNotIn('patch_deployment', base)
                self.assertNotIn('Skill interpretation supplements', base['retrieved_memory_text'])
                candidate = runner.run_task(task, self.bank(), patch_skill_dir=self.skill)
                self.assertTrue(candidate['success'])
                self.assertIn('Compare dates in the same time zone.', runner.action_agent.act.call_args.args[3])
                self.assertIn('patch_deployment', candidate)

    def test_batch_optional_delivery_and_checkpoint_reuse_guard(self):
        @optional_skill_patches
        def run_task_set(task_ids, skill_bank, checkpoint_dir=None):
            return [{'retrieved_memory_text': skill_bank.format_for_prompt({})} for _ in task_ids]
        rows = run_task_set([1, 2], self.bank(), patch_skill_dir=self.skill)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('patch_deployment' in row for row in rows))
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            run_task_set([1], self.bank(), checkpoint_dir='old', patch_skill_dir=self.skill)
