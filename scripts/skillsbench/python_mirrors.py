"""Approved Python download routing, with read-only distribution inventories."""

import json
from pathlib import Path
import shlex
from urllib.parse import urlsplit

INDEX = 'https://pypi.org/simple'
MARKER = 'SKILLPATCH_PYPI_MIRROR_V1'


def runtime_policy(arguments):
    """Opt-in routing for installer/verifier; build commands stay cache-identical."""
    if '--config' not in arguments:
        return None
    config = Path(arguments[arguments.index('--config') + 1]).resolve()
    return policy_for_directory(config.parent)


def policy_for_directory(directory):
    path = Path(directory).resolve()
    for folder in (path, *path.parents):
        marker = folder / 'RUNTIME_PYTHON_INDEX_REQUESTED.json'
        if marker.is_file():
            policy = json.loads(marker.read_text())
            endpoint = urlsplit(policy['index_url'])
            if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
                    or endpoint.password or endpoint.query or endpoint.fragment):
                raise ValueError('Runtime Python index must be HTTPS without credentials')
            return policy
    return None


def bootstrap_command(index=INDEX):
    # Do not replace task-owned configs or explicit command-line indexes.
    uv = 'index-url = ' + json.dumps(index)
    return (f': {MARKER}; '
            f'export UV_INDEX_URL="${{UV_INDEX_URL:-{index}}}" '
            f'PIP_INDEX_URL="${{PIP_INDEX_URL:-{index}}}"; '
            'mkdir -p /etc/uv; '
            f'if [ ! -e /etc/uv/uv.toml ]; then printf "%s\\n" {shlex.quote(uv)} > /etc/uv/uv.toml; fi; '
            f'if [ ! -e /etc/pip.conf ]; then printf "%s\\n" "[global]" {shlex.quote("index-url = " + index)} > /etc/pip.conf; fi')


def build_bootstrap_command(index=INDEX):
    # Shell exports cover pip/uv in this RUN; persisted system configuration
    # also covers later native verifier invocations. No pins/hashes are edited.
    return bootstrap_command(index=index)


async def route_environment(env, audit_dir, phase):
    folder = Path(audit_dir)
    folder.mkdir(parents=True, exist_ok=True)
    result = await env.exec(bootstrap_command(), timeout_sec=30, user='root')
    if result.return_code:
        raise RuntimeError('Python mirror configuration failed')
    (folder / (phase + '.python-routing.json')).write_text(json.dumps({
        'default_index': INDEX, 'tls_verification': 'unchanged',
        'package_pins_and_hashes': 'unchanged',
        'existing_system_configs': 'retained when present',
        'explicit_task_index_overrides': 'retained',
    }, indent=2) + '\n')


INVENTORY_SCRIPT = r'''
import email.parser, glob, json
from pathlib import Path
patterns = {
    'system': ['/usr/local/lib/python*/site-packages', '/usr/local/lib/python*/dist-packages',
               '/usr/lib/python*/site-packages', '/usr/lib/python*/dist-packages'],
    'project-environment': ['/opt/venv/lib/python*/site-packages',
                            '/app/.venv/lib/python*/site-packages',
                            '/workspace/.venv/lib/python*/site-packages'],
    'uv-tools': ['/root/.local/share/uv/tools/*/lib/python*/site-packages'],
    'uv-cache-not-proof-of-use': ['/root/.cache/uv/archive-v*/*'],
}
rows = []
for scope, paths in patterns.items():
    for pattern in paths:
        for folder in glob.glob(pattern):
            for meta in Path(folder).glob('*.dist-info/METADATA'):
                try:
                    data = email.parser.Parser().parsestr(meta.read_text(errors='replace'))
                    rows.append({'name': data.get('Name'), 'version': data.get('Version'),
                                 'scope': scope, 'metadata_path': str(meta)})
                except OSError:
                    pass
print(json.dumps({'distributions': sorted(rows, key=lambda row: (row['scope'], row['name'] or '', row['metadata_path'])),
                  'cache_note': 'Cache presence alone does not establish verifier use.'}))
'''


async def record_packages(env, audit_dir, phase):
    folder = Path(audit_dir)
    folder.mkdir(parents=True, exist_ok=True)
    command = ('inventory_python=$(command -v python3 || command -v python) && '
               '"$inventory_python" -I -S -c ' + shlex.quote(INVENTORY_SCRIPT))
    try:
        result = await env.exec(command, timeout_sec=30, user='root')
        if result.return_code:
            raise RuntimeError('Python inventory unavailable')
        data = json.loads(result.stdout)
        (folder / (phase + '.python-packages.json')).write_text(json.dumps(data, indent=2) + '\n')
    except Exception as exc:
        # Inventory cannot change the native benchmark outcome.
        (folder / (phase + '.python-inventory-unavailable.json')).write_text(json.dumps({
            'status': 'unavailable', 'error_type': type(exc).__name__}) + '\n')


def enable_verifier_routing(index=INDEX):
    from benchflow.sandbox import lockdown
    from benchflow import rollout
    from benchflow.rollout import _setup
    lockdown.VERIFIER_ENV.setdefault('UV_INDEX_URL', index)
    lockdown.VERIFIER_ENV.setdefault('PIP_INDEX_URL', index)
    if getattr(_setup, '_skillpatch_python_mirror', False):
        return
    original = _setup._verify_rollout

    async def verify(env, task, rollout_paths, timing, planes, sandbox_user=None, workspace=None):
        # The original function owns asyncio.wait_for and the native budget.
        # Inventory runs after that function has finalized scoring/timing.
        from .verifier_precache import requested_policy, PrecachePlanes
        policy = requested_policy(rollout_paths.rollout_dir, task.name)
        selected = PrecachePlanes(planes, rollout_paths, policy) if policy else planes
        from .verifier_index_routing import requested_policy as index_policy, RoutedPlanes
        routing = index_policy(rollout_paths.rollout_dir, task.name)
        if routing:
            selected = RoutedPlanes(selected, rollout_paths, routing)
        result = await original(env, task, rollout_paths, timing, selected,
                                sandbox_user=sandbox_user, workspace=workspace)
        await record_packages(env, rollout_paths.rollout_dir / 'mirror-routing', 'after-verifier')
        return result

    _setup._verify_rollout = rollout._verify_rollout = verify
    _setup._skillpatch_python_mirror = True
