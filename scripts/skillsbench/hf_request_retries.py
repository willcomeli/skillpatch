"""Preparation-only HuggingFace snapshot retries, preserving its file cache."""
import base64
from functools import wraps
from pathlib import Path
import shlex
import sys
import time

try:
    from .retry_policy import transient_network_failure, retry_delay
except ImportError:  # The same pinned source is embedded in the build RUN.
    from skillpatch_download_retry_policy import transient_network_failure, retry_delay


def wrap_snapshot_download(original):
    @wraps(original)
    def call(*args, **kwargs):
        attempt = 0
        while True:
            try:
                return original(*args, **kwargs)
            except Exception as error:
                if not transient_network_failure(error):
                    raise
                attempt += 1
                print('Retrying the same HuggingFace snapshot in place; attempt=' + str(attempt)
                      + '; cache retained; retry limit=unlimited', file=sys.stderr, flush=True)
                time.sleep(retry_delay(attempt))
    call._skillpatch_preparation_retry = True
    return call


def install_or_exit():
    try:
        import huggingface_hub
        if not getattr(huggingface_hub.snapshot_download, '_skillpatch_preparation_retry', False):
            huggingface_hub.snapshot_download = wrap_snapshot_download(huggingface_hub.snapshot_download)
    except Exception:
        import os
        os._exit(86)  # Python otherwise silently ignores sitecustomize failures.


def bootstrap_command():
    folder = '/tmp/skillpatch-hf-preparation-network'
    sources = {'skillpatch_download_retry_policy.py': Path(__file__).with_name('retry_policy.py').read_bytes(),
               'skillpatch_hf_request_retry.py': Path(__file__).read_bytes(),
               'sitecustomize.py': b'import skillpatch_hf_request_retry\nskillpatch_hf_request_retry.install_or_exit()\n'}
    commands = ['mkdir -p ' + folder]
    for name, content in sources.items():
        encoded = base64.b64encode(content).decode()
        commands.append('printf %s ' + shlex.quote(encoded) + ' | base64 -d > ' + folder + '/' + name)
    commands.append('export PYTHONPATH=' + folder + '"${PYTHONPATH:+:$PYTHONPATH}" HF_HUB_DISABLE_XET=1')
    return '; '.join(commands)
