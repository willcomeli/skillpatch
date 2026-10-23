"""Select benchmark-specific offline diagnosis adapters."""

from typing import Callable

from .adapter.base import DiagnosisAdapter


_factories: dict[str, Callable[[], DiagnosisAdapter]] = {}


def register_diagnosis_adapter(name: str, factory: Callable[[], DiagnosisAdapter]) -> None:
    if not isinstance(name, str) or not name.strip() or not callable(factory):
        raise ValueError("Adapter registration needs a name and a factory")
    if name in _factories or name in {"skillsbench", "alfworld", "scienceworld", "appworld"}:
        raise ValueError(f"Adapter already registered: {name}")
    _factories[name] = factory


def get_diagnosis_adapter(name: str) -> DiagnosisAdapter:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Benchmark adapter name must be a nonempty string")
    if name in _factories:
        return _factories[name]()
    if name == "skillsbench":
        from .adapter.skillsbench import SkillsBenchDiagnosisAdapter
        return SkillsBenchDiagnosisAdapter()
    if name == "alfworld":
        from .adapter.alfworld import AlfWorldDiagnosisAdapter
        return AlfWorldDiagnosisAdapter()
    if name == "scienceworld":
        from .adapter.scienceworld import ScienceWorldDiagnosisAdapter
        return ScienceWorldDiagnosisAdapter()
    if name == "appworld":
        from .adapter.appworld import AppWorldDiagnosisAdapter
        return AppWorldDiagnosisAdapter()
    raise ValueError(f"Unsupported benchmark adapter: {name}")
