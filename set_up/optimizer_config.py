"""Resolve an optimization model independently of the frozen task executor."""

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

UNCONFIGURED_URL = 'https://optimizer-not-configured.invalid/v1'


@dataclass(frozen=True)
class OptimizerConfig:
    model: str
    api_key_env: str
    base_url_env: str
    base_url: str
    missing_environment: tuple[str, ...] = ()
    harness: str = 'openhands'

    def metadata(self):
        return {**asdict(self), 'ready': not self.missing_environment}


def resolve_optimizer(path, native, environment, *, require_credentials=False):
    """Without an explicit profile, preserve the historical same-model API."""
    if path is None:
        values = {'model': native.model, 'api_key_env': native.api_key_env,
                  'base_url_env': native.base_url_env, 'base_url': native.base_url}
    else:
        with Path(path).expanduser().resolve(strict=True).open('rb') as stream:
            document = tomllib.load(stream)
        if set(document) != {'optimizer'} or not isinstance(document['optimizer'], dict):
            raise ValueError('Optimizer configuration requires one [optimizer] table')
        values = document['optimizer']
        if (set(values) - {'model', 'model_env', 'api_key_env', 'base_url_env', 'base_url', 'harness', 'harness_env'}
                or not {'api_key_env', 'base_url_env'} <= set(values)
                or ('model' in values) == ('model_env' in values)):
            raise ValueError('Missing or unknown optimizer configuration fields')
        values = dict(values)
        if 'model_env' in values:
            name = values['model_env']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                raise ValueError('optimizer.model_env must name an environment variable')
            values['model'] = environment.get(name)
            if not isinstance(values['model'], str) or not values['model'].strip():
                raise ValueError(f'Missing method model environment variable: {name}')
    if 'harness' in values and 'harness_env' in values:
        raise ValueError('Use optimizer.harness or optimizer.harness_env, not both')
    harness = values.get('harness', 'openhands')
    if 'harness_env' in values:
        name = values['harness_env']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError('optimizer.harness_env must name an environment variable')
        harness = environment.get(name) or 'openhands'
    if harness not in {'openhands', 'codex'}:
        raise ValueError('SkillPatch harness must be openhands or codex')
    for field in ('model', 'api_key_env', 'base_url_env'):
        if not isinstance(values.get(field), str) or not values[field].strip():
            raise ValueError(f'optimizer.{field} must be nonempty text')
    for field in ('api_key_env', 'base_url_env'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', values[field]):
            raise ValueError(f'optimizer.{field} must name an environment variable')
    explicit = values.get('base_url')
    if explicit is not None and not isinstance(explicit, str):
        raise ValueError('optimizer.base_url must be text')
    endpoint = (explicit or environment.get(values['base_url_env']) or '').strip()
    missing = []
    if not endpoint:
        missing.append(values['base_url_env'])
    else:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {'http', 'https'} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(c.isspace() for c in endpoint)):
            raise ValueError('Optimizer URL must be an HTTP(S) API root without credentials, query or fragment')
    if not environment.get(values['api_key_env']):
        missing.append(values['api_key_env'])
    if require_credentials and missing:
        raise ValueError('Missing optimizer environment variables: ' + ', '.join(missing))
    if (endpoint and values['base_url_env'] == native.base_url_env
            and endpoint != (native.base_url or environment.get(native.base_url_env))):
        raise ValueError('Different executor and optimizer URLs require different base_url_env names')
    return OptimizerConfig(values['model'], values['api_key_env'], values['base_url_env'],
                           endpoint or UNCONFIGURED_URL, tuple(missing), harness)


def method_optimizer(configs, native, environment):
    """Read the immutable model snapshot used by all three method adapters."""
    import json
    if 'optimizer' not in configs:
        config = resolve_optimizer(None, native, environment, require_credentials=True)
    else:
        raw = json.loads(Path(configs['optimizer']).read_text(encoding='utf-8'))
        raw.pop('ready', None)
        raw['missing_environment'] = tuple(raw.get('missing_environment', ()))
        config = OptimizerConfig(**raw)
        if config.missing_environment or not environment.get(config.api_key_env):
            raise ValueError('Optimizer is not configured; fill its API key and URL before running methods')
    return config, {**environment, config.base_url_env: config.base_url}
