"""Independent validation settings; no generation or revision loop."""
from dataclasses import dataclass
from pathlib import Path
import tomllib
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from set_up.config import load_config

@dataclass(frozen=True)
class ValidationConfig:
    evaluation_config: Path
    regression_configs: tuple[Path, ...] = ()
    repetitions: int = 1


def load_validation_config(path: Path):
    path = path.expanduser().resolve(strict=True)
    with path.open('rb') as stream:
        doc = tomllib.load(stream)
    if set(doc) != {'patch_validation'} or not isinstance(doc['patch_validation'], dict):
        raise ValueError('Expected one [patch_validation] table')
    values = dict(doc['patch_validation'])
    if 'evaluation_config' not in values or set(values) - {'evaluation_config','regression_configs','repetitions'}:
        raise ValueError('Missing or unknown patch_validation settings')
    def resolve(value):
        if not isinstance(value,str) or not value.strip():
            raise ValueError('Validation paths must be nonempty strings')
        return (path.parent/value).resolve(strict=True)
    evaluation = resolve(values['evaluation_config'])
    load_discovery_config(evaluation)
    regression = values.get('regression_configs', [])
    if not isinstance(regression,list):
        raise ValueError('regression_configs must be an array')
    regression = tuple(resolve(value) for value in regression)
    if len(set(regression)) != len(regression):
        raise ValueError('Duplicate regression configuration')
    for config in regression:
        request,_ = load_config(config)
        if request.benchmark != 'skillsbench' or request.backend != 'benchflow':
            raise ValueError('Validation currently supports only SkillsBench with BenchFlow')
    repetitions = values.get('repetitions',1)
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError('repetitions must be a positive integer')
    return ValidationConfig(evaluation,regression,repetitions)
