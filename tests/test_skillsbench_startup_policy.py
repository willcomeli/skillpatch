import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.skillsbench.startup_policy import configure_platform, run_policy, startup_clone
from scripts.skillsbench.bench import install_command


async def example_startup():
    return await asyncio.wait_for(asyncio.sleep(0, result='same-session'), timeout=60)


class StartupPolicyTests(unittest.TestCase):
    def test_native_installer_download_diagnostics_are_retained(self):
        command = install_command('curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 && next-step',
                                  'http://host.docker.internal:7897', uv_installer_github=True)
        self.assertIn('uv-installer.sh | sh && next-step', command)
        self.assertNotIn('sh >/dev/null', command)

    def test_private_clone_does_not_modify_global_asyncio(self):
        original = asyncio.wait_for
        self.assertEqual(asyncio.run(startup_clone(example_startup)()), 'same-session')
        self.assertIs(asyncio.wait_for, original)

    def test_platform_is_scoped_to_approved_single_task(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            folder = Path(temporary)
            (folder / 'STARTUP_POLICY_REQUESTED.json').write_text(json.dumps({
                'native_acp_budget': True, 'platform_overrides': {'dubbing': 'linux/amd64'}}))
            config = folder / 'task/backend/config.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'include': ['other']}))
            policy, found = run_policy(['run', '--config', str(config)])
            configure_platform(policy, found)
            self.assertNotIn('DOCKER_DEFAULT_PLATFORM', os.environ)
            config.write_text(json.dumps({'include': ['dubbing']}))
            configure_platform(policy, found)
            self.assertEqual(os.environ['DOCKER_DEFAULT_PLATFORM'], 'linux/amd64')
            config.write_text(json.dumps({'include': ['dubbing', 'other']}))
            with self.assertRaises(RuntimeError):
                configure_platform(policy, found)


if __name__ == '__main__':
    unittest.main()
