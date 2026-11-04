"""Composition contract for benchmarks whose runners own environment evaluation."""

import inspect
from pathlib import Path
from typing import Any


class NativeBenchmarkAdapter:
    """Bind one benchmark's runner and action agent to the shared launcher.

    Native runners return their official environment feedback from run_task().
    They use this contract rather than the prepare/evaluate ExecutionAdapter
    contract used by external execution backends.
    """

    benchmark: str
    runner_type: type
    agent_type: type

    def prepare_runner_options(self, config: Any, output: Path) -> dict:
        return dict(config.runner_options)

    def runner_arguments(self, config: Any, output: Path) -> dict:
        if config.benchmark != self.benchmark:
            raise ValueError(f'{self.benchmark} adapter cannot run {config.benchmark}')
        options = self.prepare_runner_options(config, output)
        try:
            inspect.signature(self.runner_type).bind(object(), **options)
        except TypeError as error:
            raise ValueError(f'Invalid {self.benchmark} runner_options: {error}') from None
        return options

    def create_runner(self, config: Any, output: Path, client: Any):
        options = self.runner_arguments(config, output)
        return self.runner_type(self.agent_type(client), **options)
