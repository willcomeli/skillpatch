"""Retry browser preparation downloads inside their original Docker RUN."""

import base64
import re
import shlex

MARKER = 'SKILLPATCH_PLAYWRIGHT_REQUEST_RETRY_V1'
WRAPPER = r'''#!/bin/sh
log=$(mktemp) || exit 1
trap 'rm -f "$log"' EXIT
attempt=0
while :; do
    "$@" >"$log" 2>&1
    result=$?
    cat "$log"
    [ "$result" != 0 ] || exit 0
    if grep -Eiq 'certificate verif|self.signed certificate|permission denied|no space left on device|401[[:space:]]+Unauthorized|403[[:space:]]+Forbidden|no matching distribution|could not find a version that satisfies' "$log"; then
        exit "$result"
    fi
    if ! grep -Eiq 'Request to https?://[^[:space:]]+ timed out|server closed connection|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|connection (reset|closed|refused|timed out)|socket disconnected|502[[:space:]]+Bad Gateway|503[[:space:]]+Service Unavailable|504[[:space:]]+Gateway Timeout' "$log"; then
        exit "$result"
    fi
    attempt=$((attempt + 1))
    printf 'Retrying the same Playwright browser preparation command; attempt=%s; limit=unlimited; existing browser cache retained\n' "$attempt" >&2
    case "$attempt" in 1) sleep 1 ;; 2) sleep 2 ;; *) sleep 4 ;; esac
done
'''
SCRIPT = '/tmp/skillpatch-playwright-download-retry.sh'
COMMAND = re.compile(r'(?<![\w/.-])(python3?(?:\.\d+)?\s+-m\s+playwright\s+install)(?=\s|$)')


def bootstrap_command():
    encoded = base64.b64encode(WRAPPER.encode()).decode()
    return (f': {MARKER}; printf %s {shlex.quote(encoded)} | base64 -d > {SCRIPT}')


def route_dockerfile(text):
    # Only shell-form RUN instructions with an explicit Python Playwright
    # install invocation. Do not reinterpret JSON RUNs or heredoc scripts.
    lines = iter(text.splitlines(keepends=True))
    output = []
    for first in lines:
        chunk = first
        while chunk.rstrip().endswith('\\'):
            following = next(lines, '')
            if not following:
                break
            chunk += following
        body = []
        for terminal in re.findall(r'<<-?\s*[\'\"]?([A-Za-z_][A-Za-z0-9_]*)[\'\"]?', chunk):
            for line in lines:
                body.append(line)
                if line.strip() == terminal:
                    break
        match = re.match(r'^(\s*RUN\s+(?:--\S+\s+)*)(.*)', chunk, re.I | re.S)
        if (match and MARKER not in chunk and not match[2].lstrip().startswith(('[', '<<'))
                and COMMAND.search(match[2])):
            command = COMMAND.sub(lambda m: f'sh {SCRIPT} ' + m[0], match[2])
            chunk = match[1] + bootstrap_command() + '; ' + command
        output.append(chunk + ''.join(body))
    return ''.join(output)
