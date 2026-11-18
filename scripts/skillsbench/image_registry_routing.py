"""Route verified public image manifests in temporary preparation contexts."""
import hashlib
import json
from pathlib import Path
import re

FROM = re.compile(r'^(\s*FROM\s+(?:--\S+\s+)*)(\S+)(.*)$', re.I)
COPY = re.compile(r'^(\s*COPY\s+(?:--(?!from=)\S+\s+)*--from=)(\S+)(.*)$', re.I)
HOSTS = {'docker.io': 'docker.m.daocloud.io', 'ghcr.io': 'ghcr.m.daocloud.io',
         'gcr.io': 'gcr.m.daocloud.io'}


def canonical_ref(ref):
    if '$' in ref or ref.lower() == 'scratch':
        return None
    first = ref.split('/')[0]
    explicit = '/' in ref and ('.' in first or ':' in first or first == 'localhost')
    if not explicit:
        ref = 'docker.io/' + (ref if '/' in ref else 'library/' + ref)
    if ref.startswith('index.docker.io/'):
        ref = 'docker.io/' + ref.split('/', 1)[1]
    if '@' not in ref and ':' not in ref.rsplit('/', 1)[-1]:
        ref += ':latest'
    return ref


def from_lines(text):
    """Yield external FROM/COPY images, excluding stages and heredoc data."""
    pending = []
    aliases = set()
    for number, line in enumerate(text.splitlines(keepends=True)):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        match = FROM.match(line.rstrip('\r\n')) or COPY.match(line.rstrip('\r\n'))
        if match:
            ref = match[2]
            if ref.lower() not in aliases and not ref.isdecimal():
                yield number, match, canonical_ref(ref)
            alias = re.match(r'\s+AS\s+(\S+)', match[3], re.I) if FROM.match(line) else None
            if alias:
                aliases.add(alias[1].lower())
        if not line.lstrip().startswith('#'):
            pending.extend(re.findall(r'<<-?\s*[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]?', line))


def requested_policy(folder):
    path = Path(folder).resolve()
    for parent in (path, *path.parents):
        marker = parent / 'REGISTRY_MIRROR_REQUESTED.json'
        if marker.is_file():
            return json.loads(marker.read_text())
    return None


def public_client_environment(args, environment):
    """Use an experiment-owned public Docker client; leave global login intact."""
    values = dict(environment)
    if '--config' not in args:
        return values
    config = Path(args[args.index('--config') + 1]).resolve()
    for root in config.parents:
        marker = root / 'REGISTRY_MIRROR_REQUESTED.json'
        if not marker.is_file():
            continue
        client = json.loads(marker.read_text()).get('public_client')
        if not client:
            return values
        folder = Path(client['config_dir']).resolve(strict=True)
        if not folder.is_relative_to(root):
            raise ValueError('Public Docker client must stay inside its experiment')
        file = folder / 'config.json'
        digest = 'sha256:' + hashlib.sha256(file.read_bytes()).hexdigest()
        if digest != client['config_sha256']:
            raise ValueError('Public Docker client configuration changed')
        document = json.loads(file.read_text())
        if document.get('auths') or document.get('credsStore') or any(document.get('credHelpers', {}).values()):
            raise ValueError('Public preparation client must not retrieve registry credentials')
        if document.get('currentContext') != client['context_name']:
            raise ValueError('Public Docker context changed')
        if values.get('DOCKER_CONTEXT') not in (None, '', client['context_name']):
            raise ValueError('Public Docker client conflicts with an explicit context')
        if values.get('DOCKER_HOST') not in (None, '', client['endpoint']):
            raise ValueError('Public Docker client conflicts with an explicit endpoint')
        values['DOCKER_CONFIG'] = str(folder)
        return values
    return values


def route_dockerfile(text, policy):
    if not policy or not policy.get('enabled'):
        return text, []
    lines = text.splitlines(keepends=True)
    records = []
    for number, match, ref in from_lines(text):
        spec = policy.get('images', {}).get(ref)
        if not spec:
            continue
        digest = 'sha256:' + hashlib.sha256(spec['manifest_bytes'].encode()).hexdigest()
        if digest != spec['manifest_digest'] or spec['original_ref'] != ref:
            raise ValueError('Verified original image manifest changed')
        host, repository = ref.split('/', 1)
        if host not in HOSTS:
            raise ValueError('Unapproved registry route')
        target = HOSTS[host] + '/' + repository
        if '@' in target:
            if target.rsplit('@', 1)[1] != digest:
                raise ValueError('Original image digest changed')
        else:
            target += '@' + digest
        if target != spec['mirror_ref']:
            raise ValueError('Registry route changed repository or tag')
        newline = '\r\n' if lines[number].endswith('\r\n') else '\n' if lines[number].endswith('\n') else ''
        lines[number] = match[1] + target + match[3] + newline
        records.append({'original_ref': match[2], 'canonical_ref': ref, 'mirror_ref': target,
                        'manifest_digest': digest})
    return ''.join(lines), records


def route_auxiliary_files(environment_dir, audit_dir):
    root = Path(environment_dir).resolve()
    policy = requested_policy(audit_dir)
    changed = False
    for path in sorted(root.glob('Dockerfile.*')):
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('Auxiliary Dockerfile escapes preparation context')
        before = path.read_text()
        after, routes = route_dockerfile(before, policy)
        if before == after:
            continue
        folder = Path(audit_dir) / 'image-registry' / path.name
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(before.encode()).hexdigest()
        (folder / ('before-' + digest + '.txt')).write_text(before)
        (folder / 'after.txt').write_text(after)
        (folder / 'routing.json').write_text(json.dumps(routes, indent=2) + '\n')
        temporary = path.with_name(path.name + '.registry-tmp')
        temporary.write_text(after)
        temporary.replace(path)
        changed = True
    return changed
