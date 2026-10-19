"""Explicit budgets for one local patch-set consolidation episode."""

from dataclasses import dataclass
from pathlib import Path
import tomllib

from skillpatch.model_client import load_diagnosis_config


@dataclass(frozen=True)
class Stage3Config:
    consolidation_config: Path
    evaluation_config: Path
    repair_evaluation_config: Path
    regression_configs: tuple[Path, ...] = ()
    repetitions: int = 1
    model_session_timeout_sec: int = 900
    max_validation_cases: int = 20


def load_stage3_config(path: Path) -> Stage3Config:
    path = path.resolve(strict=True)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"stage3"} or not isinstance(data["stage3"], dict):
        raise ValueError("Stage3 config requires one [stage3] table")
    values = dict(data["stage3"])
    required = {"consolidation_config", "evaluation_config", "repair_evaluation_config"}
    if not required <= values.keys() or values.keys() - (required | {
            "regression_configs", "repetitions", "model_session_timeout_sec", "max_validation_cases"}):
        raise ValueError("Missing or unknown Stage3 options")
    for key in required:
        if not isinstance(values[key], str) or not values[key].strip():
            raise ValueError("Stage3 model config paths must be nonempty strings")
        values[key] = (path.parent / values[key]).resolve(strict=True)
        load_diagnosis_config(values[key])
    regressions = values.get("regression_configs", [])
    if not isinstance(regressions, list) or any(not isinstance(p, str) or not p.strip() for p in regressions):
        raise ValueError("regression_configs must contain paths")
    values["regression_configs"] = tuple((path.parent / p).resolve(strict=True) for p in regressions)
    if len(set(values["regression_configs"])) != len(regressions):
        raise ValueError("Duplicate regression config")
    for key in ("repetitions", "model_session_timeout_sec", "max_validation_cases"):
        if key in values and (type(values[key]) is not int or values[key] < 1):
            raise ValueError(key + " must be a positive integer")
    return Stage3Config(**values)
