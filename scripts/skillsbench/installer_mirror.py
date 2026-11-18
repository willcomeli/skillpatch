"""Temporarily route only the pinned native OpenHands installation to a mirror."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROUTE_FILE = '/tmp/skillpatch-openhands-index/route.json'


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_install(args, environment, spec):
    if args[:2] != ['tool', 'install'] or 'openhands' not in args:
        return False
    if environment.get('UV_INDEX_URL') != spec['previous_index']:
        return False
    try:
        source = args[args.index('--from') + 1]
        overrides = Path(args[args.index('--overrides') + 1]).read_text().splitlines()
    except (ValueError, IndexError, OSError):
        return False
    return (source == spec['native_source'] and sorted(overrides) == sorted(spec['overrides'])
            and not any(a in {'--index', '--index-url', '--default-index', '--extra-index-url'}
                        or a.startswith(('--index=', '--index-url=', '--default-index=', '--extra-index-url='))
                        for a in args))


def installer_environment(environment, spec):
    return {**environment, 'UV_INDEX_URL': spec['index_url'], 'PIP_INDEX_URL': spec['index_url']}


def route(args, spec, *, environment=None, run=subprocess.run, passthrough=os.execve):
    environment = dict(os.environ if environment is None else environment)
    real = spec['original_binary']
    if not native_install(args, environment, spec):
        return passthrough(real, [real, *args], environment)
    if file_sha(real) != spec['original_sha256']:
        raise ValueError('Original uv binary changed')
    events = Path(spec['events_file'])

    def record(status, **extra):
        with events.open('a') as stream:
            stream.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(),
                'status': status, 'index_url': spec['index_url'],
                'scope': 'same native OpenHands install command; arguments/pins/TLS unchanged', **extra}) + '\n')

    record('attempt')
    result = run([real, *args], env=installer_environment(environment, spec), check=False)
    record('returned', return_code=result.returncode)
    if result.returncode == 0:
        if file_sha(spec['wrapper_path']) != spec['wrapper_sha256']:
            raise ValueError('Temporary uv router changed before restoration')
        if file_sha(real) != spec['original_sha256']:
            raise ValueError('Original uv binary changed before restoration')
        os.replace(real, spec['wrapper_path'])
        record('original_binary_restored')
    return result.returncode


if __name__ == '__main__':
    spec = json.loads(Path(ROUTE_FILE).read_text())
    sys.exit(route(sys.argv[1:], spec))
