"""Benchmark dispatch and compatibility checks without environment dependencies."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from excutor.executor_adapter.registry import get_native_adapter


class NativeAdapterTests(unittest.TestCase):
    def test_benchmark_imports_are_independent(self):
        for benchmark in ('appworld', 'alfworld', 'scienceworld'):
            with self.subTest(benchmark=benchmark):
                script = (
                    'import sys, json; '
                    'from excutor.executor_adapter.registry import get_native_adapter; '
                    f'adapter = get_native_adapter({benchmark!r}); '
                    'print(json.dumps(list(sys.modules)))'
                )
                result = subprocess.run([sys.executable, '-c', script], check=True,
                                        capture_output=True, text=True, cwd=Path(__file__).parents[1])
                modules = json.loads(result.stdout)
                self.assertIn(f'excutor.{benchmark}_runners.runner', modules)
                for other in {'appworld', 'alfworld', 'scienceworld'} - {benchmark}:
                    self.assertFalse(any(name.startswith(f'excutor.{other}_runners') or
                                         name == f'excutor.executor_adapter.{other}'
                                         for name in modules))

    def test_appworld_owns_output_without_mutating_config(self):
        adapter = get_native_adapter('appworld')
        options = {'data_dir': '/tmp/appworld-data', 'environment_url': 'http://localhost:8841'}
        config = SimpleNamespace(benchmark='appworld', runner_options=options.copy())
        prepared = adapter.runner_arguments(config, Path('/tmp/run'))
        self.assertEqual(prepared['output_dir'], '/tmp/run')
        self.assertEqual(config.runner_options, options)
        config.runner_options['output_dir'] = '/tmp/other'
        with self.assertRaisesRegex(ValueError, 'output_dir is managed'):
            adapter.runner_arguments(config, Path('/tmp/run'))

    def test_bad_options_and_wrong_benchmark_fail_before_runner_start(self):
        for benchmark in ('appworld', 'alfworld', 'scienceworld'):
            with self.subTest(benchmark=benchmark):
                adapter = get_native_adapter(benchmark)
                config = SimpleNamespace(benchmark='skillsbench', runner_options={})
                with self.assertRaisesRegex(ValueError, 'cannot run'):
                    adapter.create_runner(config, Path('/tmp/run'), Mock())
                config.benchmark = benchmark
                config.runner_options = {'unknown_option': 42}
                with self.assertRaisesRegex(ValueError, 'runner_options'):
                    adapter.create_runner(config, Path('/tmp/run'), Mock())

    def test_unknown_native_benchmark_is_rejected(self):
        for name in ('skillsbench', 'eviskill', '', None):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsupported native'):
                get_native_adapter(name)
