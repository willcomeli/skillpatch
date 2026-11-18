import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.skillsbench.image_registry_routing import canonical_ref, from_lines, route_dockerfile, route_auxiliary_files, public_client_environment


class RegistryRoutingTests(unittest.TestCase):
    def test_public_client_is_per_experiment_and_does_not_modify_caller_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); folder = root / 'client'; folder.mkdir()
            file = folder / 'config.json'; file.write_text(json.dumps({'auths': {}, 'currentContext': 'desktop-linux'}))
            client = {'config_dir': str(folder), 'config_sha256': 'sha256:' + hashlib.sha256(file.read_bytes()).hexdigest(),
                      'context_name': 'desktop-linux', 'endpoint': 'unix:///original/socket'}
            marker = root / 'REGISTRY_MIRROR_REQUESTED.json'; marker.write_text(json.dumps({'public_client': client}))
            args = ['--config', str(root / 'backend/config.json')]
            env = {'DOCKER_CONFIG': '/original/config', 'OTHER': 'preserved'}
            actual = public_client_environment(args, env)
            self.assertEqual(actual, {**env, 'DOCKER_CONFIG': str(folder)})
            self.assertEqual(env['DOCKER_CONFIG'], '/original/config')
            with self.assertRaises(ValueError): public_client_environment(args, {**env, 'DOCKER_HOST': 'unix:///other'})
            file.write_text('{}')
            with self.assertRaises(ValueError): public_client_environment(args, env)
            self.assertEqual(public_client_environment([], env), env)

    def policy(self):
        content = '{"schemaVersion":2}'
        digest = 'sha256:' + hashlib.sha256(content.encode()).hexdigest()
        return {'enabled': True, 'images': {'docker.io/library/ubuntu:24.04': {
            'original_ref': 'docker.io/library/ubuntu:24.04', 'manifest_bytes': content,
            'manifest_digest': digest, 'mirror_ref': 'docker.m.daocloud.io/library/ubuntu:24.04@' + digest}}}

    def test_tag_dots_are_not_registry_names(self):
        self.assertEqual(canonical_ref('ubuntu:24.04'), 'docker.io/library/ubuntu:24.04')
        self.assertEqual(canonical_ref('python:3.12-slim'), 'docker.io/library/python:3.12-slim')
        self.assertEqual(canonical_ref('ghcr.io/astral-sh/uv:0.9.7'), 'ghcr.io/astral-sh/uv:0.9.7')

    def test_platform_stage_alias_and_heredoc_are_preserved(self):
        text = ("FROM --platform=linux/amd64 ubuntu:24.04 AS builder\n"
                "RUN python3 - <<'PY'\nfrom huggingface_hub import snapshot_download\nPY\n"
                "FROM builder AS final\nCOPY --from=builder /app /app\n")
        policy = self.policy()
        actual, records = route_dockerfile(text, policy)
        self.assertEqual(len(records), 1)
        self.assertIn('FROM --platform=linux/amd64 docker.m.daocloud.io/library/ubuntu:24.04@sha256:', actual)
        self.assertTrue(actual.endswith("FROM builder AS final\nCOPY --from=builder /app /app\n"))
        self.assertIn('from huggingface_hub import snapshot_download', actual)
        self.assertEqual(route_dockerfile(actual, policy), (actual, []))
        self.assertEqual([ref for _, _, ref in from_lines(text)], ['docker.io/library/ubuntu:24.04'])

    def test_changed_manifest_repository_and_original_digest_are_rejected(self):
        for field, value in [('manifest_bytes', '{}'), ('mirror_ref', 'evil.invalid/library/ubuntu:24.04')]:
            policy = self.policy(); policy['images']['docker.io/library/ubuntu:24.04'][field] = value
            with self.assertRaises(ValueError): route_dockerfile('FROM ubuntu:24.04\n', policy)
        untouched = 'FROM ${BASE_IMAGE}\nFROM scratch\nFROM private.invalid/team/image:version\n'
        self.assertEqual(route_dockerfile(untouched, self.policy()), (untouched, []))

    def test_copy_external_image_routes_but_named_and_numeric_stages_do_not(self):
        text = ('FROM scratch AS build\n'
                'COPY --chown=0:0 --from=ubuntu:24.04 /bin /bin\n'
                'COPY --from=build /bin /bin\nCOPY --from=0 /lib /lib\n')
        actual, records = route_dockerfile(text, self.policy())
        self.assertEqual(len(records), 1)
        self.assertIn('COPY --chown=0:0 --from=docker.m.daocloud.io/library/ubuntu:24.04@sha256:', actual)
        self.assertTrue(actual.endswith('COPY --from=build /bin /bin\nCOPY --from=0 /lib /lib\n'))

    def test_auxiliary_file_routed_only_in_temporary_context_and_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'REGISTRY_MIRROR_REQUESTED.json').write_text(json.dumps(self.policy()))
            context = root / 'context'; context.mkdir()
            file = context / 'Dockerfile.api'; file.write_text('FROM ubuntu:24.04\nRUN echo api\n')
            self.assertTrue(route_auxiliary_files(context, root / 'audit'))
            self.assertIn('docker.m.daocloud.io', file.read_text())
            self.assertTrue((root / 'audit/image-registry/Dockerfile.api/routing.json').exists())
            self.assertFalse(route_auxiliary_files(context, root / 'audit'))
