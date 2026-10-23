"""SkillOpt result and execution adapters."""

from .results import export_results


def create_adapter(*args, **kwargs):
    from .adapter import SharedExecutionAdapter
    return SharedExecutionAdapter(*args, **kwargs)
