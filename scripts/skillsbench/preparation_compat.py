"""Apply explicitly approved, task-scoped dependency compatibility fixes."""
import hashlib
import json
from pathlib import Path
import re
import shlex

POLICY_FILE = 'PREPARATION_COMPATIBILITY_REQUESTED.json'
MARKER = 'SKILLPATCH_PREPARATION_COMPAT_V1'


def run_policy(audit_dir):
    path = Path(audit_dir).resolve()
    for folder in (path, *path.parents):
        marker = folder / POLICY_FILE
        if marker.is_file():
            return json.loads(marker.read_text())
    return None


def rewrite_dockerfile(text, specification):
    if MARKER in text:
        return text
    packages = specification['packages']
    if not packages or any(not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+', p) for p in packages):
        raise ValueError('Preparation compatibility packages require exact approved versions')
    anchor = specification['before_line']
    matches = list(re.finditer(r'(?m)^RUN[^\n]*(?:\\\r?\n[^\n]*)*\r?\n(?='
                              + re.escape(anchor) + r'\r?$)', text))
    if len(matches) != 1:
        raise ValueError('Approved dependency insertion point is missing or ambiguous')
    command = 'python -m pip install --no-cache-dir --no-deps ' + shlex.join(packages)
    insertion = f'# {MARKER}\nRUN {command}\n\n'
    if 'SKILLPATCH_APT_MIRROR_V1' in text:
        # A reused temporary context may already have routed RUN layers.
        # Route this new layer too, preserving the existing layers verbatim.
        from .apt_mirrors import rewrite_dockerfile as route_downloads
        insertion = route_downloads(insertion)
    offset = matches[0].start()
    return text[:offset] + insertion + text[offset:]


def apply_compatibility(environment_dir, audit_dir):
    environment_dir = Path(environment_dir)
    policy = run_policy(audit_dir)
    specification = (policy or {}).get('tasks', {}).get(environment_dir.parent.name)
    if not specification:
        return False
    path = environment_dir / 'Dockerfile'
    before = path.read_text(encoding='utf-8')
    after = rewrite_dockerfile(before, specification)
    if before == after:
        return False
    folder = Path(audit_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'Dockerfile.before').write_text(before, encoding='utf-8')
    (folder / 'Dockerfile.after').write_text(after, encoding='utf-8')
    (folder / 'compatibility.json').write_text(json.dumps({
        'task': environment_dir.parent.name, 'specification': specification,
        'before_sha256': hashlib.sha256(before.encode()).hexdigest(),
        'after_sha256': hashlib.sha256(after.encode()).hexdigest(),
        'scope': 'temporary SDK build context; pinned task dependencies and native limits unchanged',
    }, indent=2) + '\n', encoding='utf-8')
    temporary = path.with_name(path.name + '.compat-tmp')
    temporary.write_text(after, encoding='utf-8')
    temporary.replace(path)
    return True
