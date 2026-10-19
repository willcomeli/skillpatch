import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skillpatch.config import load_config
from skillpatch.environment import load_environment


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_paths_resolve_relative_to_config_and_model_has_no_default(self):
        path = self.root / "config.toml"
        path.write_text('[task]\nbenchmark="skillsbench"\npath="tasks/example"\n'
                        '[agent]\nname="openhands"\nmodel="my-model"\n'
                        '[runtime]\nbench_command=["bin/bench"]\n')
        request, _ = load_config(path)
        self.assertEqual(request.task_dir, self.root / "tasks/example")
        self.assertEqual(request.bench_command, (str(self.root / "bin/bench"),))
        path.write_text(path.read_text().replace('model="my-model"\n', ''))
        with self.assertRaisesRegex(ValueError, "agent.model"):
            load_config(path)

    def test_unknown_config_option_rejected(self):
        path = self.root / "config.toml"
        path.write_text('[runtime]\ntimeot_sec=123\n')
        with self.assertRaisesRegex(ValueError, "timeot_sec"):
            load_config(path)

    def test_live_configuration_is_explicit_and_paths_are_relative(self):
        path = self.root / "config.toml"
        base = ('[task]\nbenchmark="skillsbench"\npath="tasks/example"\nexplicit_skill_read=false\n'
                '[agent]\nname="openhands"\nmodel="my-model"\n'
                '[runtime]\ndiagnosis_config="discovery.toml"\ndiagnosis_timeout_sec=30\n')
        path.write_text(base)
        with self.assertRaisesRegex(ValueError, "bench_python"):
            load_config(path)
        path.write_text(base + 'bench_python="bin/python"\n')
        request, _ = load_config(path)
        self.assertEqual(request.bench_python, self.root / "bin/python")
        self.assertEqual(request.diagnosis_config, self.root / "discovery.toml")
        self.assertEqual(request.diagnosis_timeout_sec, 30)
        self.assertFalse(request.explicit_skill_read)
        path.write_text(path.read_text().replace('explicit_skill_read=false', 'explicit_skill_read="yes"'))
        with self.assertRaisesRegex(ValueError, "boolean"):
            load_config(path)

    def test_other_backend_does_not_require_benchflow_python(self):
        path = self.root / "config.toml"
        path.write_text('[task]\nbenchmark="other"\npath="task"\n'
                        '[agent]\nname="agent"\nmodel="model"\n'
                        '[runtime]\nbackend="other"\ndiagnosis_config="diagnosis.toml"\n')
        request, _ = load_config(path)
        self.assertIsNone(request.bench_python)

    def test_venv_interpreter_symlink_is_preserved_for_process_launch(self):
        base = self.root / "base-python"
        base.write_text("fixture interpreter")
        venv = self.root / "venv/bin"
        venv.mkdir(parents=True)
        python = venv / "python"
        python.symlink_to(base)
        path = self.root / "config.toml"
        path.write_text('[task]\nbenchmark="skillsbench"\npath="task"\n'
                        '[agent]\nname="agent"\nmodel="model"\n'
                        '[runtime]\nbench_python="venv/bin/python"\n'
                        'bench_command=["venv/bin/python", "-m", "benchflow"]\n'
                        'diagnosis_config="diagnosis.toml"\n')
        request, _ = load_config(path)
        self.assertEqual(request.bench_python, python)
        self.assertEqual(request.bench_command[0], str(python))
        self.assertNotEqual(request.bench_python, python.resolve())

    def test_environment_is_literal_and_process_environment_takes_precedence(self):
        path = self.root / ".env"
        path.write_text('export LLM_API_KEY="secret # literal"\nDEEPSEEK_BASE_URL=https://api.deepseek.com # comment\n')
        with patch.dict(os.environ, {"LLM_API_KEY": "existing"}, clear=True):
            environment = load_environment(path)
        self.assertEqual(environment["LLM_API_KEY"], "existing")
        self.assertEqual(environment["DEEPSEEK_BASE_URL"], "https://api.deepseek.com")

    def test_shell_expansion_is_never_executed(self):
        path = self.root / ".env"
        path.write_text("LLM_API_KEY='$(touch should-never-exist)'\n")
        with self.assertRaisesRegex(ValueError, "expansion"):
            load_environment(path)
        self.assertFalse((self.root / "should-never-exist").exists())


if __name__ == "__main__":
    unittest.main()
