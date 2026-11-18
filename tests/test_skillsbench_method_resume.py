"""Recovery must reuse completed Skills and preserve interrupted archives."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.skillsbench import resume_methods as resume


class MethodResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'original'
        self.recovery = self.root / 'recovery'
        self.skills = self.root / 'reference'
        self.baseline = self.root / 'baseline'
        self.baseline.mkdir()
        (self.baseline / 'evidence').write_text('immutable')
        for name in ('alpha', 'beta'):
            p = self.skills / name
            p.mkdir(parents=True)
            (p / 'SKILL.md').write_text('original ' + name)
            (p / 'helper.py').write_text('keep helper')
        self.request = resume.exp.RunRequest(benchmark='skillsbench', task_dir=self.root / 'task',
            skills_dir=self.skills, agent='openhands', model='executor', bench_command=('bench',))

    def finish(self, folder, name):
        folder.mkdir(parents=True, exist_ok=True)
        summary = {'total_steps': 1, 'config': {'out_root': str(folder),
                    'skill_init': str(self.skills / name / 'SKILL.md')}}
        resume.exp.write_json(folder / 'summary.json', summary)
        resume.exp.write_json(folder / 'runtime_state.json', {'last_completed_step': 1})
        (folder / 'best_skill.md').write_text('selected ' + name)
        return summary

    def test_reuses_finished_skill_and_restarts_only_unfinished_skill(self):
        self.finish(self.original / 'skills/alpha', 'alpha')
        interrupted = self.original / 'skills/beta'
        interrupted.mkdir(parents=True)
        (interrupted / 'candidate_skill.md').write_text('old interrupted candidate')
        calls = []

        def optimize(baseline, request, name, output, *args):
            calls.append(name)
            self.assertNotEqual(output, interrupted)
            return self.finish(output, name)

        def execute(request, output, environment):
            for name in ('alpha', 'beta'):
                self.assertEqual((request.skills_dir / name / 'SKILL.md').read_text(), 'selected ' + name)
                self.assertEqual((request.skills_dir / name / 'helper.py').read_text(), 'keep helper')
            self.assertNotEqual(output, self.original / 'source_replay')
            output.mkdir()
            return output

        before = resume.exp.tree_hashes(self.original)
        with patch.object(resume.exp, 'optimize_skillopt_one', side_effect=optimize), \
                patch.object(resume.exp, 'execute', side_effect=execute), \
                patch.object(resume.exp, 'comparison', return_value={'candidate_score': 1}):
            result = resume.recover_pack(self.baseline, self.request, self.original, self.recovery,
                                         {}, SimpleNamespace(), self.root / 'config')
        self.assertEqual(calls, ['beta'])
        self.assertEqual(resume.exp.tree_hashes(self.original), before)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.skills / 'beta/SKILL.md').read_text(), 'original beta')

    def test_second_resume_uses_completed_skill_checkpoint_after_replay_failure(self):
        for name in ('alpha', 'beta'):
            self.finish(self.original / 'skills' / name, name)
        with patch.object(resume.exp, 'execute', side_effect=ConnectionError('lost container')):
            with self.assertRaises(ConnectionError):
                resume.recover_pack(self.baseline, self.request, self.original, self.recovery,
                                     {}, SimpleNamespace(), self.root / 'config')
        with patch.object(resume.exp, 'optimize_skillopt_one', side_effect=AssertionError('must reuse')), \
                patch.object(resume.exp, 'execute', return_value=self.root / 'replay'), \
                patch.object(resume.exp, 'comparison', return_value={}):
            resume.recover_pack(self.baseline, self.request, self.original, self.recovery,
                                 {}, SimpleNamespace(), self.root / 'config')
        saved = json.loads((self.recovery / 'progress.json').read_text())
        self.assertEqual(set(saved['skills']), {'alpha', 'beta'})

    def test_rejects_summary_for_a_different_initial_skill(self):
        folder = self.original / 'skills/alpha'
        self.finish(folder, 'beta')
        with self.assertRaisesRegex(ValueError, 'Inconsistent'):
            resume.completed_skill(folder, self.skills / 'alpha/SKILL.md')

    def test_nested_completed_attempts_from_previous_recovery_are_reused(self):
        progress = {'skills': {}}
        for name in ('alpha', 'beta'):
            folder = self.original / 'skills' / name / 'attempt_saved'
            self.finish(folder, name)
            progress['skills'][name] = {'folder': str(folder),
                'best_sha256': resume.exp.sha256_file(folder / 'best_skill.md')}
        resume.exp.write_json(self.original / 'progress.json', progress)
        with (patch.object(resume.exp, 'optimize_skillopt_one', side_effect=AssertionError('must reuse')),
                patch.object(resume.exp, 'execute', return_value=self.root / 'replay'),
                patch.object(resume.exp, 'comparison', return_value={})):
            result = resume.recover_pack(self.baseline, self.request, self.original, self.recovery,
                                         {}, SimpleNamespace(), self.root / 'config')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(all(row['selected_skill_source'].endswith('attempt_saved/best_skill.md') for row in result['skills']))


if __name__ == '__main__':
    unittest.main()
