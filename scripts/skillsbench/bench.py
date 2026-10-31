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
    # Per-download timeout is independent of the 1800s total install budget.
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
    if ubuntu_install_mirror:
        # Preserve Ubuntu's archive keyring, suites and components. Only the
        # installer sees this temporary APT configuration; task sources stay put.
        rewrite = ('s|http://archive[.]ubuntu[.]com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g;'
                   's|http://security[.]ubuntu[.]com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g')
        apt_config = 'Dir::Etc::sourcelist "/tmp/benchflow-install.sources"; Dir::Etc::sourceparts "-";'
        bootstrap = ('if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then sed '
            + shlex.quote(rewrite) + ' /etc/apt/sources.list.d/ubuntu.sources > /tmp/benchflow-install.sources'
            + ' && printf %s ' + shlex.quote(apt_config) + ' > /tmp/benchflow-install-apt.conf'
            + ' && export APT_CONFIG=/tmp/benchflow-install-apt.conf; fi')
        command = bootstrap + ' && ' + command
    return f'export {assignments} NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 && {command}'


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
    parser.add_argument('--uv-installer-github', action='store_true')
    parser.add_argument('--ubuntu-install-mirror', action='store_true')
    args, bench_args = parser.parse_known_args()
    if args.install_timeout is not None and args.install_timeout <= 0:
        parser.error("--install-timeout must be positive")
    from benchflow.agents.registry import AGENTS, AGENT_INSTALLERS
    original = AGENTS['openhands']
    command = install_command(original.install_cmd, args.install_proxy,
                              args.install_index, args.uv_installer_github, args.ubuntu_install_mirror)
    # Process-local install settings; task/agent/verifier budgets stay unchanged.
    overrides = {'install_cmd': command}
    if args.install_timeout is not None:
        overrides['install_timeout'] = args.install_timeout
    AGENTS['openhands'] = replace(original, **overrides)
    AGENT_INSTALLERS['openhands'] = command
    from benchflow.cli.main import app
    from benchflow.agents import env as agent_environment
    from benchflow import rollout_planes
    original_resolver = agent_environment.resolve_agent_env

    def resolve_agent_env(agent, model, agent_env):
        return resolve_execution_environment(original_resolver, agent, model, agent_env, os.environ)

    agent_environment.resolve_agent_env = resolve_agent_env
    rollout_planes.resolve_agent_env = resolve_agent_env
    sys.argv = [sys.argv[0], *bench_args]
    return app()


if __name__ == '__main__':
    sys.exit(main())
