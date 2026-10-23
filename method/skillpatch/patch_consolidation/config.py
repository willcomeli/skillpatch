"""Settings for the consolidation proposal agent."""
from dataclasses import dataclass
from pathlib import Path
import tomllib
from method.skillpatch.patch_diagnosis.config import load_discovery_config

@dataclass(frozen=True)
class ConsolidationConfig:
    consolidation_config: Path


def load_consolidation_config(path: Path) -> ConsolidationConfig:
    path = path.expanduser().resolve(strict=True)
    with path.open('rb') as stream:
        doc = tomllib.load(stream)
    if set(doc) != {'stage3'} or not isinstance(doc['stage3'], dict) or set(doc['stage3']) != {'consolidation_config'}:
        raise ValueError('Expected [stage3] with only consolidation_config')
    value = doc['stage3']['consolidation_config']
    if not isinstance(value, str) or not value.strip():
        raise ValueError('consolidation_config must be a nonempty path')
    config = (path.parent / value).resolve(strict=True)
    load_discovery_config(config)
    return ConsolidationConfig(config)
