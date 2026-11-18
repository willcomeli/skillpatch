"""Clean finalized owned containers and dangling images during monitoring."""

import argparse
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime


def command(args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'Docker {args[0]} failed: {result.stderr[-300:]}')
    return result.stdout


def cleanup(run):
    run = run.resolve(strict=True)
    lock = (run / 'docker-housekeeping.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {'status': 'already_checking'}
    record = {'at': datetime.now().astimezone().isoformat(), 'removed_containers': [],
              'free_bytes_before': shutil.disk_usage(run).free,
              'scope': 'Finalized stopped experiment containers and unreferenced dangling images; '
                       'retain running containers, tagged base images and reusable build cache'}
    try:
        record['usage_before'] = command(['system', 'df'])
        ids = command(['ps', '-aq', '--filter', 'label=benchflow.owned=true']).split()
        for cid in ids:
            # Never inspect secret-bearing Config.Env or command arguments.
            raw = command(['inspect', '--format',
                '{"state":{{json .State.Status}},"mounts":{{json .Mounts}}}', cid])
            item = json.loads(raw)
            if item['state'] not in {'exited', 'dead'}:
                continue
            finalized = False
            for mount in item['mounts']:
                source = Path(mount.get('Source', '/')).resolve()
                if not source.is_relative_to(run) or source.name not in {'agent', 'verifier', 'artifacts'}:
                    continue
                trial = source.parent
                if trial.parent.parent.name != 'jobs':
                    continue
                execution = trial.parent.parent.parent
                path = execution / 'result.json'
                if path.exists() and (trial / 'result.json').exists():
                    state = json.loads(path.read_text()).get('status')
                    finalized = state in {'completed', 'timeout', 'agent_error', 'backend_error', 'verifier_error', 'failed'}
                if finalized:
                    break
            if finalized:
                # No force flag: Docker rejects a container that restarted.
                command(['container', 'rm', cid])
                record['removed_containers'].append(cid)
        builds = [json.loads(line) for line in command(
            ['buildx', 'history', 'ls', '--format', 'json']).splitlines() if line.strip()]
        record['active_image_builds'] = sum(row.get('status', '').lower() == 'running' for row in builds)
        image_progress = run / 'google_image_reload/image-resume-progress.json'
        record['active_image_preload'] = (image_progress.exists() and
            json.loads(image_progress.read_text()).get('status') == 'running')
        if record['active_image_builds'] or record['active_image_preload']:
            # Avoid global image GC while containerd is ingesting a layer.
            # Stopped, finalized Task containers above can still be removed.
            record['dangling_image_cleanup'] = 'Deferred while image builds or their downloads are active'
        else:
            record['dangling_image_cleanup'] = command(['image', 'prune', '--force'])
        record['usage_after'] = command(['system', 'df'])
        record['status'] = 'completed'
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        record.update(status='deferred', error_type=type(error).__name__, error_message=str(error))
    record['free_bytes_after'] = shutil.disk_usage(run).free
    folder = run / 'docker-housekeeping'
    folder.mkdir(exist_ok=True)
    (folder / (datetime.now().strftime('%Y%m%dT%H%M%S') + '.json')).write_text(json.dumps(record, indent=2))
    (run / 'docker-housekeeping-latest.json').write_text(json.dumps(record, indent=2))
    return {k: record[k] for k in ('at', 'status', 'removed_containers', 'free_bytes_after')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    print(json.dumps(cleanup(parser.parse_args().run)))
