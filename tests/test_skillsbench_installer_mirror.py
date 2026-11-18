import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.skillsbench.installer_mirror import native_install, route


class InstallerMirrorTests(unittest.TestCase):
    def fixture(self, folder):
        folder = Path(folder)
        original, wrapper = folder / 'uv.original', folder / 'uv'
        original.write_bytes(b'original binary')
        wrapper.write_bytes(b'temporary router')
        overrides = folder / 'overrides.txt'
        overrides.write_text('openhands-sdk==1.22.1\nopenhands-tools==1.22.1\n')
        spec = {'original_binary': str(original), 'wrapper_path': str(wrapper),
            'original_sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
            'wrapper_sha256': hashlib.sha256(wrapper.read_bytes()).hexdigest(),
            'previous_index': 'https://mirrors.ustc.edu.cn/pypi/simple/',
            'index_url': 'https://mirrors.aliyun.com/pypi/simple/',
            'native_source': 'git+https://github.com/OpenHands/OpenHands-CLI.git@pinned',
            'overrides': overrides.read_text().splitlines(), 'events_file': str(folder / 'events.jsonl')}
        args = ['tool', 'install', '--force', '--refresh', '--overrides', str(overrides),
                '--from', spec['native_source'], 'openhands', '--python', '3.12']
        env = {'UV_INDEX_URL': spec['previous_index'], 'OTHER': 'preserved'}
        return spec, args, env

    def test_success_preserves_arguments_and_restores_exact_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            spec, args, env = self.fixture(folder)
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                return SimpleNamespace(returncode=0)
            self.assertEqual(route(args, spec, environment=env, run=run), 0)
            self.assertEqual(calls[0][0], [spec['original_binary'], *args])
            self.assertEqual(calls[0][1]['env'], {**env, 'UV_INDEX_URL': spec['index_url'], 'PIP_INDEX_URL': spec['index_url']})
            self.assertEqual(Path(spec['wrapper_path']).read_bytes(), b'original binary')
            self.assertFalse(Path(spec['original_binary']).exists())

    def test_failure_keeps_router_and_exit_code_for_existing_step_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            spec, args, env = self.fixture(folder)
            self.assertEqual(route(args, spec, environment=env,
                                   run=lambda *a, **k: SimpleNamespace(returncode=23)), 23)
            self.assertEqual(Path(spec['wrapper_path']).read_bytes(), b'temporary router')
            self.assertEqual(Path(spec['original_binary']).read_bytes(), b'original binary')

    def test_other_commands_indexes_and_pins_are_not_rerouted(self):
        with tempfile.TemporaryDirectory() as folder:
            spec, args, env = self.fixture(folder)
            for other in [[], ['--version'], ['pip', 'install', 'example'],
                          [*args, '--index-url', 'https://explicit.example/simple/']]:
                self.assertFalse(native_install(other, env, spec))
                captured = []
                route(other, spec, environment=env, passthrough=lambda *a: captured.append(a))
                self.assertEqual(captured[0][1:], ([spec['original_binary'], *other], env))
            Path(args[args.index('--overrides') + 1]).write_text('openhands-sdk==different\n')
            self.assertFalse(native_install(args, env, spec))


if __name__ == '__main__':
    unittest.main()
