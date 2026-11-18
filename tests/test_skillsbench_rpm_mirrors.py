import base64
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import os
import shlex
from unittest.mock import patch

from scripts.skillsbench.apt_mirrors import apply_build_mirror, rewrite_dockerfile
from scripts.skillsbench.rpm_mirrors import bootstrap_command, route_dockerfile
from scripts.skillsbench.retry_policy import transient_network_failure
from scripts.skillsbench import preparation_followup


class RpmMirrorTests(unittest.TestCase):
    def test_followup_cannot_replay_an_agent_or_replace_a_changed_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'failed/one'
            source.mkdir(parents=True)
            (source / 'result.json').write_text(json.dumps({'execution': {'trajectory_events': 1},
                                                           'feedback': {'status': 'unavailable'}}))
            request = {'task': 'one', 'failure_source': str(source),
                       'failure_result_sha256': preparation_followup.exp.sha256_file(source / 'result.json')}
            (root / 'PREPARATION_FOLLOWUP_REQUESTED.json').write_text(json.dumps(request))
            with patch.object(preparation_followup.exp, '_baseline_job') as execute:
                with self.assertRaisesRegex(ValueError, 'replay Agent'):
                    preparation_followup.main(['--run', str(root)])
                execute.assert_not_called()

    def test_dnf_same_command_retries_seven_failures_with_ca_package_in_log(self):
        command = bootstrap_command('http://host.docker.internal:7897')
        wrapper = base64.b64decode(re.search(r'printf %s (\S+) \| base64 -d', command).group(1)).decode()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            count = root / 'count'
            fake = root / 'dnf-native'
            fake.write_text('#!/bin/sh\nn=$(cat ' + shlex.quote(str(count))
                + ' 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > '
                + shlex.quote(str(count)) + '\n[ "$n" -ge 8 ] && exit 0\n'
                + 'echo ca-certificates\necho "Curl error (28): Timeout was reached"\nexit 1\n')
            fake.chmod(0o755)
            sleep = root / 'sleep'
            sleep.write_text('#!/bin/sh\nexit 0\n')
            sleep.chmod(0o755)
            tested = wrapper.replace('/usr/bin/dnf', shlex.quote(str(fake)))
            result = subprocess.run(['sh', '-c', tested, 'dnf', '-y', 'install', 'ca-certificates'],
                env={**os.environ, 'PATH': str(root) + ':' + os.environ['PATH']}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(count.read_text().strip(), '8')
            self.assertEqual(result.stderr.count('Retrying the same DNF'), 7)

    def test_dnf_transport_message_is_retryable_but_auth_and_certificates_are_not(self):
        self.assertTrue(transient_network_failure('Curl error (28): Timeout was reached [Operation too slow]'))
        self.assertFalse(transient_network_failure('Curl error (60): SSL certificate problem'))
        self.assertFalse(transient_network_failure('Curl error (28) followed by 403 Forbidden'))

    def test_run_only_wrapper_keeps_dnf_arguments_and_verification(self):
        command = bootstrap_command('http://host.docker.internal:7897')
        subprocess.run(['bash', '-n', '-c', command], check=True)
        encoded = re.search(r'printf %s (\S+) \| base64 -d', command).group(1)
        wrapper = base64.b64decode(encoded).decode()
        subprocess.run(['bash', '-n', '-c', wrapper], check=True)
        self.assertIn('"$@"', wrapper)
        self.assertIn('limit=unlimited; cache retained', wrapper)
        self.assertNotIn('clean all', wrapper)
        self.assertNotIn('gpgcheck=0', command)
        self.assertNotIn('sslverify', command)

    def test_only_opted_in_task_is_routed_and_existing_scripts_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'RPM_MIRROR_REQUESTED.json').write_text(json.dumps({'tasks': {
                'suricata-custom-exfil': {'proxy': 'http://host.docker.internal:7897'}}}))
            for name in ('suricata-custom-exfil', 'other'):
                context = root / 'temporary' / name / 'environment'
                context.mkdir(parents=True)
                source = 'FROM jasonish/suricata:7.0.11\nRUN dnf -y install jq\n'
                (context / 'Dockerfile').write_text(source)
                audit = root / 'jobs' / name / 'mirror-routing'
                apply_build_mirror(context, audit)
                after = (context / 'Dockerfile').read_text()
                self.assertEqual('SKILLPATCH_RPM_MIRROR_V1' in after, name == 'suricata-custom-exfil')
                self.assertIn('dnf -y install jq', after)
                self.assertEqual((audit / 'Dockerfile.before').read_text(), source)
                self.assertFalse(apply_build_mirror(context, audit))

    def test_exec_and_heredoc_forms_are_idempotent(self):
        source = 'FROM almalinux:9\nRUN ["dnf", "install", "jq"]\nRUN <<EOF\necho original\nEOF\n'
        routed = route_dockerfile(rewrite_dockerfile(source), {'proxy': 'http://host.docker.internal:7897'})
        self.assertIn('echo original\nEOF\n', routed)
        self.assertEqual(route_dockerfile(routed, {'proxy': 'http://host.docker.internal:7897'}), routed)
        self.assertNotIn('\nENV ', routed)
        self.assertEqual(json.loads(routed.splitlines()[1][4:])[:2], ['/bin/sh', '-c'])


if __name__ == '__main__':
    unittest.main()
