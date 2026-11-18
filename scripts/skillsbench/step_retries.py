"""Process-local recovery hooks for the existing BenchFlow backend."""

import asyncio
import base64
from contextlib import contextmanager
from functools import wraps
import json
import logging
from pathlib import Path
import re
import shlex
import shutil
import time
from uuid import uuid4

from .retry_policy import retry_delay, transient_network_failure

logger = logging.getLogger('skillpatch.step_retry')


def install_policy(args):
    if '--config' not in args:
        return None
    config = Path(args[args.index('--config') + 1]).resolve()
    for folder in config.parents:
        marker = folder / 'INSTALL_POLICY_REQUESTED.json'
        if marker.is_file():
            return json.loads(marker.read_text())
    return None


@contextmanager
def method_request_retries():
    """Scope GPT recovery to SDK HTTP requests inside one method job."""
    from openai._base_client import SyncAPIClient, AsyncAPIClient
    native_sync, native_async = SyncAPIClient.request, AsyncAPIClient.request

    @wraps(native_sync)
    def request(self, *args, **kwargs):
        attempt = 0
        while True:
            try:
                return native_sync(self, *args, **kwargs)
            except Exception as exc:
                if not transient_network_failure(exc):
                    raise
                attempt += 1
                logger.warning('Retrying the same method model HTTP request; attempt=%s; retry limit=unlimited', attempt)
                time.sleep(retry_delay(attempt))

    @wraps(native_async)
    async def async_request(self, *args, **kwargs):
        attempt = 0
        while True:
            try:
                return await native_async(self, *args, **kwargs)
            except Exception as exc:
                if not transient_network_failure(exc):
                    raise
                attempt += 1
                logger.warning('Retrying the same async method model HTTP request; attempt=%s; retry limit=unlimited', attempt)
                await asyncio.sleep(retry_delay(attempt))

    SyncAPIClient.request, AsyncAPIClient.request = request, async_request
    try:
        yield
    finally:
        SyncAPIClient.request, AsyncAPIClient.request = native_sync, native_async


def request_retry_install_command():
    root = Path(__file__).parent
    files = {'skillpatch_network_retry_policy.py': 'retry_policy.py',
             'skillpatch_openhands_request_retry.py': 'openhands_request_retry.py'}
    payload = {target: base64.b64encode((root / source).read_bytes()).decode()
               for target, source in files.items()}
    startup = 'import skillpatch_openhands_request_retry; skillpatch_openhands_request_retry.install_or_exit()\n'
    code = (
        'import base64,pathlib,sysconfig; from importlib.metadata import version; '
        'assert version("openhands-sdk")=="1.22.1", "Unexpected OpenHands SDK version"; '
        'p=pathlib.Path(sysconfig.get_path("purelib")); '
        f'files={payload!r}; '
        '[ (p/name).write_bytes(base64.b64decode(data)) for name,data in files.items() ]; '
        f'(p/"skillpatch_request_retry.pth").write_text({startup!r})'
    )
    check = ('from openhands.sdk.llm.utils.retry_mixin import RetryMixin; '
             'assert RetryMixin._skillpatch_step_retries')
    python = '"$(uv tool dir)/openhands/bin/python"'
    return f'{python} -c {shlex.quote(code)} && {python} -c {shlex.quote(check)}'


def guarded_install(command, timeout, identity):
    """Serialize attempts and remember success after a lost exec response.

    timeout runs *inside* the container, killing its process group before a
    new attempt can acquire the lock. The Docker exec client's timeout alone
    would leave an installer running and permit duplicate apt/uv processes.
    """
    directory = '/tmp/benchflow-install-retry-' + identity
    work = shlex.quote(directory)
    invoke = f'bash -c {shlex.quote(command)}'
    if timeout is not None:
        invoke = f'timeout --verbose --signal=TERM --kill-after=5s {float(timeout):g}s {invoke}'
    body = (f'if [ -f {work}/done ]; then cat {work}/output.log; exit 0; fi; '
            f'{invoke} >{work}/output.log 2>&1; result=$?; '
            f'cat {work}/output.log; '
            f'if [ "$result" = 0 ]; then touch {work}/done; fi; exit "$result"')
    return (f'mkdir -p {work} && chmod 700 {work} && '
            f'flock --exclusive {work}/lock bash -c {shlex.quote(body)}')


def retryable_build_store_failure(error):
    """Recover transient Docker content-store errors during preparation."""
    text = str(error)
    if re.search(r'no space left|permission denied|401\s+unauthorized|403\s+forbidden|certificate', text, re.I):
        return False
    if ('Unavailable: connection error' in text and
            'transport: Error while dialing: only one connection allowed' in text):
        return True
    if (('failed to compute cache key' in text or 'unable to lease content' in text) and
            'lease does not exist: not found' in text):
        return True
    return all(part in text for part in (
        'failed commit on ref', 'commit failed: rename',
        '/io.containerd.content.v1.content/ingest/',
        '/io.containerd.content.v1.content/blobs/sha256/', 'no such file or directory'))


async def retry_operation(operation, stage, *, record=None):
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            removal_race = (stage == 'docker_startup' and
                'container is marked for removal and cannot be started' in str(exc).lower())
            if not (transient_network_failure(exc) or removal_race or (
                    stage in {'docker_build', 'docker_startup'} and retryable_build_store_failure(exc))):
                raise
            if record:
                record(attempt, exc)
            logger.warning('Transient failure in %s; retrying this step in place; attempt=%s; retry limit=unlimited',
                           stage, attempt)
            await asyncio.sleep(retry_delay(attempt))


class InstallEnvironment:
    def __init__(self, env, install_cmd):
        self.env = env
        self.install_cmd = install_cmd
        self.identity = uuid4().hex

    def __getattr__(self, name):
        return getattr(self.env, name)

    async def exec(self, command, **kwargs):
        if command != self.install_cmd:
            return await self.env.exec(command, **kwargs)
        timeout = kwargs.get('timeout_sec')
        guarded = guarded_install(command, timeout, self.identity)
        # This grace is for terminating the container-side process tree and
        # transferring its logs. It does not extend any agent/verifier budget.
        try:
            result = await self.env.exec(guarded, **{**kwargs, 'timeout_sec': timeout + 15 if timeout else None})
        except RuntimeError as exc:
            if str(exc).startswith('Command timed out after '):
                raise TimeoutError('Installation exec response timed out; locked installer will be checked in place') from exc
            raise
        if result.return_code in {124, 137} and 'timeout: sending signal' in (result.stdout or '') + (result.stderr or ''):
            raise TimeoutError('Agent installation attempt timed out; previous process group terminated')
        return result


def install_step_wrapper(original, installers, *, apt_mirror=False):
    @wraps(original)
    async def install_agent(env, agent, rollout_dir, sandbox_setup_timeout=120):
        base = agent.split()[0]
        if base != 'openhands' or base not in installers:
            return await original(env, agent, rollout_dir, sandbox_setup_timeout=sandbox_setup_timeout)
        proxy = InstallEnvironment(env, installers[base])
        output = Path(rollout_dir) / 'agent'
        if apt_mirror:
            from .apt_mirrors import route_environment
            await route_environment(env, Path(rollout_dir) / 'mirror-routing', 'before-agent-install')

        async def operation():
            try:
                return await original(proxy, agent, rollout_dir, sandbox_setup_timeout=sandbox_setup_timeout)
            except Exception as exc:
                # AgentInstallError stores installer stderr/stdout separately
                # from str(error). Use it to classify the failed step only.
                if getattr(exc, 'stdout', None) and transient_network_failure(exc.stdout):
                    raise ConnectionError('Agent package installation encountered a transient network failure') from exc
                raise

        def record(attempt, exc):
            output.mkdir(parents=True, exist_ok=True)
            attempts = output / 'install-attempts'
            attempts.mkdir(exist_ok=True)
            log = output / 'install-stdout.txt'
            if log.exists():
                shutil.copyfile(log, attempts / f'{attempt:06d}.log')
            with (attempts / 'retries.jsonl').open('a') as stream:
                stream.write(json.dumps({'attempt': attempt, 'stage': 'agent_install',
                                         'error_type': type(exc).__name__}) + '\n')

        result = await retry_operation(operation, 'agent_install', record=record)
        if apt_mirror:
            from .apt_mirrors import record_packages
            await record_packages(env, Path(rollout_dir) / 'mirror-routing', 'after-agent-install')
        return result
    return install_agent


def enable_step_retries(*, apt_mirror=False):
    from benchflow.sandbox.docker import DockerSandbox
    from benchflow.agents import install as agent_install
    from benchflow.agents.registry import AGENT_INSTALLERS
    from benchflow import rollout_planes
    if getattr(DockerSandbox, '_skillpatch_step_retries', False):
        return
    native_build = DockerSandbox._run_docker_compose_build
    native_up = DockerSandbox._run_docker_compose_up

    async def build(self):
        async def operation():
            if self.rollout_paths is not None:
                from .preparation_compat import apply_compatibility
                apply_compatibility(self.environment_dir,
                    self.rollout_paths.rollout_dir / 'preparation-compatibility')
            if apt_mirror:
                from .apt_mirrors import apply_build_mirror
                output = self.rollout_paths.rollout_dir / 'mirror-routing'
                apply_build_mirror(self.environment_dir, output)
            return await native_build(self)

        def record(attempt, exc):
            paths = self.rollout_paths
            if paths is not None:
                folder = paths.rollout_dir / 'build-attempts'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f'{attempt:06d}.log').write_text(str(exc))

        return await retry_operation(operation, 'docker_build', record=record)

    DockerSandbox._run_docker_compose_build = build
    async def up(self):
        # Compose up may itself build a missing image. This remains sandbox
        # preparation, before any Agent session exists; repeating up reuses
        # any container already created by the same compose project.
        async def operation():
            if self.rollout_paths is not None:
                from .preparation_compat import apply_compatibility
                apply_compatibility(self.environment_dir,
                    self.rollout_paths.rollout_dir / 'preparation-compatibility')
            if apt_mirror:
                from .apt_mirrors import apply_build_mirror
                apply_build_mirror(self.environment_dir, self.rollout_paths.rollout_dir / 'mirror-routing')
            return await native_up(self)

        def record(attempt, exc):
            if self.rollout_paths is not None:
                folder = self.rollout_paths.rollout_dir / 'startup-attempts'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f'{attempt:06d}.log').write_text(str(exc))

        return await retry_operation(operation, 'docker_startup', record=record)

    DockerSandbox._run_docker_compose_up = up
    agent_install.install_agent = install_step_wrapper(agent_install.install_agent, AGENT_INSTALLERS, apt_mirror=apt_mirror)
    rollout_planes.install_agent = agent_install.install_agent
    DockerSandbox._skillpatch_step_retries = True
