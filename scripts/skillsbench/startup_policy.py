"""Per-run approved platform routing and native-budget ACP startup."""
import asyncio
from functools import wraps
import json
import os
from pathlib import Path
from types import FunctionType


def run_policy(args):
    if '--config' not in args:
        return None, None
    config = Path(args[args.index('--config') + 1]).resolve()
    for folder in config.parents:
        marker = folder / 'STARTUP_POLICY_REQUESTED.json'
        if marker.exists():
            return json.loads(marker.read_text()), config
    return None, None


def configure_platform(policy, config):
    job = json.loads(config.read_text())
    names = job.get('include', [])
    platforms = {policy.get('platform_overrides', {}).get(name) for name in names}
    platforms.discard(None)
    if not platforms:
        return
    if len(names) != 1 or platforms != {'linux/amd64'}:
        raise RuntimeError('Platform override requires one approved task')
    os.environ['DOCKER_DEFAULT_PLATFORM'] = 'linux/amd64'
    audit = config.parent / 'platform-routing.json'
    audit.write_text(json.dumps({'task': names[0], 'platform': 'linux/amd64',
        'native_host_platform': 'linux/arm64', 'package_pins': 'unchanged',
        'execution': 'Docker Desktop emulation'}, indent=2) + '\n')


class StartupAsyncio:
    """Private function namespace; never mutate global asyncio.wait_for."""
    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, awaitable, timeout):
        if timeout == 60:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout)


def startup_clone(original):
    namespace = dict(original.__globals__)
    namespace['asyncio'] = StartupAsyncio()
    # Native startup closes the client on Exception. Include cancellation so
    # exhausting the enclosing native budget cannot leak a startup process.
    namespace['Exception'] = BaseException
    clone = FunctionType(original.__code__, namespace, original.__name__,
                         original.__defaults__, original.__closure__)
    clone.__kwdefaults__ = original.__kwdefaults__
    return clone


def enable_native_startup_budget():
    from importlib.metadata import version
    if version('benchflow') != '0.6.3':
        raise RuntimeError('ACP startup adaptation requires BenchFlow 0.6.3')
    from benchflow import rollout, rollout_planes
    from benchflow.acp import runtime
    from .step_retries import retry_operation
    cls = rollout.Rollout
    if getattr(cls, '_skillpatch_native_startup_budget', False):
        return
    native_startup = startup_clone(runtime.connect_acp)
    native_prompts = rollout_planes.execute_prompts

    @wraps(runtime.connect_acp)
    async def connect_acp(*args, **kwargs):
        return await retry_operation(lambda: native_startup(*args, **kwargs), 'acp_startup')

    def budgeted(original):
        @wraps(original)
        async def connect(self, *args, **kwargs):
            loop = asyncio.get_running_loop()
            role = args[0] if args else kwargs.get('role', getattr(self, '_active_role', None))
            budget = getattr(role, 'timeout_sec', None) or self._timeout
            if not hasattr(self, '_skillpatch_agent_deadline'):
                self._skillpatch_agent_deadline = loop.time() + budget
            deadline = self._skillpatch_agent_deadline
            async with asyncio.timeout_at(deadline):
                result = await original(self, *args, **kwargs)
            self._acp_client._skillpatch_agent_deadline = deadline
            return result
        return connect

    @wraps(native_prompts)
    async def prompts(acp_client, session, prompts, timeout, idle_timeout=None):
        deadline = getattr(acp_client, '_skillpatch_agent_deadline', None)
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError('Native agent budget exhausted before prompt dispatch')
            timeout = min(timeout, remaining)
        return await native_prompts(acp_client, session, prompts, timeout, idle_timeout=idle_timeout)

    cls.connect = budgeted(cls.connect)
    cls.connect_as = budgeted(cls.connect_as)
    rollout_planes.connect_acp = connect_acp
    rollout_planes.execute_prompts = prompts
    cls._skillpatch_native_startup_budget = True
