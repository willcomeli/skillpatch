import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import shlex

from scripts.skillsbench.apt_mirrors import apply_build_mirror, bootstrap_command, build_proxy_command, rewrite_dockerfile, route_urls


class AptMirrorTests(unittest.TestCase):
    def test_git_route_preserves_prior_cached_layers_repository_history_and_scripts(self):
        repository = 'https://github.com/apache/druid.git'
        source = ('FROM ubuntu:24.04\nRUN apt-get update\nRUN echo cached-download\n'
                  'RUN cd /root && \\\n    git clone ' + repository + ' && \\\n    cd druid && git checkout druid-0.20.0\n'
                  'RUN python3 - <<\'PY\'\nprint("git clone ' + repository + '")\nPY\n')
        plain = rewrite_dockerfile(source)
        transport = {'repository': repository, 'proxy': 'http://host.docker.internal:7897',
                     'http_version': 'HTTP/1.1'}
        routed = rewrite_dockerfile(plain, git_transport=transport)
        self.assertEqual(routed.split('cd /root', 1)[0], plain.split('cd /root', 1)[0])
        self.assertIn('git -c http.proxy=http://host.docker.internal:7897 -c http.version=HTTP/1.1 clone ' + repository, routed)
        self.assertIn('git checkout druid-0.20.0', routed)
        self.assertIn('print("git clone ' + repository + '")', routed)
        self.assertNotIn('--depth', routed)
        self.assertEqual(rewrite_dockerfile(routed, git_transport=transport), routed)

    def test_git_route_keeps_exec_clone_arguments_and_other_repositories(self):
        repository = 'https://github.com/apache/druid.git'
        transport = {'repository': repository, 'proxy': 'http://host.docker.internal:7897',
                     'http_version': 'HTTP/1.1'}
        text = 'FROM ubuntu:24.04\nRUN ["git", "clone", "' + repository + '", "destination"]\n'
        result = rewrite_dockerfile(text, git_transport=transport)
        command = json.loads(result.splitlines()[1].removeprefix('RUN '))[2]
        self.assertTrue(command.endswith('clone ' + repository + ' destination'))
        self.assertIn('exec git -c http.proxy=', command)
        other = rewrite_dockerfile('FROM ubuntu:24.04\nRUN git clone https://github.com/other/repo.git\n')
        self.assertEqual(rewrite_dockerfile(other, git_transport=transport), other)

    def test_shell_source_migration_preserves_suites_and_signing_key(self):
        text = ('URIs: http://mirrors.ustc.edu.cn/ubuntu-ports\n'
                'Suites: noble noble-updates\nSigned-By: /usr/share/keyrings/key.gpg\n'
                'deb https://mirrors.ustc.edu.cn/debian-security trixie-security main\n')
        args = shlex.split(bootstrap_command())
        expression = args[args.index('sed') + 3]
        actual = subprocess.run(['sed', '-E', expression], input=text,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(actual, text.replace('mirrors.ustc.edu.cn', 'mirrors.aliyun.com'))

    def test_previous_apt_route_migrates_without_changing_python_indexes(self):
        text = ('deb http://mirrors.ustc.edu.cn/ubuntu-ports noble main\n'
                'deb https://mirrors.ustc.edu.cn/debian-security trixie-security main\n'
                'https://mirrors.ustc.edu.cn/pypi/simple\n')
        result = route_urls(text)
        self.assertIn('http://mirrors.aliyun.com/ubuntu-ports noble main', result)
        self.assertIn('https://mirrors.aliyun.com/debian-security trixie-security main', result)
        self.assertIn('https://mirrors.ustc.edu.cn/pypi/simple', result)
        old = rewrite_dockerfile('FROM ubuntu:24.04\nRUN apt-get update\n').replace(
            '://mirrors.aliyun.com/', '://mirrors.ustc.edu.cn/')
        migrated = rewrite_dockerfile(old)
        self.assertNotIn('://mirrors.ustc.edu.cn/ubuntu', migrated)
        self.assertEqual(rewrite_dockerfile(migrated), migrated)

    def test_explicit_task_apt_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / 'apt.conf'
            original.write_text('task configuration')
            result = subprocess.run(['bash', '-c', build_proxy_command('http://host.docker.internal:7897')
                + '; printf "%s" "$APT_CONFIG"'], env={**os.environ, 'APT_CONFIG': str(original)},
                capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, str(original))
            self.assertEqual(original.read_text(), 'task configuration')

    def test_build_proxy_is_run_scoped_idempotent_and_preserves_exec_arguments(self):
        text = 'FROM ubuntu:24.04\nRUN ["apt-get", "download", "hello=2.10-3build1"]\n'
        mirror = rewrite_dockerfile(text)
        result = rewrite_dockerfile(mirror, build_proxy='http://host.docker.internal:7897')
        self.assertEqual(rewrite_dockerfile(result, build_proxy='http://host.docker.internal:7897'), result)
        self.assertNotIn('\nENV ', result)
        args = json.loads(result.splitlines()[1].removeprefix('RUN '))
        self.assertIn('Acquire::http::Proxy', args[2])
        self.assertTrue(args[2].endswith('; exec apt-get download hello=2.10-3build1'))
        self.assertNotIn('Verify-Peer', result)
        for proxy in ('ftp://proxy.invalid', 'http://user:secret@proxy.invalid'):
            with self.assertRaises(ValueError):
                rewrite_dockerfile(text, build_proxy=proxy)

    def test_run_marker_routes_build_proxy_without_changing_source_task(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'APT_MIRROR_REQUESTED.json').write_text(json.dumps({'enabled': True,
                'build_proxy': 'http://host.docker.internal:7897'}))
            context = root / 'temporary/environment'
            context.mkdir(parents=True)
            (context / 'Dockerfile').write_text('FROM ubuntu:24.04\nRUN apt-get update\n')
            self.assertTrue(apply_build_mirror(context, root / 'live/task/build'))
            self.assertIn('SKILLPATCH_APT_BUILD_PROXY_V1', (context / 'Dockerfile').read_text())
            self.assertEqual(json.loads((root / 'live/task/build/routing.json').read_text())['build_proxy'],
                             'http://host.docker.internal:7897')

    def test_suites_components_keys_and_transport_are_preserved(self):
        text = ('Types: deb\nURIs: http://ports.ubuntu.com/ubuntu-ports\n'
                'Suites: noble noble-updates\nComponents: main universe\n'
                'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
        result = route_urls(text)
        self.assertEqual(result, text.replace('http://ports.ubuntu.com/ubuntu-ports',
                                             'http://mirrors.aliyun.com/ubuntu-ports'))
        secure = 'deb https://deb.debian.org/debian-security trixie-security main'
        self.assertEqual(route_urls(secure), 'deb https://mirrors.aliyun.com/debian-security trixie-security main')
        custom = 'deb https://private.invalid/repo stable main'
        self.assertEqual(route_urls(custom), custom)

    def test_native_scripts_and_docker_instruction_boundaries_are_preserved(self):
        text = ("# syntax=docker/dockerfile:1.4\nFROM ubuntu:24.04\n"
                "RUN --mount=type=cache,target=/tmp/cache apt-get update && \\\n  apt-get install -y python3\n"
                "RUN python3 - <<'PY'\nfrom huggingface_hub import download\nprint('RUN something')\nPY\n"
                "RUN <<'EOF'\nset -e\napt-get update\nEOF\nCOPY input /work/input\n")
        result = rewrite_dockerfile(text)
        self.assertIn('RUN --mount=type=cache,target=/tmp/cache : SKILLPATCH_APT_MIRROR_V1;', result)
        self.assertIn("from huggingface_hub import download\nprint('RUN something')\nPY\n", result)
        self.assertEqual(result.count('SKILLPATCH_APT_MIRROR_V1'), 3)
        self.assertTrue(result.endswith('COPY input /work/input\n'))
        self.assertEqual(rewrite_dockerfile(result), result)

    def test_exec_form_preserves_original_argument_values(self):
        result = rewrite_dockerfile('FROM ubuntu:24.04\nRUN ["apt-get", "install", "python3"]\n')
        args = json.loads(result.splitlines()[1].removeprefix('RUN '))
        self.assertEqual(args[:2], ['/bin/sh', '-c'])
        self.assertTrue(args[2].endswith('; exec apt-get install python3'))

    def test_only_temporary_context_is_changed_and_original_is_archived(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / 'temporary/environment'
            context.mkdir(parents=True)
            original = 'FROM ubuntu:24.04\nRUN apt-get update\n'
            (context / 'Dockerfile').write_text(original)
            source = root / 'original-task-Dockerfile'
            source.write_text(original)
            self.assertTrue(apply_build_mirror(context, root / 'audit'))
            self.assertEqual(source.read_text(), original)
            self.assertEqual((root / 'audit/Dockerfile.before').read_text(), original)
            self.assertFalse(apply_build_mirror(context, root / 'audit'))


if __name__ == '__main__':
    unittest.main()
