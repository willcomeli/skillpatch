"""Explicit model, repetition and revision budgets for Stage 2."""

import tomllib
from pathlib import Path

from skillpatch.model_client import load_diagnosis_config
from skillpatch.patch_models import Stage2Config


def load_stage2_config(path: Path) -> Stage2Config:
    path = path.resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"stage2"} or not isinstance(data["stage2"], dict):
        raise ValueError("Stage 2 config must contain exactly one [stage2] table")
    values = data["stage2"]
    required = {"generation_config", "evaluation_config"}
    if not required <= values.keys() or values.keys() - (required | {
            "regression_configs", "repetitions", "max_revisions", "model_session_timeout_sec",
            "generation_session_timeout_sec"}):
        raise ValueError("Missing or unknown Stage 2 options")
    def resolve(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Stage 2 paths must be nonempty strings")
        return (path.parent / value).resolve()
    regressions = values.get("regression_configs", [])
    if not isinstance(regressions, list):
        raise ValueError("regression_configs must be an array")
    paths = tuple(resolve(value) for value in regressions)
    if len(paths) != len(set(paths)):
        raise ValueError("Regression configs must be distinct")
    repetitions = values.get("repetitions", 1)
    revisions = values.get("max_revisions", 2)
    session_timeout = values.get("model_session_timeout_sec", 900)
    generation_timeout = values.get("generation_session_timeout_sec")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if type(revisions) is not int or not 0 <= revisions <= 2:
        raise ValueError("max_revisions must be between zero and two")
    if type(session_timeout) is not int or session_timeout <= 0:
        raise ValueError("model_session_timeout_sec must be a positive integer")
    if generation_timeout is not None and (type(generation_timeout) is not int or generation_timeout <= 0):
        raise ValueError("generation_session_timeout_sec must be a positive integer")
    config = Stage2Config(resolve(values["generation_config"]), resolve(values["evaluation_config"]),
                          paths, repetitions, revisions, session_timeout, generation_timeout)
    load_diagnosis_config(config.generation_config)
    load_diagnosis_config(config.evaluation_config)
    return config
