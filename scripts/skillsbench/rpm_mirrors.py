"""Opt-in AlmaLinux/EPEL transport routing for native preparation commands."""
import base64
import json
from pathlib import Path
import shlex

MARKER = 'SKILLPATCH_RPM_MIRROR_V1'


def run_policy(audit_dir, task):
    path = Path(audit_dir).resolve()
    for folder in (path, *path.parents):
        marker = folder / 'RPM_MIRROR_REQUESTED.json'
        if marker.exists():
            return json.loads(marker.read_text()).get('tasks', {}).get(task)
    return None


def bootstrap_command(proxy):
    from .apt_mirrors import build_proxy_command
    build_proxy_command(proxy)  # Reuse strict endpoint validation.
    rules = []
    for repo, directory in (('baseos', 'BaseOS'), ('appstream', 'AppStream'),
                            ('crb', 'CRB'), ('extras', 'extras')):
        rules.append(r's|^mirrorlist=https://mirrors\.almalinux\.org/mirrorlist/\$releasever/'
                     + repo + r'$|baseurl=https://mirrors.huaweicloud.com/almalinux/$releasever/'
                     + directory + r'/$basearch/os/|')
    rules.append(r's|^metalink=https://mirrors\.fedoraproject\.org/metalink\?repo=epel-9&.*$|'
                 r'baseurl=https://mirrors.ustc.edu.cn/epel/9/Everything/$basearch/|')
    wrapper = r'''#!/bin/sh
log=$(mktemp) || exit 1
trap 'rm -f "$log"' EXIT
attempt=0
while :; do
    /usr/bin/dnf --setopt=proxy=__PROXY__ "$@" >"$log" 2>&1
    result=$?
    cat "$log"
    [ "$result" -eq 0 ] && exit 0
    if grep -Eiq 'certificate (verify|verification) failed|SSL certificate problem|permission denied|no space left|401[[:space:]]+Unauthorized|403[[:space:]]+Forbidden' "$log"; then
        exit "$result"
    fi
    if ! grep -Eiq 'Curl error \((5|6|7|16|18|28|35|52|55|56|92|95|96)\)|Connection (failed|reset|refused|timed out)|Could not resolve|Operation too slow|502[[:space:]]+Bad Gateway|503[[:space:]]+Service Unavailable|504[[:space:]]+Gateway Timeout' "$log"; then
        exit "$result"
    fi
    attempt=$((attempt + 1))
    printf 'Retrying the same DNF preparation command; attempt=%s; limit=unlimited; cache retained\n' "$attempt" >&2
    case "$attempt" in 1) sleep 1 ;; 2) sleep 2 ;; *) sleep 4 ;; esac
done
'''.replace('__PROXY__', shlex.quote(proxy))
    encoded = base64.b64encode(wrapper.encode()).decode()
    return (f': {MARKER}; for rpm_source in /etc/yum.repos.d/*.repo; do '
            '[ -f "$rpm_source" ] || continue; '
            f'sed -E -i {shlex.quote(";".join(rules))} "$rpm_source" || exit $?; done; '
            'if [ -x /usr/bin/dnf ]; then mkdir -p /tmp/skillpatch-build-download-tools; '
            f'printf %s {encoded} | base64 -d > /tmp/skillpatch-build-download-tools/dnf; '
            'chmod 755 /tmp/skillpatch-build-download-tools/dnf; '
            'export PATH=/tmp/skillpatch-build-download-tools:"$PATH"; fi')


def route_dockerfile(text, specification):
    if MARKER in text:
        return text
    from .apt_mirrors import bootstrap_command as apt_bootstrap
    before = apt_bootstrap()
    after = bootstrap_command(specification['proxy']) + '; ' + before
    return text.replace(before, after).replace(json.dumps(before)[1:-1], json.dumps(after)[1:-1])
