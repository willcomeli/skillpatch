"""ALFWorld runner and action-agent composition."""

from .native import NativeBenchmarkAdapter
from ..alfworld_runners.agent import AlfWorldActionAgent
from ..alfworld_runners.runner import AlfWorldRunner


class AlfWorldAdapter(NativeBenchmarkAdapter):
    benchmark = 'alfworld'
    runner_type = AlfWorldRunner
    agent_type = AlfWorldActionAgent
