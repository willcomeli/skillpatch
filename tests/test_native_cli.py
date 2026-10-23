"""Dataset dispatch, native preparation and saved-result diagnosis integration."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from set_up.cli import main
from set_up.native_config import load_native_config
from method_input_adapters.skillpatch import build_diagnosis_context
from excutor.executor_adapter.storage import sha256_file
from excutor.excutor_runners.patch_sidecars import write_sidecars
from excutor.eviskill_runners.launcher import create_runner, FileSkillBank


class NativeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'tasks.json').write_text('[{"task_id":"fixture_1"}]')
        self.skill = self.root / 'skill'
        self.skill.mkdir()
        (self.skill / 'SKILL.md').write_text('Check dates.')
        write_sidecars(self.skill, [{'entry_id': 'p1', 'target': {
            'relative_path': 'SKILL.md', 'scope': 'substep', 'quote': 'Check dates.',
            'version': sha256_file(self.skill / 'SKILL.md')},
            'when_to_use': 'When dates differ.', 'how_to_interpret': 'Normalize dates.'}])

    def config(self, benchmark, skill=True):
        path = self.root / f'{benchmark}.toml'
        options = {
            'alfworld': 'alfworld_path="."\nalfworld_config="env.yaml"\n',
            'scienceworld': 'remote_environment_url="http://localhost:8811"\n',
            'appworld': 'data_dir="."\nenvironment_url="http://localhost:8841"\n',
        }
        skill_line = 'skill_path="skill/SKILL.md"' if skill else 'skill_mode="no-skill"'
        path.write_text(f'[task]\nbenchmark="{benchmark}"\npath="tasks.json"\n{skill_line}\n'
                        '[agent]\nname="native"\nmodel="fixture"\nbase_url="http://localhost/v1"\n'
                        '[runtime]\nbackend="eviskill"\n[runtime.runner_options]\n' + options[benchmark])
        return path

    def invoke(self, config, output, extra=()):
        with redirect_stdout(io.StringIO()):
            return main(['prepare', '--config', str(config), '--output', str(output), *extra])

    def test_each_dataset_prepares_with_and_without_patch_without_starting_services(self):
        for benchmark in ('alfworld', 'scienceworld', 'appworld'):
            config = self.config(benchmark)
            for patched in (False, True):
                with self.subTest(benchmark=benchmark, patched=patched):
                    output = self.root / f'{benchmark}-{patched}'
                    extra = ['--patch-skill-dir', str(self.skill)] if patched else []
                    with patch('excutor.eviskill_runners.launcher.create_runner') as create:
                        self.assertEqual(self.invoke(config, output, extra), 0)
                        create.assert_not_called()
                    state = json.loads((output / 'result.json').read_text())
                    plan = json.loads((output / 'plan.json').read_text())
                    self.assertEqual(state['status'], 'prepared')
                    self.assertEqual(bool(plan['patch_skill_dir']), patched)
                    self.assertEqual((output / 'input/SKILL.md').read_text(), 'Check dates.')

    def test_factory_chooses_matching_runner_and_agent(self):
        for benchmark in ('alfworld', 'scienceworld', 'appworld'):
            config = load_native_config(self.config(benchmark))
            runner_class = Mock()
            # Preserve constructor signature validation using the real class.
            from excutor.eviskill_runners.launcher import RUNNERS
            import inspect
            runner_class.__signature__ = inspect.signature(RUNNERS[benchmark])
            with patch.dict(RUNNERS, {benchmark: runner_class}):
                create_runner(config, self.root, {'OPENAI_API_KEY': 'fixture-secret'})
            agent = runner_class.call_args.args[0]
            self.assertEqual(type(agent).__name__, {
                'alfworld': 'AlfWorldActionAgent', 'scienceworld': 'ScienceWorldActionAgent',
                'appworld': 'AppWorldActionAgent'}[benchmark])

    def test_run_saves_native_rows_consumable_by_diagnosis_and_closes_runner(self):
        config = self.config('appworld')
        output = self.root / 'run'
        runner = Mock()
        def run_task(task, bank, **kwargs):
            text = bank.format_for_prompt({})
            return {'dataset': 'appworld', 'task_id': task['task_id'], 'instruction': 'Do task',
                    'success': True, 'score': 1.0, 'steps': 0, 'trajectory': [],
                    'retrieved_memory_text': text}
        runner.run_task.side_effect = run_task
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=runner), redirect_stdout(io.StringIO()):
            code = main(['run', '--config', str(config), '--output', str(output)])
        self.assertEqual(code, 0)
        runner.close.assert_called_once()
        context = build_diagnosis_context(output / 'tasks/task_000001.json')
        self.assertEqual(context.benchmark, 'appworld')
        self.assertEqual(next(i.text for i in context.items if i.kind == 'skill'), 'Check dates.')
        self.assertNotIn('patch_skill_dir', runner.run_task.call_args.kwargs)

    def test_no_skill_and_wrong_patch_options(self):
        config = self.config('appworld', skill=False)
        self.assertEqual(self.invoke(config, self.root / 'no-skill'), 0)
        self.assertEqual(self.invoke(config, self.root / 'bad-patch', ['--patch-skill-dir', str(self.skill)]), 2)
        self.assertEqual(self.invoke(config, self.root / 'wrong-flag', ['--patch-skills-dir', str(self.skill)]), 2)

    def test_bad_runner_options_fail_during_prepare(self):
        config = self.config('appworld')
        config.write_text(config.read_text() + 'unknown_option=42\n')
        output = self.root / 'bad-options'
        self.assertEqual(self.invoke(config, output), 2)
        self.assertIn('runner_options', (output / 'result.json').read_text())

    def test_runner_is_closed_and_prior_results_survive_task_failure(self):
        config = self.config('appworld')
        (self.root / 'tasks.json').write_text('["fixture_1", "fixture_2"]')
        runner = Mock()
        runner.run_task.side_effect = [{'dataset': 'appworld', 'task_id': 'fixture_1'}, RuntimeError('failed')]
        output = self.root / 'partial'
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=runner), redirect_stdout(io.StringIO()):
            code = main(['run', '--config', str(config), '--output', str(output)])
        self.assertEqual(code, 2)
        runner.close.assert_called_once()
        self.assertTrue((output / 'tasks/task_000001.json').is_file())
        self.assertEqual(len(json.loads((output / 'result.json').read_text())['task_results']), 1)
