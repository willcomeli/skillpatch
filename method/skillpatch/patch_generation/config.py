"""Stage2 patch-generation settings, independent of benchmark execution."""
from dataclasses import dataclass
from pathlib import Path
import tomllib
from method.skillpatch.patch_diagnosis.config import load_discovery_config


@dataclass(frozen=True)
class Stage2Config:
    generation_config: Path


def load_stage2_config(path: Path) -> Stage2Config:
    path = path.expanduser().resolve()
    with path.open('rb') as stream:
        doc = tomllib.load(stream)
    if set(doc) != {'stage2'} or not isinstance(doc['stage2'], dict):
        raise ValueError('Expected one [stage2] table')
    values = doc['stage2']
    if set(values) != {'generation_config'}:
        raise ValueError('Stage2 requires only generation_config; execution and evaluation are separate')
    value = values['generation_config']
    if not isinstance(value, str) or not value.strip():
        raise ValueError('generation_config must be a nonempty path')
    generation_config = (path.parent / value).resolve(strict=True)
    load_discovery_config(generation_config)
    return Stage2Config(generation_config)
