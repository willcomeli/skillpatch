import ast
import asyncio
from datetime import datetime
import hashlib
import logging
import os
import shlex
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from scripts.skillsbench.verifier_precache import PrecachePlanes, precache, prepare_commands


def specification(script=b'original verifier'):
    return {'uv_version': '0.9.7', 'requirements': ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5',
            'numpy==1.26.4', 'casadi==3.6.7'], 'verifier_script_sha256': hashlib.sha256(script).hexdigest()}


class VerifierPrecacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_uv_archive_is_checked_and_reused_by_unmodified_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'verifier').mkdir()
            (root / 'verifier/test.sh').write_bytes(b'original verifier')
            archive = root / 'uv-aarch64-unknown-linux-gnu.tar.gz'
            archive.write_bytes(b'original official archive')
            policy = specification()
            policy['uv_artifact'] = {'host_path': str(archive), 'filename': archive.name,
                'architecture': 'aarch64', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
            task = SimpleNamespace(task_dir=root, config=SimpleNamespace(
                verifier=SimpleNamespace(env={'HOME': '/root'}, timeout_sec=240)))
            async def execute(command, **kwargs):
                return SimpleNamespace(return_code=0, stdout='aarch64\n' if command == 'uname -m' else '', stderr='')
            env = SimpleNamespace(exec=AsyncMock(side_effect=execute), upload_file=AsyncMock())
            await precache(env, task, SimpleNamespace(rollout_dir=root / 'rollout'), policy)
            env.upload_file.assert_awaited_once()
            self.assertEqual(task.config.verifier.env['UV_DOWNLOAD_URL'], 'file:///var/cache/skillpatch/uv-0.9.7')
            commands = [call.args[0] for call in env.exec.await_args_list]
            self.assertTrue(any('sha256sum -c -' in command for command in commands))
            self.assertTrue(any('https://astral.sh/uv/0.9.7/install.sh' in command and 'UV_DOWNLOAD_URL=file://' in command for command in commands))
            self.assertEqual(task.config.verifier.timeout_sec, 240)
            archive.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                await precache(env, task, SimpleNamespace(rollout_dir=root / 'rollout'), policy)

    async def test_probe_ignores_unavailable_grader_paths_without_mutating_grader(self):
        original = {'PYTEST_ADDOPTS': '-c /dev/null --confcutdir=/absent-grader-directory --rootdir=/absent-grader-directory -p no:cacheprovider',
                    'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
        saved = dict(original)
        command = dict(prepare_commands(specification(), original))['python_dependencies']
        tokens = shlex.split(command)
        probe_env = dict(token.split('=', 1) for token in tokens[2:-3])
        self.assertEqual(original, saved)
        self.assertEqual(probe_env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'], '1')
        self.assertNotIn('absent-grader-directory', probe_env['PYTEST_ADDOPTS'])
        native_python = Path.home() / '.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12'
        if not native_python.exists():
            self.skipTest('Pinned native interpreter not available')
        dependency_path = os.environ.get('SKILLPATCH_TEST_PYTEST_PATH')
        if not dependency_path:
            self.skipTest('Set SKILLPATCH_TEST_PYTEST_PATH for the real pytest version-probe regression')
        probe_env['PYTHONPATH'] = dependency_path
        result = subprocess.run([str(native_python), '-m', 'pytest', '--version'],
                                env=probe_env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    async def test_only_dependency_version_check_no_tests_and_original_pins(self):
        commands = prepare_commands(specification(), {'HOME': '/root', 'PATH': '/usr/bin:/bin'})
        self.assertEqual([name for name, _ in commands],
                         ['apt_metadata', 'apt_packages', 'uv_installer', 'python_dependencies'])
        text = '\n'.join(command for _, command in commands)
        for pin in specification()['requirements']:
            self.assertIn(pin, text)
        self.assertIn('pytest --version', text)
        self.assertNotIn('test_outputs.py', text)
        self.assertNotIn('reward.txt', text)
        self.assertNotIn('--ctrf', text)

    async def test_retries_only_failed_preparation_stage_and_preserves_verifier_script(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'verifier').mkdir()
            script = root / 'verifier/test.sh'
            script.write_bytes(b'original verifier')
            task = SimpleNamespace(task_dir=root, config=SimpleNamespace(
                verifier=SimpleNamespace(env={'HOME': '/root'}, timeout_sec=240)))
            paths = SimpleNamespace(rollout_dir=root / 'rollout')
            ok = SimpleNamespace(return_code=0, stdout='pytest 8.4.1', stderr='')
            env = SimpleNamespace(exec=AsyncMock(side_effect=[ok, ok, ok, ConnectionError('connection reset'), ok]))
            with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
                await precache(env, task, paths, specification())
            self.assertEqual(env.exec.await_count, 5)
            calls = env.exec.await_args_list
            self.assertEqual(calls[3].args[0], calls[4].args[0])
            self.assertTrue(all(call.kwargs['timeout_sec'] is None for call in calls))
            self.assertEqual(task.config.verifier.timeout_sec, 240)
            self.assertEqual(script.read_bytes(), b'original verifier')
            self.assertTrue((paths.rollout_dir / 'verifier-precache/preparation.json').is_file())
            # The real Executor discovers native rollout results recursively.
            # Dependency audit metadata must never resemble another rollout.
            (paths.rollout_dir / 'result.json').write_text('{}')
            self.assertEqual(list(paths.rollout_dir.rglob('result.json')), [paths.rollout_dir / 'result.json'])

    async def test_changed_verifier_is_rejected_before_preparation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'verifier').mkdir()
            (root / 'verifier/test.sh').write_bytes(b'changed verifier')
            env = SimpleNamespace(exec=AsyncMock())
            with self.assertRaisesRegex(ValueError, 'script changed'):
                await precache(env, SimpleNamespace(task_dir=root), None, specification())
            env.exec.assert_not_called()

    async def test_pinned_native_timer_still_only_bounds_actual_verifier(self):
        native = Path.home() / '.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/lib/python3.12/site-packages/benchflow/rollout/_setup.py'
        if not native.exists():
            self.skipTest('Pinned native BenchFlow source not available')
        tree = ast.parse(native.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == '_verify_rollout')
        scope = {'Any': Any, 'RolloutPlanes': object, 'VerifierTimeoutDiagnostic': object,
                 'datetime': datetime, 'asyncio': asyncio, 'logger': logging.getLogger(__name__),
                 '_ensure_canonical_rewards': lambda rewards, **kwargs: rewards}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(native), 'exec'), scope)
        events = []

        async def harden(*args, **kwargs):
            events.append('original_hardening')

        async def warm(*args, **kwargs):
            events.append('prepare')
            await asyncio.sleep(0.06)  # Longer than the grader's native budget.

        async def verify():
            events.append('grade')
            await asyncio.sleep(0.001)
            return SimpleNamespace(rewards={'reward': 1})

        original = SimpleNamespace(harden_before_verify=harden,
                                   verifier=lambda **kwargs: SimpleNamespace(verify=verify))
        with tempfile.TemporaryDirectory() as d:
            paths = SimpleNamespace(rollout_dir=Path(d), verifier_dir=Path(d) / 'verifier')
            task = SimpleNamespace(name='one', config=SimpleNamespace(verifier=SimpleNamespace(timeout_sec=0.02)))
            proxy = PrecachePlanes(original, paths, {}, callback=warm)
            rewards, error, diagnostic = await scope['_verify_rollout'](None, task, paths, {}, proxy)
        self.assertEqual(rewards, {'reward': 1})
        self.assertIsNone(error)
        self.assertIsNone(diagnostic)
        self.assertEqual(events, ['original_hardening', 'prepare', 'grade'])
        self.assertEqual(task.config.verifier.timeout_sec, 0.02)


if __name__ == '__main__':
    unittest.main()
