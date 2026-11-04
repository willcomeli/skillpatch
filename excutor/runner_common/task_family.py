"""Compatibility dispatch for historical cross-benchmark family helpers."""

from typing import Any, Optional

from ..alfworld_runners.task_family import (TASK_TYPE_ALIASES, normalize_task_type,
                                   normalize_alfworld_task_family, detect_alfworld_task_family)
from ..scienceworld_runners.task_family import normalize_scienceworld_task_family

def normalize_task_type_for_dataset(value: Any, dataset: Any = None) -> Optional[str]:
    key = str(dataset or "").lower()
    if key in {"scienceworld", "science_world"}:
        return normalize_scienceworld_task_family(value)
    if key == "alfworld":
        return normalize_alfworld_task_family(value)
    return normalize_task_type(value) or normalize_scienceworld_task_family(value)
