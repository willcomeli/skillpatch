"""ScienceWorld runner and action-agent composition."""

from .native import NativeBenchmarkAdapter
from ..scienceworld_runners.agent import ScienceWorldActionAgent
from ..scienceworld_runners.runner import ScienceWorldRunner


class ScienceWorldAdapter(NativeBenchmarkAdapter):
    benchmark = 'scienceworld'
    runner_type = ScienceWorldRunner
    agent_type = ScienceWorldActionAgent
