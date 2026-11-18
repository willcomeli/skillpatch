"""Task-scoped download-URL routing in an audited temporary grader copy."""

import copy
import hashlib
import json
from pathlib import Path
import shutil

OFFICIAL = 'https://pypi.org/simple'
FILES = ('test_outputs.py', 'run_passed.sh')


def digest(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def requested_policy(rollout_dir, name):
    path = Path(rollout_dir).resolve()
    for folder in (path, *path.parents):
        marker = folder / 'VERIFIER_INDEX_REQUESTED.json'
        if marker.is_file():
            return json.loads(marker.read_text()).get('tasks', {}).get(name)
    return None


def routed_task(task, folder, policy):
    index = policy['index_url']
    if task.name != 'fix-build-agentops' or index not in (
            'https://mirrors.ustc.edu.cn/pypi/simple', 'https://mirrors.aliyun.com/pypi/simple'):
        raise ValueError('Verifier URL routing must use the approved task and HTTPS mirror')
    folder = Path(folder)
    target = folder / task.name
    shutil.copytree(task.task_dir, target)
    records = {}
    for name in FILES:
        source = Path(task.task_dir) / 'verifier' / name
        output = target / 'verifier' / name
        before = source.read_text()
        if digest(source) != policy['original_hashes'][name] or before.count(OFFICIAL) != 1:
            raise ValueError('Original hardcoded verifier download URL changed')
        after = before.replace(OFFICIAL, index)
        if after.replace(index, OFFICIAL) != before:
            raise ValueError('Verifier routing changed more than its download address')
        output.write_text(after)
        records[name] = {'before_sha256': digest(source), 'after_sha256': digest(output), 'url_replacements': 1}
    result = copy.copy(task)
    result._task_dir = target.resolve()
    result.paths = type(task.paths)(result._task_dir)
    result.config = copy.deepcopy(task.config)
    result.config.verifier.env = {**(result.config.verifier.env or {}),
                                 'UV_INDEX_URL': index, 'PIP_INDEX_URL': index}
    record = {'index_url': index, 'original_task_dir': str(task.task_dir), 'effective_task_dir': str(target),
              'files': records, 'verifier_timeout_seconds': result.config.verifier.timeout_sec,
              'scope': 'Only two hardcoded package download URL literals and index environment variables; '
                       'test logic, version constraints, TLS/hash checks and grading budget retained',
              'dependency_precache_outside_grader': False}
    (folder / 'routing.json').write_text(json.dumps(record, indent=2) + '\n')
    return result


class RoutedPlanes:
    def __init__(self, original, paths, policy):
        self.original, self.paths, self.policy = original, paths, policy

    def __getattr__(self, name):
        return getattr(self.original, name)

    def verifier(self, *args, **kwargs):
        if args or 'task' not in kwargs:
            raise ValueError('Unexpected native verifier factory signature')
        task = routed_task(kwargs['task'], self.paths.rollout_dir / 'verifier-index-routing', self.policy)
        return self.original.verifier(**{**kwargs, 'task': task})
