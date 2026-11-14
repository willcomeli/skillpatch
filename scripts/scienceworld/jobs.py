"""Private subprocess entry point for isolated experiment tasks."""

import os
from pathlib import Path
import sys

from set_up.environment import redact, secret_values
from .splits import read, save


def main(request, response):
    from .experiment import execute, skillpatch_task, transient
    environment = dict(os.environ)
    job = read(request)
    try:
        if job['kind'] == 'execute':
            source = execute(job['entry'], job['skill'], Path(job['folder']), environment,
                             Path(job['patch_dir']) if job.get('patch_dir') else None)
            value = str(source) if source is not None else None
        elif job['kind'] == 'skillpatch':
            value = [str(path) for path in skillpatch_task(
                job['entry'], job['skill'], Path(job['folder']),
                Path(job['inputs']), environment)]
        else:
            raise ValueError(f'Unknown job kind: {job["kind"]}')
        save(response, {'status': 'completed', 'value': value})
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        save(response, {'status': 'failed', 'retryable': transient(error),
                        'error': redact(f'{type(error).__name__}: {error}', secret_values(environment))})
        return 2


if __name__ == '__main__':
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
