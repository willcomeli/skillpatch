"""Run BenchFlow with installer-only proxy and optional download routing.

Keep OpenHands commit, package pins, launch command, model and task settings.
Optionally fetch uv from its official GitHub release and use an explicit
Python package index; later execution uses the ordinary BenchFlow environment.
"""

import argparse
import os
from dataclasses import replace
import shlex
import sys
from urllib.parse import urlsplit


def install_command(command, proxy, index_url=None, uv_installer_github=False, ubuntu_install_mirror=False):
    endpoint = urlsplit(proxy)
    if endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError('Installation proxy must be an HTTP(S) endpoint without credentials')
    names = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')
    assignments = ' '.join(name + '=' + shlex.quote(proxy) for name in names)
    # This is a per-request timeout, independent of any installation deadline.
    assignments += ' UV_HTTP_TIMEOUT=300'
    if index_url:
        index = urlsplit(index_url)
        if (index.scheme != 'https' or not index.hostname or index.username or index.password
                or index.query or index.fragment):
            raise ValueError('Install index must be an HTTPS endpoint without credentials')
        assignments += ' UV_INDEX_URL=' + shlex.quote(index_url)
        assignments += ' PIP_INDEX_URL=' + shlex.quote(index_url)
    if uv_installer_github:
        command = command.replace('https://astral.sh/uv/install.sh',
            'https://github.com/astral-sh/uv/releases/latest/download/uv-installer.sh')
    # Native installer suppresses the script's own download errors. Preserve
    # them so transport failures can be retried instead of becoming opaque rc=1.
    command = command.replace('| sh >/dev/null 2>&1', '| sh')
    if ubuntu_install_mirror:
        # Preserve Ubuntu's archive keyring, suites and components. Only the
        # installer sees this temporary APT configuration; task sources stay put.
        rewrite = ('s|http://archive[.]ubuntu[.]com/ubuntu|http://mirrors.ustc.edu.cn/ubuntu|g;'
                   's|http://security[.]ubuntu[.]com/ubuntu|http://mirrors.ustc.edu.cn/ubuntu|g')
        apt_config = 'Dir::Etc::sourcelist "/tmp/benchflow-install.sources"; Dir::Etc::sourceparts "-";'
        bootstrap = ('if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then sed '
            + shlex.quote(rewrite) + ' /etc/apt/sources.list.d/ubuntu.sources > /tmp/benchflow-install.sources'
            + ' && printf %s ' + shlex.quote(apt_config) + ' > /tmp/benchflow-install-apt.conf'
            + ' && export APT_CONFIG=/tmp/benchflow-install-apt.conf; fi')
        command = bootstrap + ' && ' + command
    return f'export {assignments} NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 && {command}'


def host_proxy_environment(proxy, environment):
    """Buildx fetches registry auth on the host, outside Docker Desktop's proxy."""
    values = dict(environment)
    endpoint = urlsplit(proxy)
    if endpoint.hostname == 'host.docker.internal':
        host_proxy = endpoint._replace(netloc='127.0.0.1' + (':' + str(endpoint.port) if endpoint.port else '')).geturl()
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            values.setdefault(name, host_proxy)
    return values


def resolve_execution_environment(original, agent, model, agent_env, environment):
    """Mark the configured OpenAI-compatible endpoint as an explicit override."""
    explicit = dict(agent_env or {})
    names = ('BENCHFLOW_PROVIDER_BASE_URL', 'BENCHFLOW_PROVIDER_API_KEY')
    if (agent == 'openhands' and model and model.startswith('openai/')
            and not any(name in explicit for name in names)
            and all(environment.get(name) for name in names)):
        explicit.update({name: environment[name] for name in names})
    return original(agent, model, explicit)


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--install-proxy', required=True)
    parser.add_argument('--install-index')
    parser.add_argument('--install-timeout', type=int)
    parser.add_argument('--no-install-timeout', action='store_true')
    parser.add_argument('--uv-installer-github', action='store_true')
    parser.add_argument('--ubuntu-install-mirror', action='store_true')
    parser.add_argument('--step-retries', action='store_true')
    parser.add_argument('--apt-mirror', action='store_true')
    args, bench_args = parser.parse_known_args()
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.skillsbench.image_registry_routing import public_client_environment
    os.environ.update(public_client_environment(bench_args, os.environ))
    from scripts.skillsbench.apt_mirrors import mirror_requested
    from scripts.skillsbench.startup_policy import run_policy, configure_platform
    startup_policy, native_config = run_policy(bench_args)
    from scripts.skillsbench.step_retries import install_policy
    installation = install_policy(bench_args)
    args.no_install_timeout = args.no_install_timeout or bool(
        installation and installation.get('total_timeout_seconds', 'native') is None)
    if args.no_install_timeout and '--config' in bench_args:
        audit = Path(bench_args[bench_args.index('--config') + 1]).resolve().parent / 'installation-policy.json'
        import json
        audit.write_text(json.dumps({'total_timeout_seconds': None,
            'uv_request_timeout_seconds': 300, 'network_retry_limit': None,
            'other_request_timeouts': 'existing defaults',
            'native_agent_and_verifier_budgets': 'unchanged'}, indent=2) + '\n')
    if startup_policy:
        configure_platform(startup_policy, native_config)
    args.apt_mirror = args.apt_mirror or mirror_requested(bench_args)
    from scripts.skillsbench.python_mirrors import runtime_policy as python_runtime_policy
    runtime_python = python_runtime_policy(bench_args)
    if args.apt_mirror:
        from scripts.skillsbench.python_mirrors import INDEX
        args.install_index = args.install_index or INDEX
    if runtime_python:
        from scripts.skillsbench.python_mirrors import INDEX
        if args.install_index not in (None, INDEX, runtime_python['index_url']):
            raise ValueError('Runtime mirror policy conflicts with an explicit installation index')
        args.install_index = runtime_python['index_url']
        if '--config' in bench_args:
            import json
            audit = Path(bench_args[bench_args.index('--config') + 1]).resolve().parent
            (audit / 'runtime-python-index-policy.json').write_text(json.dumps(runtime_python, indent=2) + '\n')
    if args.install_timeout is not None and args.install_timeout <= 0:
        parser.error("--install-timeout must be positive")
    from benchflow.agents.registry import AGENTS, AGENT_INSTALLERS
    original = AGENTS['openhands']
    command = install_command(original.install_cmd, args.install_proxy,
                              args.install_index, args.uv_installer_github, args.ubuntu_install_mirror)
    if args.step_retries:
        # Direct execution of this file has scripts/skillsbench on sys.path.
        # Add the repository root before importing its process-local hooks.
        from scripts.skillsbench.step_retries import request_retry_install_command
        command += ' && ' + request_retry_install_command()
    os.environ.update(host_proxy_environment(args.install_proxy, os.environ))
    # Process-local install settings; task/agent/verifier budgets stay unchanged.
    overrides = {'install_cmd': command}
    if args.no_install_timeout:
        overrides['install_timeout'] = None
    elif args.install_timeout is not None:
        overrides['install_timeout'] = args.install_timeout
    AGENTS['openhands'] = replace(original, **overrides)
    AGENT_INSTALLERS['openhands'] = command
    from benchflow.cli.main import app
    from benchflow.agents import env as agent_environment
    from benchflow import rollout_planes
    if startup_policy and startup_policy.get('native_acp_budget'):
        from scripts.skillsbench.startup_policy import enable_native_startup_budget
        enable_native_startup_budget()
    if args.apt_mirror or runtime_python:
        from scripts.skillsbench.python_mirrors import enable_verifier_routing
        enable_verifier_routing(index=runtime_python['index_url'] if runtime_python else INDEX)
    from scripts.skillsbench.verifier_budget import requested_policy, enable as enable_verifier_budget
    verifier_budget = requested_policy(bench_args)
    if verifier_budget:
        enable_verifier_budget(verifier_budget)
    if args.step_retries:
        from scripts.skillsbench.step_retries import enable_step_retries
        enable_step_retries(apt_mirror=args.apt_mirror)
    original_resolver = agent_environment.resolve_agent_env

    def resolve_agent_env(agent, model, agent_env):
        return resolve_execution_environment(original_resolver, agent, model, agent_env, os.environ)

    agent_environment.resolve_agent_env = resolve_agent_env
    rollout_planes.resolve_agent_env = resolve_agent_env
    sys.argv = [sys.argv[0], *bench_args]
    return app()


if __name__ == '__main__':
    sys.exit(main())
