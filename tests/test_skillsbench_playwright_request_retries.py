import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.skillsbench.playwright_request_retries import WRAPPER, SCRIPT, route_dockerfile


class BrowserDownloadRetryTests(unittest.TestCase):
    def test_same_command_keeps_cache_through_seven_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / 'native'
            native.write_text('''#!/bin/sh
cd "$(dirname "$0")"
n=0; [ ! -f count ] || read n < count
n=$((n + 1)); echo "$n" > count
if [ "$n" -gt 1 ] && [ ! -f retained ]; then exit 99; fi
touch retained
if [ "$n" -le 7 ]; then echo 'Error: Download failed: server closed connection.'; exit 1; fi
printf '%s\\n' "$*"
''')
            native.chmod(0o755)
            (root / 'sleep').write_text('#!/bin/sh\nexit 0\n')
            (root / 'sleep').chmod(0o755)
            script = root / 'retry'
            script.write_text(WRAPPER)
            result = subprocess.run(['sh', str(script), str(native), '-m', 'playwright',
                                     'install', '--with-deps', 'chromium'], capture_output=True, text=True,
                                    env={**os.environ, 'PATH': str(root) + ':' + os.environ['PATH']})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'count').read_text().strip(), '8')
            self.assertIn('-m playwright install --with-deps chromium', result.stdout)

    def test_permanent_failure_exits_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'retry'
            script.write_text(WRAPPER)
            result = subprocess.run(['sh', str(script), 'sh', '-c',
                                     'echo "server closed connection; certificate verify failed"; exit 17'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 17)
            self.assertNotIn('Retrying', result.stderr)

    def test_routing_preserves_pip_setup_versions_and_browser_arguments(self):
        source = ('FROM ubuntu:24.04\nRUN pip install playwright==1.49.1 && \\\n'
                  '    python3 -m playwright install --with-deps chromium\n')
        actual = route_dockerfile(source)
        self.assertIn('pip install playwright==1.49.1', actual)
        self.assertIn(f'sh {SCRIPT} python3 -m playwright install --with-deps chromium', actual)
        self.assertEqual(actual, route_dockerfile(actual))
        self.assertEqual(route_dockerfile('COPY python3 /usr/bin/python3\n'), 'COPY python3 /usr/bin/python3\n')

    def test_embedded_dockerfile_or_script_in_heredoc_stays_literal(self):
        source = "FROM ubuntu:24.04\nRUN cat > /tmp/nested <<'END'\nRUN python3 -m playwright install chromium\nEND\n"
        self.assertEqual(route_dockerfile(source), source)


if __name__ == '__main__':
    unittest.main()
