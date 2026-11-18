import asyncio
import json
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scripts.skillsbench.python_mirrors import INDEX, bootstrap_command, record_packages, runtime_policy
from scripts.skillsbench.apt_mirrors import bootstrap_command as apt_bootstrap, rewrite_dockerfile


class PythonMirrorTests(unittest.TestCase):
    def test_huggingface_proxy_changes_only_download_layer_not_pins_or_prior_cache(self):
        source = ("FROM python:3.12-slim\nRUN pip install seisbench==0.10.2\n"
                  "RUN python - <<'PY'\nfrom huggingface_hub import snapshot_download\n"
                  "snapshot_download('public/repo', revision='fixed-commit')\nPY\n")
        normal = rewrite_dockerfile(source)
        routed = rewrite_dockerfile(source, model_download_proxy='http://host.docker.internal:7897')
        self.assertEqual(normal.splitlines()[1], routed.splitlines()[1])
        self.assertTrue(routed.endswith(source[source.index('from huggingface_hub'):]))
        self.assertIn('HTTPS_PROXY=http://host.docker.internal:7897', routed)
        self.assertEqual(rewrite_dockerfile(routed, model_download_proxy='http://host.docker.internal:7897'), routed)
    def test_build_index_only_changes_approved_task_and_preserves_package_commands(self):
        from scripts.skillsbench.apt_mirrors import apply_build_mirror
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = 'https://mirrors.ustc.edu.cn/pypi/simple/'
            (root / 'RUNTIME_PYTHON_INDEX_REQUESTED.json').write_text(json.dumps(
                {'index_url': index, 'build_tasks': ['approved']}))
            for name in ('approved', 'other'):
                context = root / name / 'environment'
                context.mkdir(parents=True)
                original = 'FROM python:3.12-slim\nRUN pip install --no-cache-dir seisbench==0.10.2\n'
                (context / 'Dockerfile').write_text(original)
                audit = root / 'audit' / name
                self.assertTrue(apply_build_mirror(context, audit))
                result = (context / 'Dockerfile').read_text()
                self.assertIn('pip install --no-cache-dir seisbench==0.10.2', result)
                self.assertEqual(index in result, name == 'approved')
                self.assertEqual(json.loads((audit / 'routing.json').read_text())['python_build_index'],
                                 index if name == 'approved' else None)
                self.assertFalse(apply_build_mirror(context, audit))
    def test_runtime_index_is_scoped_to_run_without_altering_build_bootstrap(self):
        before = rewrite_dockerfile('FROM python:3.12-slim\nRUN pip install numpy==1.26.4\n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'baseline/one/backend/config.json'
            policy = {'index_url': 'https://mirrors.ustc.edu.cn/pypi/simple/'}
            self.assertIsNone(runtime_policy(['--config', str(config)]))
            (root / 'RUNTIME_PYTHON_INDEX_REQUESTED.json').write_text(json.dumps(policy))
            self.assertEqual(runtime_policy(['--config', str(config)]), policy)
            self.assertIsNone(runtime_policy([]))
        self.assertEqual(rewrite_dockerfile('FROM python:3.12-slim\nRUN pip install numpy==1.26.4\n'), before)

    def test_runtime_index_rejects_credentials_and_insecure_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for url in ('http://example.com/simple/', 'https://user:pass@example.com/simple/',
                        'https://example.com/simple/?token=abc'):
                (root / 'RUNTIME_PYTHON_INDEX_REQUESTED.json').write_text(json.dumps({'index_url': url}))
                with self.assertRaisesRegex(ValueError, 'HTTPS without credentials'):
                    runtime_policy(['--config', str(root / 'backend/config.json')])

    def test_bootstrap_is_valid_shell_and_keeps_explicit_index_and_hash_options(self):
        command = bootstrap_command()
        subprocess.run(['bash', '-n', '-c', command], check=True)
        self.assertIn('${UV_INDEX_URL:-' + INDEX + '}', command)
        self.assertNotIn('trusted-host', command)
        self.assertNotIn('insecure', command)
        source = 'FROM python:3.12-slim\nRUN uv pip install --require-hashes -r req.txt\n'
        mirrored = rewrite_dockerfile(source)
        self.assertIn('uv pip install --require-hashes -r req.txt', mirrored)
        self.assertEqual(rewrite_dockerfile(mirrored), mirrored)

    def test_upgrade_existing_apt_context_including_json_run(self):
        shell = 'FROM ubuntu:24.04\nRUN ' + apt_bootstrap() + '; apt-get update\n'
        exec_form = 'FROM ubuntu:24.04\nRUN ' + json.dumps(['/bin/sh', '-c', apt_bootstrap() + '; exec true']) + '\n'
        for source in (shell, exec_form):
            upgraded = rewrite_dockerfile(source)
            self.assertIn('SKILLPATCH_PYPI_MIRROR_V1', upgraded)
            self.assertEqual(rewrite_dockerfile(upgraded), upgraded)

    def test_inventory_failure_is_observational_and_cache_is_labeled(self):
        env = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(return_code=1)))
        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(record_packages(env, temporary, 'after-verifier'))
            self.assertEqual(json.loads((Path(temporary) / 'after-verifier.python-inventory-unavailable.json').read_text())['status'], 'unavailable')
        from scripts.skillsbench.python_mirrors import INVENTORY_SCRIPT
        data = json.loads(subprocess.check_output(['python3', '-I', '-S', '-c', INVENTORY_SCRIPT], text=True))
        self.assertIn('does not establish', data['cache_note'])

    def test_inventory_includes_ubuntu_local_pip_distribution_versions(self):
        from scripts.skillsbench.python_mirrors import INVENTORY_SCRIPT
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            metadata = folder / 'numpy-1.26.4.dist-info/METADATA'
            metadata.parent.mkdir()
            metadata.write_text('Name: numpy\nVersion: 1.26.4\n')
            def paths(pattern):
                return [str(folder)] if pattern == '/usr/local/lib/python*/dist-packages' else []
            output = io.StringIO()
            with patch('glob.glob', side_effect=paths), contextlib.redirect_stdout(output):
                exec(INVENTORY_SCRIPT, {})
            distributions = json.loads(output.getvalue())['distributions']
            self.assertEqual([(row['name'], row['version'], row['scope']) for row in distributions],
                             [('numpy', '1.26.4', 'system')])


if __name__ == '__main__':
    unittest.main()
