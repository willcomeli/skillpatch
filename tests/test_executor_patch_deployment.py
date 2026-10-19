"""Pinned deployment is checked on actual execution inputs before backend calls."""

import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from skillpatch.cli import main
from skillpatch.executor import Executor
from skillpatch.patch_deployment import prepare_patch_set_request
from skillpatch.patches import tree_hashes
import test_patch_deployment as deployment_fixtures
import test_stage2 as table_fixtures


class ExecutorPatchDeploymentTests(unittest.TestCase):
    def setUp(self):
        deployment_fixtures.PatchDeploymentTests.setUp(self)

    def prepared_request(self, *, adapter=None):
        return prepare_patch_set_request(self.request, adapter or self.adapter, self.store,
                                         self.root / 'deployment')

    def verification(self, name='run'):
        return json.loads((self.root / name / 'skill_deployment_verification.json').read_text())

    def test_matching_actual_pack_runs_and_records_verified_digest(self):
        request, manifest = self.prepared_request()
        before = tree_hashes(self.task)
        backend = table_fixtures.TableBackend()
        result = Executor(self.adapter, backend).run(request, self.root / 'run', {},
            expected_skill_digest=manifest['deployed_pack_digest'])
        self.assertEqual(result.status, 'completed', result.errors)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(self.verification()['status'], 'verified')
        self.assertEqual(self.verification()['actual_skill_digest'], manifest['deployed_pack_digest'])
        self.assertEqual(tree_hashes(self.task), before)

    def test_ignored_replacement_stops_before_prompt_preparation_or_backend(self):
        class IgnoringAdapter(table_fixtures.TableAdapter):
            def prepare(self, request, destination):
                return super().prepare(replace(request, skills_dir=None), destination)
        adapter = IgnoringAdapter()
        request, manifest = self.prepared_request(adapter=adapter)
        backend = table_fixtures.TableBackend()
        with patch.object(backend, 'prepare_prompt', create=True) as preview:
            result = Executor(adapter, backend).run(request, self.root / 'run', {},
                expected_skill_digest=manifest['deployed_pack_digest'])
            preview.assert_not_called()
        self.assertEqual(result.status, 'preparation_error')
        self.assertIsNone(result.execution)
        self.assertEqual(backend.calls, [])
        self.assertEqual(self.verification()['status'], 'failed')
        self.assertNotEqual(self.verification()['actual_skill_digest'], manifest['deployed_pack_digest'])

    def test_reported_expected_digest_does_not_hide_changed_resource_bytes(self):
        request, manifest = self.prepared_request()
        class ChangingAdapter(table_fixtures.TableAdapter):
            def prepare(self, request, destination):
                prepared = super().prepare(request, destination)
                (prepared.skills_dir / 'table/helper.py').write_text('Unexpected replacement implementation.\n')
                return prepared
        backend = table_fixtures.TableBackend()
        result = Executor(ChangingAdapter(), backend).run(request, self.root / 'run', {},
            expected_skill_digest=manifest['deployed_pack_digest'])
        self.assertEqual(result.status, 'preparation_error')
        self.assertEqual(self.verification()['reported_skill_digest'], manifest['deployed_pack_digest'])
        self.assertNotEqual(self.verification()['actual_skill_digest'], manifest['deployed_pack_digest'])
        self.assertEqual(backend.calls, [])

    def test_prompt_preparation_cannot_change_pinned_skill_before_execution(self):
        request, manifest = self.prepared_request()
        class MutatingPromptBackend(table_fixtures.TableBackend):
            def prepare_prompt(self, request, prepared, run_dir):
                (prepared.skills_dir / 'table/SKILL.md').write_text('Replacement instructions.\n')
        backend = MutatingPromptBackend()
        result = Executor(self.adapter, backend).run(request, self.root / 'run', {},
            expected_skill_digest=manifest['deployed_pack_digest'])
        self.assertEqual(result.status, 'preparation_error')
        self.assertEqual(backend.calls, [])
        self.assertEqual(self.verification()['status'], 'failed')
        self.assertEqual(self.verification()['phase'], 'after_prompt_prepare')

    def test_prepare_only_also_rejects_an_ignored_patch(self):
        request, manifest = self.prepared_request()
        class IgnoringAdapter(table_fixtures.TableAdapter):
            def prepare(self, request, destination):
                return super().prepare(replace(request, skills_dir=None), destination)
        backend = table_fixtures.TableBackend()
        result = Executor(IgnoringAdapter(), backend).run(request, self.root / 'run', {}, prepare_only=True,
            expected_skill_digest=manifest['deployed_pack_digest'])
        self.assertEqual(result.status, 'preparation_error')
        self.assertEqual(backend.calls, [])
        self.assertEqual(self.verification()['status'], 'failed')

    def test_native_digest_adapters_keep_existing_behavior_without_opt_in(self):
        class NativeDigestAdapter(table_fixtures.TableAdapter):
            def prepare(self, request, destination):
                prepared = super().prepare(request, destination)
                prepared.skill_digest = 'native-skill-version-17'
                return prepared
        backend = table_fixtures.TableBackend()
        result = Executor(NativeDigestAdapter(), backend).run(self.request, self.root / 'run', {})
        self.assertEqual(result.status, 'completed', result.errors)
        self.assertEqual(len(backend.calls), 1)
        self.assertFalse((self.root / 'run/skill_deployment_verification.json').exists())

    def invoke_cli(self, adapter, name):
        backend = table_fixtures.TableBackend()
        request = replace(self.request, backend='benchflow')
        with (patch('skillpatch.cli.load_config', return_value=(request, self.root)),
              patch('skillpatch.cli.load_environment', return_value={}),
              patch('skillpatch.cli.get_adapter', return_value=adapter),
              patch('skillpatch.runners.benchflow.BenchFlowBackend', return_value=backend),
              redirect_stdout(io.StringIO())):
            code = main(['run', '--config', str(self.root / 'fixture.toml'), '--output', str(self.root / name),
                         '--patch-registry', str(self.root / 'registry'), '--patch-skill-key', self.store.skill_key])
        return code, backend, json.loads((self.root / name / 'patch_set.json').read_text())

    def test_cli_persists_actual_successful_deployment_verification(self):
        code, backend, record = self.invoke_cli(self.adapter, 'cli-run')
        self.assertEqual(code, 0)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(record['execution_deployment_verification']['status'], 'verified')
        self.assertEqual(record['execution_deployment_verification']['actual_skill_digest'],
                         record['deployed_pack_digest'])

    def test_cli_detects_second_prepare_ignoring_replacement_before_running(self):
        class IgnoringAdapter(table_fixtures.TableAdapter):
            def prepare(self, request, destination):
                return super().prepare(replace(request, skills_dir=None), destination)
        code, backend, record = self.invoke_cli(IgnoringAdapter(), 'cli-rejected')
        self.assertEqual(code, 2)
        self.assertEqual(backend.calls, [])
        self.assertEqual(record['execution_deployment_verification']['status'], 'failed')
        self.assertEqual(json.loads((self.root / 'cli-rejected/result.json').read_text())['status'], 'preparation_error')


if __name__ == '__main__':
    unittest.main()
