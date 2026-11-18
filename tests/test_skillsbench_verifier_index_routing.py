from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from scripts.skillsbench.verifier_index_routing import digest, routed_task, OFFICIAL


class Paths:
    def __init__(self, root):
        self.tests_dir = root / 'verifier'


class Task:
    def __init__(self, root):
        self._task_dir, self.paths, self.name = root, Paths(root), 'fix-build-agentops'
        self.config = SimpleNamespace(verifier=SimpleNamespace(timeout_sec=900, env={'REPO_ID': 'same'}))

    @property
    def task_dir(self):
        return self._task_dir


class VerifierIndexRoutingTests(unittest.TestCase):
    def test_only_download_literals_and_scoped_environment_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / 'verifier').mkdir(parents=True)
            texts = {'test_outputs.py': f'INDEX="{OFFICIAL}"\nassert original_test()\n',
                     'run_passed.sh': f'export PIP_INDEX_URL={OFFICIAL}\ntox\n'}
            for name, value in texts.items(): (source / 'verifier' / name).write_text(value)
            policy = {'index_url': 'https://mirrors.ustc.edu.cn/pypi/simple',
                      'original_hashes': {name: digest(source / 'verifier' / name) for name in texts}}
            task = Task(source)
            result = routed_task(task, root / 'routing', policy)
            for name, text in texts.items():
                self.assertEqual((source / 'verifier' / name).read_text(), text)
                self.assertEqual((result.paths.tests_dir / name).read_text().replace(policy['index_url'], OFFICIAL), text)
            self.assertEqual(result.config.verifier.timeout_sec, 900)
            self.assertEqual(task.config.verifier.env, {'REPO_ID': 'same'})
            self.assertEqual(result.config.verifier.env['REPO_ID'], 'same')

    def test_changed_original_verifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source'; (source / 'verifier').mkdir(parents=True)
            (source / 'verifier/test_outputs.py').write_text(OFFICIAL)
            with self.assertRaisesRegex(ValueError, 'changed'):
                routed_task(Task(source), root / 'routing',
                            {'index_url': 'https://mirrors.ustc.edu.cn/pypi/simple', 'original_hashes': {'test_outputs.py': 'wrong'}})


if __name__ == '__main__':
    unittest.main()
