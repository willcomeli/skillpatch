"""Retry APT transport failures inside one build RUN, retaining downloads."""
import shlex
import base64

MARKER = 'SKILLPATCH_APT_REQUEST_RETRY_V1'
WRAPPER = r'''#!/bin/sh
log=$(mktemp) || exit 1
trap 'rm -f "$log"' EXIT
attempt=0
while :; do
    /usr/bin/apt-get "$@" >"$log" 2>&1
    result=$?
    cat "$log"
    if grep -Eiq 'certificate verification failed|certificate verify failed|permission denied|no space left on device|401[[:space:]]+Unauthorized|403[[:space:]]+Forbidden' "$log"; then
        exit "$result"
    fi
    if grep -Eiq 'Connection (failed|reset|refused|timed out)|Unable to connect|Could not (connect|resolve)|Temporary failure resolving|Something wicked happened resolving|502[[:space:]]+Bad Gateway|503[[:space:]]+Service Unavailable|504[[:space:]]+Gateway Timeout|Network is unreachable' "$log"; then
        attempt=$((attempt + 1))
        printf 'Retrying the same APT preparation command; attempt=%s; limit=unlimited; downloaded archives retained\n' "$attempt" >&2
        case "$attempt" in 1) sleep 1 ;; 2) sleep 2 ;; *) sleep 4 ;; esac
        continue
    fi
    exit "$result"
done
'''


def bootstrap_command():
    # PATH is scoped to this RUN, never persisted as Docker ENV. Runtime
    # Agent tools continue using native apt-get. Docker's cleanup hooks stay.
    folder = '/tmp/skillpatch-build-download-tools'
    encoded = base64.b64encode(WRAPPER.encode()).decode()
    return (f': {MARKER}; if [ -x /usr/bin/apt-get ]; then mkdir -p {folder}; '
            f'printf %s {shlex.quote(encoded)} | base64 -d > {folder}/apt-get; '
            f'chmod 755 {folder}/apt-get; export PATH={folder}:"$PATH"; fi')
