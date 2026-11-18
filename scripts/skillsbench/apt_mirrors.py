"""Route only official APT download URLs; retain suites, keys and verification."""

import hashlib
import json
from pathlib import Path
import re
import shlex
from urllib.parse import urlsplit

MIRRORS = (
    (r'https?://(?:[A-Za-z0-9-]+\.)?archive\.ubuntu\.com/ubuntu', 'https://mirrors.aliyun.com/ubuntu'),
    (r'https?://security\.ubuntu\.com/ubuntu', 'https://mirrors.aliyun.com/ubuntu'),
    (r'https?://ports\.ubuntu\.com/ubuntu-ports', 'https://mirrors.aliyun.com/ubuntu-ports'),
    (r'https?://(?:deb\.debian\.org|security\.debian\.org)/debian-security', 'https://mirrors.aliyun.com/debian-security'),
    (r'https?://deb\.debian\.org/debian(?!-)', 'https://mirrors.aliyun.com/debian'),
    (r'https?://security\.debian\.org/?(?=\s|$)', 'https://mirrors.aliyun.com/debian-security'),
)
MARKER = 'SKILLPATCH_APT_MIRROR_V1'
PROXY_MARKER = 'SKILLPATCH_APT_BUILD_PROXY_V1'


def build_proxy_command(proxy):
    endpoint = urlsplit(proxy)
    if (endpoint.scheme not in {'http', 'https'} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError('APT build proxy must be an HTTP(S) endpoint without credentials')
    configuration = f'Acquire::http::Proxy "{proxy}"; Acquire::https::Proxy "{proxy}";'
    return (f': {PROXY_MARKER}; if [ -z "${{APT_CONFIG+x}}" ]; then '
            f'printf "%s\\n" {shlex.quote(configuration)} > /tmp/skillpatch-build-apt-proxy.conf; '
            'export APT_CONFIG=/tmp/skillpatch-build-apt-proxy.conf; fi')


def migrate_previous_apt_urls(text):
    # Only previously approved APT paths; leave Python/custom repositories alone.
    return re.sub(r'(?<=://)mirrors\.ustc\.edu\.cn/(?=(?:ubuntu(?:-ports)?|debian(?:-security)?)(?![A-Za-z0-9-]))',
                  'mirrors.aliyun.com/', text)


def route_urls(text):
    text = migrate_previous_apt_urls(text)
    for pattern, target in MIRRORS:
        text = re.sub(pattern, lambda match: target.replace('https://', 'https://' if match.group().startswith('https://') else 'http://', 1), text)
    return text


def bootstrap_command():
    # sed uses extended regex. Split the look-ahead rules into explicit URL
    # substitutions, with debian-security before debian to retain its path.
    rules = [
        r's|(https?)://([[:alnum:]-]+\.)?archive\.ubuntu\.com/ubuntu|\1://mirrors.aliyun.com/ubuntu|g',
        r's|(https?)://security\.ubuntu\.com/ubuntu|\1://mirrors.aliyun.com/ubuntu|g',
        r's|(https?)://ports\.ubuntu\.com/ubuntu-ports|\1://mirrors.aliyun.com/ubuntu-ports|g',
        r's|(https?)://deb\.debian\.org/debian-security|\1://mirrors.aliyun.com/debian-security|g',
        r's|(https?)://security\.debian\.org/debian-security|\1://mirrors.aliyun.com/debian-security|g',
        r's|(https?)://deb\.debian\.org/debian|\1://mirrors.aliyun.com/debian|g',
    ]
    rules.append(r's#(https?)://mirrors\.ustc\.edu\.cn/(ubuntu(-ports)?|debian(-security)?)([/[:space:]]|$)#\1://mirrors.aliyun.com/\2\5#g')
    expression = ';'.join(rules)
    return (
        f': {MARKER}; '
        'for apt_source in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do '
        '[ -f "$apt_source" ] || continue; '
        f'sed -E -i {shlex.quote(expression)} "$apt_source" || exit $?; '
        'done'
    )


def _rewrite_dockerfile(text, python_index=None, model_download_proxy=None):
    """Route temporary build sources without changing embedded script bodies."""
    text = migrate_previous_apt_urls(text)
    from .python_mirrors import build_bootstrap_command, MARKER as PYTHON_MARKER
    from .apt_request_retries import bootstrap_command as retry_bootstrap, MARKER as RETRY_MARKER
    python_setup = build_bootstrap_command(index=python_index) if python_index else build_bootstrap_command()
    old_combined = bootstrap_command() + '; ' + python_setup
    combined = old_combined + '; ' + retry_bootstrap()
    if MARKER in text:
        if RETRY_MARKER in text:
            return text
        prior = old_combined if PYTHON_MARKER in text else bootstrap_command()
        text = text.replace(prior, combined)
        # JSON-form RUN instructions contain escaped shell argument strings.
        return text.replace(json.dumps(prior)[1:-1], json.dumps(combined)[1:-1])
    lines = text.splitlines(keepends=True)
    output, index = [], 0
    bootstrap = combined
    while index < len(lines):
        start = index
        first = lines[index]
        index += 1
        # Read a complete Docker instruction; do not interpret heredoc bodies
        # as FROM/RUN instructions.
        instruction = first
        while instruction.rstrip().endswith('\\') and index < len(lines):
            instruction += lines[index]
            index += 1
        heredocs = re.findall(r'<<-?\s*[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]?', instruction)
        body = []
        for terminal in heredocs:
            while index < len(lines):
                line = lines[index]
                body.append(line)
                index += 1
                if line.strip() == terminal:
                    break
        chunk = instruction + ''.join(body)
        if not re.match(r'^\s*RUN\s', first, re.I):
            output.append(chunk)
            continue
        # Rewrite official URLs in commands that create new sources too.
        if re.search(r'\bapt(?:-get)?\b', chunk):
            chunk = route_urls(chunk)
        layer_bootstrap = bootstrap
        if model_download_proxy and 'huggingface_hub' in chunk and 'snapshot_download' in chunk:
            build_proxy_command(model_download_proxy)  # Validate the same approved endpoint.
            assignments = ' '.join(name + '=' + shlex.quote(model_download_proxy)
                for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'))
            layer_bootstrap += '; export ' + assignments
            from .hf_request_retries import bootstrap_command as hf_bootstrap
            layer_bootstrap += '; ' + hf_bootstrap()
        if re.match(r'^\s*RUN\s+(?:--\S+\s+)*<<', first, re.I):
            split = chunk.find('\n') + 1
            chunk = chunk[:split] + layer_bootstrap + '\n' + chunk[split:]
        else:
            match = re.match(r'^(\s*RUN\s+(?:--\S+\s+)*)(.*)', chunk, re.I | re.S)
            prefix, command = match.groups()
            if command.lstrip().startswith('['):
                args = json.loads(command.replace('\\\n', ' '))
                chunk = prefix + json.dumps(['/bin/sh', '-c', layer_bootstrap + '; exec ' + shlex.join(args)]) + '\n'
            else:
                chunk = prefix + layer_bootstrap + '; ' + command
        output.append(chunk)
    return ''.join(output)


def route_git_clone_commands(text, transport):
    """Route matching shell clones without touching earlier layers or heredoc data."""
    if not transport:
        return text
    repository, proxy = transport['repository'], transport['proxy']
    endpoint = urlsplit(repository)
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment
            or not endpoint.path.endswith('.git') or transport['http_version'] != 'HTTP/1.1'):
        raise ValueError('Git preparation route must retain an HTTPS repository and HTTP/1.1')
    build_proxy_command(proxy)
    pattern = re.compile(r'(?P<prefix>(?:^|[;&])[\s\\]*(?:exec[ \t]+)?)git[ \t]+clone[ \t]+'
        r'(?P<quote>[\'\"]?)' + re.escape(repository) + r'(?P=quote)(?=[\s\\;&]|$)')
    replacement = 'git -c ' + shlex.quote('http.proxy=' + proxy) + ' -c http.version=HTTP/1.1 clone '

    def rewrite(command):
        return pattern.sub(lambda match: match['prefix'] + replacement
            + match['quote'] + repository + match['quote'], command)

    lines, output, index = text.splitlines(keepends=True), [], 0
    while index < len(lines):
        instruction = lines[index]
        index += 1
        while instruction.rstrip().endswith('\\') and index < len(lines):
            instruction += lines[index]
            index += 1
        heredocs = re.findall(r'<<-?\s*[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]?', instruction)
        body = ''
        for terminal in heredocs:
            while index < len(lines):
                line = lines[index]
                body += line
                index += 1
                if line.strip() == terminal:
                    break
        match = re.match(r'^(\s*RUN\s+(?:--\S+\s+)*)(.*)', instruction, re.I | re.S)
        if match:
            prefix, command = match.groups()
            if command.lstrip().startswith('['):
                args = json.loads(command.replace('\\\n', ' '))
                if len(args) == 3 and args[:2] == ['/bin/sh', '-c']:
                    changed = rewrite(args[2])
                    if changed != args[2]:
                        instruction = prefix + json.dumps([*args[:2], changed]) + '\n'
            else:
                instruction = prefix + rewrite(command)
        output.append(instruction + body)
    return ''.join(output)


def rewrite_dockerfile(text, build_proxy=None, python_index=None, model_download_proxy=None,
                       git_transport=None):
    result = _rewrite_dockerfile(text, python_index=python_index, model_download_proxy=model_download_proxy)
    from .playwright_request_retries import route_dockerfile as retry_browser_downloads
    result = retry_browser_downloads(result)
    if build_proxy and PROXY_MARKER not in result:
        prior = bootstrap_command()
        replacement = build_proxy_command(build_proxy) + '; ' + prior
        result = result.replace(prior, replacement).replace(
            json.dumps(prior)[1:-1], json.dumps(replacement)[1:-1])
    return route_git_clone_commands(result, git_transport)


def requested_build_proxy(audit_dir):
    path = Path(audit_dir).resolve()
    for folder in (path, *path.parents):
        marker = folder / 'APT_MIRROR_REQUESTED.json'
        if marker.is_file():
            return json.loads(marker.read_text()).get('build_proxy')
    return None


def requested_git_transport(audit_dir, task_name):
    path = Path(audit_dir).resolve()
    for folder in (path, *path.parents):
        marker = folder / 'APT_MIRROR_REQUESTED.json'
        if marker.is_file():
            return json.loads(marker.read_text()).get('git_clone_transport', {}).get(task_name)
    return None


def apply_build_mirror(environment_dir, audit_dir):
    from .image_registry_routing import route_auxiliary_files
    auxiliary_changed = route_auxiliary_files(environment_dir, audit_dir)
    path = Path(environment_dir) / 'Dockerfile'
    if not path.is_file():
        return False
    before = path.read_text()
    proxy = requested_build_proxy(audit_dir)
    from .python_mirrors import policy_for_directory
    python_policy = policy_for_directory(audit_dir)
    python_index = (python_policy['index_url'] if python_policy and
                    Path(environment_dir).parent.name in python_policy.get('build_tasks', []) else None)
    model_proxy = (proxy if python_policy and
                   Path(environment_dir).parent.name in python_policy.get('hf_proxy_tasks', []) else None)
    git_transport = requested_git_transport(audit_dir, Path(environment_dir).parent.name)
    after = rewrite_dockerfile(before, build_proxy=proxy, python_index=python_index,
                               model_download_proxy=model_proxy, git_transport=git_transport)
    from .image_registry_routing import requested_policy as registry_policy, route_dockerfile as route_registry
    after, registry_routes = route_registry(after, registry_policy(audit_dir))
    from .rpm_mirrors import run_policy as rpm_policy, route_dockerfile as route_rpm
    rpm = rpm_policy(audit_dir, Path(environment_dir).parent.name)
    if rpm:
        after = route_rpm(after, rpm)
    if before == after:
        return auxiliary_changed
    audit = Path(audit_dir)
    audit.mkdir(parents=True, exist_ok=True)
    original = audit / 'Dockerfile.before'
    if not original.exists():
        original.write_text(before)
    else:
        (audit / ('Dockerfile.before-' + hashlib.sha256(before.encode()).hexdigest()[:12])).write_text(before)
    (audit / 'Dockerfile.mirrored').write_text(after)
    (audit / 'routing.json').write_text(json.dumps({
        'temporary_build_context': str(path),
        'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
        'after_sha256': hashlib.sha256(after.encode()).hexdigest(),
        'build_proxy': proxy,
        'python_build_index': python_index,
        'model_download_proxy': model_proxy,
        'rpm_download_policy': rpm,
        'image_registry_routes': registry_routes,
        'git_clone_transport': git_transport,
        'policy': 'Aliyun mirrors; native suites, components, signing keys and APT verification retained',
    }, indent=2) + '\n')
    temporary = path.with_name(path.name + '.mirror-tmp')
    temporary.write_text(after)
    temporary.replace(path)
    return True


async def route_environment(env, audit_dir, phase):
    folder = Path(audit_dir)
    folder.mkdir(parents=True, exist_ok=True)
    result = await env.exec(bootstrap_command(), timeout_sec=30)
    if result.return_code:
        raise RuntimeError('APT mirror routing failed in preparation environment')
    from .python_mirrors import route_environment as route_python
    await route_python(env, folder, phase)
    await record_packages(env, folder, phase)


async def record_packages(env, audit_dir, phase):
    from .python_mirrors import record_packages as record_python
    await record_python(env, audit_dir, phase)
    command = ("if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f='${binary:Package}\\t${Version}\\t${Architecture}\\n'; "
               "elif command -v rpm >/dev/null 2>&1; then rpm -qa --queryformat '%{NAME}\\t%{VERSION}-%{RELEASE}\\t%{ARCH}\\n'; fi")
    folder = Path(audit_dir)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        result = await env.exec(command, timeout_sec=30)
        if result.return_code:
            raise RuntimeError('Package inventory command did not complete')
        (folder / (phase + '.packages.tsv')).write_text(result.stdout or '')
    except Exception as exc:
        # Inventory is observational; a teardown race must not fail a task.
        (folder / (phase + '.inventory-unavailable.json')).write_text(json.dumps({
            'status': 'unavailable', 'error_type': type(exc).__name__}) + '\n')


def mirror_requested(bench_args):
    if '--config' not in bench_args:
        return False
    config = Path(bench_args[bench_args.index('--config') + 1]).resolve()
    for root in config.parents:
        marker = root / 'APT_MIRROR_REQUESTED.json'
        if marker.is_file():
            return json.loads(marker.read_text()).get('enabled') is True
    return False
