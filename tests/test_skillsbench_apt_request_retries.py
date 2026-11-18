from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from scripts.skillsbench.apt_request_retries import WRAPPER, bootstrap_command


class AptRequestRetryTests(unittest.TestCase):
    def invoke(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake = folder / 'native-apt'
            fake.write_text('''#!/bin/sh
cd "$(dirname "$0")"
n=0; [ ! -f count ] || read n < count
n=$((n + 1)); echo "$n" > count
if [ "$n" -gt 1 ] && [ ! -f downloaded.deb ]; then exit 99; fi
touch downloaded.deb
case "$MODE" in
 network) if [ "$n" -le 7 ]; then echo 'E: Failed to fetch pkg.deb 502  Bad Gateway'; exit 100; fi ;;
 index) if [ "$n" = 1 ]; then echo 'W: Failed to fetch index Could not resolve host'; exit 0; fi ;;
 permanent) echo 'E: Unable to locate package absent'; exit 100 ;;
esac
printf '%s\\n' "$*"
''')
            fake.chmod(0o755)
            (folder / 'sleep').write_text('#!/bin/sh\nexit 0\n')
            (folder / 'sleep').chmod(0o755)
            wrapper = folder / 'apt-get'
            wrapper.write_text(WRAPPER.replace('/usr/bin/apt-get', str(fake)))
            result = subprocess.run(['sh', str(wrapper), '--yes', 'install', 'hello'],
                env={**os.environ, 'MODE': mode, 'PATH': str(folder) + ':' + os.environ['PATH']},
                capture_output=True, text=True)
            return result, int((folder / 'count').read_text())

    def test_seven_failures_reuse_downloaded_archives_and_original_arguments(self):
        result, count = self.invoke('network')
        self.assertEqual((result.returncode, count), (0, 8))
        self.assertIn('--yes install hello', result.stdout)

    def test_update_warning_with_zero_exit_retries_the_lost_index(self):
        result, count = self.invoke('index')
        self.assertEqual((result.returncode, count), (0, 2))

    def test_permanent_package_error_stops_and_bootstrap_has_no_docker_newline(self):
        result, count = self.invoke('permanent')
        self.assertEqual((result.returncode, count), (100, 1))
        self.assertNotIn('\n', bootstrap_command())
        subprocess.run(['sh', '-n', '-c', bootstrap_command()], check=True)


if __name__ == '__main__':
    unittest.main()
