"""Approved, task-scoped dependency preparation before the native grader timer."""
import hashlib
import json
from pathlib import Path
import shlex
import time

from .step_retries import retry_operation


def requested_policy(rollout_dir, task_name):
    path = Path(rollout_dir).resolve()
    for folder in (path, *path.parents):
        marker = folder / 'VERIFIER_PRECACHE_REQUESTED.json'
        if marker.exists():
            return json.loads(marker.read_text()).get('tasks', {}).get(task_name)
    return None


def prepare_commands(policy, verifier_environment):
    pins = policy['requirements']
    if pins != ['pytest==8.4.1', 'pytest-json-ctrf==0.3.5', 'numpy==1.26.4', 'casadi==3.6.7']:
        raise ValueError('Precache requirements must match the approved original verifier')
    if policy['uv_version'] != '0.9.7':
        raise ValueError('Precache uv must match the original pinned installer')
    # Copy the hardened environment; never mutate the actual grader's settings.
    # No grader files, outputs or test cases are read or executed here.
    environment = dict(verifier_environment)
    environment.setdefault('HOME', '/root')
    environment.setdefault('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    # The native grader directory is uploaded only after this preparation pass.
    # A version probe must not inherit its --confcutdir/--rootdir flags. Keep
    # configuration and plugin isolation for this probe; grading is unchanged.
    environment['PYTEST_ADDOPTS'] = '-c /dev/null -p no:cacheprovider'
    prefix = shlex.join(['env', '-i', *[f'{name}={value}' for name, value in sorted(environment.items())],
                         '/bin/bash', '-c']) + ' '
    setup = 'set -euo pipefail; '
    uvx = '/root/.local/bin/uvx ' + ' '.join('--with ' + shlex.quote(pin) for pin in pins)
    steps = [('apt_metadata', 'apt-get update'),
             ('apt_packages', 'apt-get install -y curl libgfortran5'),
             ('uv_installer', 'curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh'),
             ('python_dependencies', uvx + ' pytest --version')]
    return [(name, prefix + shlex.quote(setup + command)) for name, command in steps]


async def precache(env, task, rollout_paths, policy):
    script = Path(task.task_dir) / 'verifier/test.sh'
    original = script.read_bytes()
    if hashlib.sha256(original).hexdigest() != policy['verifier_script_sha256']:
        raise ValueError('Original verifier script changed; refuse dependency precache')
    folder = rollout_paths.rollout_dir / 'verifier-precache'
    folder.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    records = []
    artifact = policy.get('uv_artifact')
    if artifact:
        host = Path(artifact['host_path'])
        if hashlib.sha256(host.read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError('Cached official uv archive checksum mismatch')
        arch = await env.exec('uname -m', user='root', timeout_sec=None)
        if arch.return_code or arch.stdout.strip() != artifact['architecture']:
            raise ValueError('Cached uv archive architecture mismatch')
        cache = '/var/cache/skillpatch/uv-0.9.7'
        result = await env.exec('mkdir -p ' + cache + ' && chmod 700 ' + cache,
                                user='root', timeout_sec=None)
        if result.return_code:
            raise RuntimeError('Cannot create trusted uv artifact cache')
        destination = cache + '/' + artifact['filename']
        await retry_operation(lambda: env.upload_file(host, destination), 'verifier_dependency_preparation')
        check = shlex.quote(artifact['sha256'] + '  ' + destination)
        result = await env.exec('printf "%s\\n" ' + check + ' | sha256sum -c - && chmod 444 '
                                + shlex.quote(destination), user='root', timeout_sec=None)
        if result.return_code:
            raise ValueError('Sandbox uv archive checksum mismatch')
        # The unmodified official installer supports this download base URL.
        # It still extracts/installs the exact official tar archive itself.
        task.config.verifier.env['UV_DOWNLOAD_URL'] = 'file://' + cache
    commands = prepare_commands(policy, task.config.verifier.env or {})
    for stage, command in commands:
        attempt = 0

        async def operation():
            nonlocal attempt
            attempt += 1
            result = await env.exec(command, user='root', timeout_sec=None)
            output = (result.stdout or '') + '\n' + (result.stderr or '')
            (folder / f'{stage}-{attempt:06d}.log').write_text(output)
            if result.return_code:
                raise RuntimeError(output)
            return result

        await retry_operation(operation, 'verifier_dependency_preparation')
        records.append({'stage': stage, 'attempts': attempt})
    if script.read_bytes() != original:
        raise ValueError('Verifier script changed during dependency preparation')
    (folder / 'preparation.json').write_text(json.dumps({
        'status': 'completed', 'requirements': policy['requirements'], 'uv_version': policy['uv_version'],
        'verifier_script_sha256': policy['verifier_script_sha256'],
        'native_verifier_timeout_seconds': task.config.verifier.timeout_sec,
        'uv_artifact': artifact,
        'preparation_seconds': time.monotonic() - started, 'stages': records,
        'scope': 'after original hardening and before original verifier timer; pytest --version only; no tests executed',
        'environment': 'copy of hardened verifier environment; version probe only replaces PYTEST_ADDOPTS to avoid not-yet-uploaded grader paths; actual grading environment unchanged',
    }, indent=2) + '\n')


class PrecachePlanes:
    def __init__(self, original, rollout_paths, policy, callback=precache):
        self.original = original
        self.rollout_paths = rollout_paths
        self.policy = policy
        self.callback = callback

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def harden_before_verify(self, env, task, sandbox_user, **kwargs):
        await self.original.harden_before_verify(env, task, sandbox_user, **kwargs)
        await self.callback(env, task, self.rollout_paths, self.policy)
