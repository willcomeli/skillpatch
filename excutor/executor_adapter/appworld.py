"""AppWorld runner composition and output ownership."""

from pathlib import Path
from typing import Any

from .native import NativeBenchmarkAdapter
from ..appworld_runners.agent import AppWorldActionAgent
from ..appworld_runners.runner import AppWorldRunner


class AppWorldAdapter(NativeBenchmarkAdapter):
    benchmark = 'appworld'
    runner_type = AppWorldRunner
    agent_type = AppWorldActionAgent

    def prepare_runner_options(self, config: Any, output: Path) -> dict:
        options = super().prepare_runner_options(config, output)
        if 'output_dir' in options:
            raise ValueError('AppWorld output_dir is managed by the launcher')
        options['output_dir'] = str(output)
        return options
