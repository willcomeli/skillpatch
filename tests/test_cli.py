"""Shared CLI integration without external agents or model calls."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from set_up.cli import main
from set_up.config import load_config


class CliTests(unittest.TestCase):
    def test_prepare_connects_config_adapter_executor_and_prompt_without_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / 'task'
            for name, content in {
                'task.md': 'Summarize the input.',
                'environment/Dockerfile': 'FROM synthetic-only',
                'environment/skills/example/SKILL.md': '---\nname: example\n---\nRead inputs.',
                'verifier/test.sh': '# synthetic verifier, do not execute',
            }.items():
                path = task / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            config = root / 'run.toml'
            config.write_text('[task]\nbenchmark="skillsbench"\npath="task"\n'
                              '[agent]\nname="openhands"\nmodel="fixture"\n')
            output = root / 'output'
            with patch('set_up.cli.BenchFlowBackend.run') as run, redirect_stdout(io.StringIO()):
                status = main(['prepare', '--config', str(config), '--output', str(output)])
            self.assertEqual(status, 0)
            run.assert_not_called()
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['status'], 'prepared')
            self.assertNotIn('diagnosis', result)
            self.assertTrue((output / 'input/manifest.json').is_file())
            self.assertIn('`example`', (output / 'prompt/effective.md').read_text())
            self.assertEqual(load_config(config)[1], (root / '../runs').resolve())

    def test_diagnosis_setting_rejected_in_execution_only_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'run.toml'
            config.write_text('[runtime]\ndiagnosis_config="discovery.toml"\n')
            with self.assertRaisesRegex(ValueError, 'Unknown runtime options'):
                load_config(config)
