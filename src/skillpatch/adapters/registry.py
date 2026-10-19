"""Adapter selection at application boundaries, shared by CLI and workers."""

from typing import Callable

from skillpatch.adapters.base import BenchmarkAdapter


_factories: dict[str, Callable[[], BenchmarkAdapter]] = {}


def register_adapter(name: str, factory: Callable[[], BenchmarkAdapter]) -> None:
    if not isinstance(name, str) or not name.strip() or not callable(factory):
        raise ValueError("Adapter registration needs a name and a factory")
    if name in _factories or name == "skillsbench":
        raise ValueError(f"Adapter already registered: {name}")
    _factories[name] = factory


def get_adapter(name: str) -> BenchmarkAdapter:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Benchmark adapter name must be a nonempty string")
    if name in _factories:
        return _factories[name]()
    if name == "skillsbench":
        from skillpatch.adapters.skillsbench import SkillsBenchAdapter
        return SkillsBenchAdapter()
    raise ValueError(f"Unsupported benchmark adapter: {name}")
