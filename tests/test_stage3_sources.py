"""Accepted-queue ingestion rechecks actual Stage2 artifacts, not just labels."""

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from skillpatch.context import refresh_input_digest
from skillpatch.executor import Executor
from skillpatch.models import EvidenceItem
from skillpatch.patch_sources import read_object
from skillpatch.patches import render_patch
from skillpatch.stage3_sources import load_accepted_queue, read_receipt, save_receipt
from skillpatch.stages.patch_validation import run_stage2
from skillpatch.storage import sha256_file, write_json
import test_stage2 as fixtures


class Stage3SourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Stage2Fixture("test_prepare_only_never_calls_model_or_execution")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        result, _ = self.fixture.execute([fixtures.candidate(), fixtures.assessment()])
        self.assertEqual(result["decision"], "accepted", result)
        self.queue_path = self.root / "episode/stage3_queue.json"
        self.queue = read_object(self.queue_path)
        self.validation_path = Path(self.queue["validation_path"])

    def load(self):
        return load_accepted_queue(self.queue_path, adapter_factory=lambda _: self.fixture.adapter)

    def replace_validation(self, value):
        write_json(self.validation_path, value)
        self.queue["validation_sha256"] = sha256_file(self.validation_path)
        write_json(self.queue_path, self.queue)

    def test_accepts_reproducible_foreign_benchmark_queue_without_running_anything(self):
        budgets = list((self.fixture.run_dir / "stage2/budgets").glob("*.json"))
        self.assertEqual(len(budgets), 1)
        before_budget = budgets[0].read_bytes()
        before_calls = list(self.fixture.backend.calls)
        original_report = (self.fixture.diagnosis / "report.json").read_bytes()
        accepted = self.load()
        self.assertEqual(accepted.patch.patch_id, self.queue["patch"]["patch_id"])
        self.assertEqual(accepted.candidate_pack, Path(self.queue["candidate_pack"]))
        self.assertEqual(accepted.receipt["queue_sha256"], sha256_file(self.queue_path))
        self.assertEqual(accepted.presentation_mode, "sidecar_v1")
        self.assertEqual(accepted.receipt["presentation_mode"], "sidecar_v1")
        self.assertEqual(accepted.cases[0]["role"], "source")
        self.assertEqual(accepted.cases[0]["baseline_origin"], "source")
        self.assertEqual(Path(accepted.cases[0]["baseline_dir"]), self.fixture.run_dir)
        self.assertEqual(self.fixture.backend.calls, before_calls)
        self.assertEqual(budgets[0].read_bytes(), before_budget)
        self.assertEqual((self.fixture.diagnosis / "report.json").read_bytes(), original_report)

    def test_reused_baseline_must_match_source_run_identity(self):
        data = read_object(self.validation_path)
        data["cases"][0]["baseline_run_id"] = "another-original-run"
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "bound source execution"):
            self.load()

    def test_reused_baseline_must_match_source_result_digest(self):
        data = read_object(self.validation_path)
        data["cases"][0]["baseline_result_sha256"] = "sha256:" + "0" * 64
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "source baseline result digest"):
            self.load()

    def test_regression_cannot_claim_source_baseline(self):
        data = read_object(self.validation_path)
        data["cases"][0]["role"] = "regression"
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "bound source execution"):
            self.load()

    def test_reused_source_needs_explicit_origin(self):
        data = read_object(self.validation_path)
        del data["cases"][0]["baseline_origin"]
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "unique archived validation run"):
            self.load()

    def test_source_baseline_allows_only_execution_neutral_request_changes(self):
        source_request_path = self.fixture.run_dir / "request.json"
        source_request = read_object(source_request_path)
        source_request.update(diagnosis_config=str(self.root / "source-diagnosis.toml"),
                              skill_condition="reference")
        write_json(source_request_path, source_request)
        accepted = self.load()
        candidate_request_path = Path(accepted.cases[0]["candidate_dir"]) / "request.json"
        candidate_request = read_object(candidate_request_path)
        candidate_request["model"] = "different-execution-model"
        write_json(candidate_request_path, candidate_request)
        with self.assertRaisesRegex(ValueError, "model, harness or budgets differ"):
            self.load()

    def test_legacy_episode_local_baseline_remains_supported(self):
        # Produce a real fixture execution in the old layout; no paths or
        # resource snapshots are relabelled to masquerade as another run.
        source = self.fixture.source
        request = replace(source.request, task_dir=source.prepared.task_dir,
                          skills_dir=source.prepared.skills_dir, diagnosis_config=None,
                          skill_condition="stage2-validation")
        baseline_dir = self.root / "episode/baselines/source-baseline-000"
        Executor(self.fixture.adapter, self.fixture.backend).run(
            request, baseline_dir, {"FIXTURE_KEY": "fixture-secret-value"})
        context = self.fixture.adapter.collect_evidence(baseline_dir)
        data = read_object(self.validation_path)
        item = data["cases"][0]
        del item["baseline_origin"]
        del item["baseline_result_sha256"]
        item.update(baseline_run_id=baseline_dir.name, baseline_input_digest=context.input_digest)
        self.replace_validation(data)
        evaluation_path = self.validation_path.parent / "evaluation/source/000/evaluation.json"
        evaluation = read_object(evaluation_path)
        evaluation["original_input_digests"]["baseline"] = context.input_digest
        evaluation["baseline_digest"] = context.input_digest
        write_json(evaluation_path, evaluation)
        before_calls = list(self.fixture.backend.calls)
        accepted = self.load()
        self.assertEqual(Path(accepted.cases[0]["baseline_dir"]), baseline_dir)
        self.assertEqual(accepted.cases[0]["baseline_origin"], "fresh")
        self.assertEqual(self.fixture.backend.calls, before_calls)

    def test_source_observations_remain_bound_to_evaluation_and_receipt(self):
        observation_path = self.fixture.diagnosis / "observed-table.txt"
        observation_path.write_text("Output table contains fewer rows than the input.")
        context = self.fixture.adapter.collect_evidence(self.fixture.run_dir)
        context.evidence.append(EvidenceItem(
            "A000001", "artifact", observation_path.read_text(), str(observation_path),
            version=sha256_file(observation_path), availability="post_execution",
            observation={"local_source": str(observation_path)}))
        refresh_input_digest(context)
        write_json(self.fixture.diagnosis / "input.json", context)
        report = read_object(self.fixture.diagnosis / "report.json")
        report["input_digest"] = context.input_digest
        write_json(self.fixture.diagnosis / "report.json", report)
        output = self.root / "observed-episode"
        result = run_stage2(self.fixture.diagnosis, "C000001", self.fixture.config, output,
            {"FIXTURE_KEY": "fixture-secret-value"},
            client=fixtures.RecordedClient([fixtures.candidate(), fixtures.assessment()]),
            adapter_factory=lambda _: self.fixture.adapter, backend_factory=lambda _: self.fixture.backend)
        self.assertEqual(result["decision"], "accepted", result)
        accepted = load_accepted_queue(output / "stage3_queue.json",
                                       adapter_factory=lambda _: self.fixture.adapter)
        self.assertEqual(accepted.source.context.input_digest, context.input_digest)
        self.assertEqual(accepted.receipt["archive_files"][str(observation_path)],
                         sha256_file(observation_path))

    def test_validation_digest_tampering_rejected(self):
        data = read_object(self.validation_path)
        data["reason"] = "changed after publication"
        write_json(self.validation_path, data)
        with self.assertRaisesRegex(ValueError, "validation digest"):
            self.load()

    def test_pending_label_cannot_enter_stage3_even_with_updated_checksum(self):
        data = read_object(self.validation_path)
        data.update(decision="pending", ready_for_stage3=False)
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "only imports accepted"):
            self.load()

    def test_queued_patch_must_equal_validated_patch(self):
        self.queue["patch"]["how_to_interpret"] += " New unvalidated requirement."
        write_json(self.queue_path, self.queue)
        with self.assertRaisesRegex(ValueError, "Validated and queued patches differ"):
            self.load()

    def test_queue_cannot_relabel_presentation_without_revalidation(self):
        self.queue["presentation_mode"] = "inline_v1"
        write_json(self.queue_path, self.queue)
        with self.assertRaisesRegex(ValueError, "presentation modes differ"):
            self.load()

    def test_missing_historical_mode_never_assumes_sidecar_validation(self):
        self.queue.pop("presentation_mode")
        data = read_object(self.validation_path)
        data.pop("presentation_mode")
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "exact source plus archived patch"):
            self.load()

    def test_historical_inline_queue_without_mode_reconstructs_original_presentation(self):
        output = self.root / "legacy-inline-episode"
        def render_legacy(source, candidate, destination):
            return render_patch(source, candidate, destination, presentation_mode="inline_v1")
        with patch("skillpatch.stages.patch_validation.render_patch", side_effect=render_legacy):
            result = run_stage2(self.fixture.diagnosis, "C000001", self.fixture.config, output,
                {"FIXTURE_KEY": "fixture-secret-value"},
                client=fixtures.RecordedClient([fixtures.candidate(), fixtures.assessment()]),
                adapter_factory=lambda _: self.fixture.adapter, backend_factory=lambda _: self.fixture.backend)
        self.assertEqual(result["decision"], "accepted", result)
        queue_path = output / "stage3_queue.json"
        queue = read_object(queue_path)
        validation_path = Path(queue["validation_path"])
        validation = read_object(validation_path)
        queue.pop("presentation_mode")
        validation.pop("presentation_mode")
        write_json(validation_path, validation)
        queue["validation_sha256"] = sha256_file(validation_path)
        write_json(queue_path, queue)
        before = queue_path.read_bytes(), validation_path.read_bytes()

        accepted = load_accepted_queue(queue_path, adapter_factory=lambda _: self.fixture.adapter)

        self.assertEqual(accepted.presentation_mode, "inline_v1")
        self.assertFalse((accepted.candidate_pack / "table/SKILL_PATCHES.md").exists())
        self.assertNotEqual((accepted.candidate_pack / "table/SKILL.md").read_bytes(),
                            (self.fixture.source.prepared.skills_dir / "table/SKILL.md").read_bytes())
        self.assertEqual((queue_path.read_bytes(), validation_path.read_bytes()), before)

    def replay_queue(self):
        external = self.root / 'provided-patch.json'
        write_json(external, self.queue['patch'])
        output = self.root / 'replay-episode'
        result = run_stage2(self.fixture.diagnosis, 'C000001', self.fixture.config, output,
            {'FIXTURE_KEY': 'fixture-secret-value'}, patch_file=external,
            client=fixtures.RecordedClient([fixtures.assessment()]),
            adapter_factory=lambda _: self.fixture.adapter, backend_factory=lambda _: self.fixture.backend)
        self.assertEqual(result['decision'], 'accepted', result)
        external.unlink()
        return output / 'stage3_queue.json'

    def test_exact_replay_queue_uses_immutable_copy_without_external_file_dependency(self):
        queue = self.replay_queue()

        accepted = load_accepted_queue(queue, adapter_factory=lambda _: self.fixture.adapter)

        self.assertEqual(accepted.presentation_mode, 'sidecar_v1')
        self.assertEqual(accepted.receipt['patch_origin'], 'existing')
        copied_patch = queue.parent / 'source_patch.json'
        self.assertEqual(accepted.receipt['archive_files'][str(copied_patch)], sha256_file(copied_patch))
        generation = Path(read_object(queue)['validation_path']).parent / 'generation/generation.json'
        self.assertEqual(accepted.receipt['archive_files'][str(generation)], sha256_file(generation))
        self.assertNotIn(str(self.root / 'provided-patch.json'), accepted.receipt['archive_files'])

    def test_exact_replay_patch_archive_tampering_is_rejected(self):
        queue = self.replay_queue()
        copied_patch = queue.parent / 'source_patch.json'
        copied_patch.write_text(copied_patch.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'replay archive'):
            load_accepted_queue(queue, adapter_factory=lambda _: self.fixture.adapter)

    def test_queued_patch_binding_cannot_be_retargeted(self):
        self.queue["patch"]["target"]["quote"] = "a different clause"
        write_json(self.queue_path, self.queue)
        with self.assertRaisesRegex(ValueError, "source diagnosis"):
            self.load()

    def test_saved_patch_read_audit_must_match_recomputed_execution_evidence(self):
        accepted = self.load()
        candidate = Path(accepted.cases[0]['candidate_dir'])
        audit = candidate / 'patch_read_audit.json'
        self.assertEqual(accepted.receipt['archive_files'][str(audit)], sha256_file(audit))
        write_json(audit, {'status': 'passed', 'reason': 'unsupported claim'})
        with self.assertRaisesRegex(ValueError, 'archived patch read audit'):
            self.load()

    def test_changed_candidate_resource_rejected(self):
        helper = Path(self.queue["candidate_pack"]) / "table/helper.py"
        helper.write_text("changed helper resource")
        with self.assertRaisesRegex(ValueError, "candidate pack changed"):
            self.load()

    def test_candidate_cannot_change_unrelated_resource_even_if_queue_rehashed(self):
        from skillpatch.patches import tree_hashes
        from skillpatch.storage import hash_manifest
        pack = Path(self.queue["candidate_pack"])
        (pack / "table/helper.py").write_text("changed helper resource")
        self.queue["candidate_pack_digest"] = hash_manifest(tree_hashes(pack))
        write_json(self.queue_path, self.queue)
        with self.assertRaisesRegex(ValueError, "exact source plus archived patch"):
            self.load()

    def test_mutated_execution_snapshot_rejected(self):
        accepted = self.load()
        execution = Path(accepted.cases[0]["candidate_dir"])
        record = read_object(execution / "result.json")
        (Path(record["prepared"]["skills_dir"]) / "table/helper.py").write_text("mutated after run")
        with self.assertRaisesRegex(ValueError, "input snapshot changed"):
            self.load()

    def test_independent_evaluation_must_agree_with_validation(self):
        evaluation_path = self.validation_path.parent / "evaluation/source/000/evaluation.json"
        data = read_object(evaluation_path)
        data["evaluation"]["target_status"] = "uncertain"
        write_json(evaluation_path, data)
        with self.assertRaisesRegex(ValueError, "Independent evaluation"):
            self.load()

    def test_comparison_recomputed_instead_of_trusting_record(self):
        data = read_object(self.validation_path)
        data["cases"][0]["comparison"]["status"] = "regressed"
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "verifier comparison"):
            self.load()

    def test_receipt_roundtrip_and_archive_tampering(self):
        accepted = self.load()
        reference = save_receipt(accepted, self.root / "receipt.json")
        self.assertEqual(read_receipt(reference), accepted.receipt)
        self.validation_path.write_text(self.validation_path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "support archive changed"):
            read_receipt(reference)

    def test_receipt_own_digest_checked(self):
        reference = save_receipt(self.load(), self.root / "receipt.json")
        path = Path(reference["path"])
        data = read_object(path)
        data["coverage_notes"] = ["new coverage claim"]
        write_json(path, data)
        with self.assertRaisesRegex(ValueError, "support receipt changed"):
            read_receipt(reference)

    def test_receipt_detects_candidate_resource_changed_after_import(self):
        accepted = self.load()
        reference = save_receipt(accepted, self.root / "receipt.json")
        (accepted.candidate_pack / "table/helper.py").write_text("mutated after receipt")
        with self.assertRaises(ValueError):
            read_receipt(reference)

    def test_receipt_detects_resources_added_after_import(self):
        accepted = self.load()
        reference = save_receipt(accepted, self.root / "receipt.json")
        (accepted.candidate_pack / "table/new-helper.py").write_text("unvalidated extra resource")
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            read_receipt(reference)

    def test_receipt_detects_raw_execution_evidence_changed_after_import(self):
        accepted = self.load()
        reference = save_receipt(accepted, self.root / "receipt.json")
        record = read_object(Path(accepted.cases[0]["candidate_dir"]) / "result.json")
        trajectory = Path(record["execution"]["normalized_trajectory_path"])
        trajectory.write_text(trajectory.read_text() + "Changed archived evidence.\n")
        with self.assertRaises(ValueError):
            read_receipt(reference)

    def test_queue_paths_cannot_escape_episode(self):
        outside = self.root / "outside-validation.json"
        outside.write_bytes(self.validation_path.read_bytes())
        self.queue["validation_path"] = str(outside)
        self.queue["validation_sha256"] = sha256_file(outside)
        write_json(self.queue_path, self.queue)
        with self.assertRaisesRegex(ValueError, "outside its Stage2 episode"):
            self.load()

    def accepted_with_unresolved_evaluation(self, **changes):
        """Exercise the historical Stage2 decision contract, without editing its artifacts."""
        output = self.root / "unresolved-episode"
        payload = dict(fixtures.assessment(), **changes)
        client = fixtures.RecordedClient([fixtures.candidate(), payload])
        result = run_stage2(self.fixture.diagnosis, "C000001", self.fixture.config, output,
            {"FIXTURE_KEY": "fixture-secret-value"}, client=client,
            adapter_factory=lambda _: self.fixture.adapter,
            backend_factory=lambda _: self.fixture.backend)
        self.assertEqual(result["decision"], "accepted", result)
        return output / "stage3_queue.json"

    def test_legacy_uncertainty_notes_do_not_block_or_rewrite_accepted_archives(self):
        evaluation_path = self.validation_path.parent / "evaluation/source/000/evaluation.json"
        validation = read_object(self.validation_path)
        archived = read_object(evaluation_path)
        notes = ["The verifier's reference algorithm is not published."]
        validation["cases"][0]["evaluation"]["uncertainties"] = notes
        archived["evaluation"]["uncertainties"] = notes
        self.replace_validation(validation)
        write_json(evaluation_path, archived)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        before_calls = list(self.fixture.backend.calls)

        accepted = self.load()

        self.assertEqual(accepted.patch.patch_id, self.queue["patch"]["patch_id"])
        self.assertEqual(self.fixture.backend.calls, before_calls)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertEqual(accepted.receipt["archive_files"][str(evaluation_path)], sha256_file(evaluation_path))
        self.assertEqual(accepted.receipt["archive_files"][str(self.validation_path)], sha256_file(self.validation_path))

    def test_legacy_normalization_does_not_ignore_unknown_evaluation_fields(self):
        data = read_object(self.validation_path)
        data["cases"][0]["evaluation"].update(uncertainties=[], unsupported_field=[])
        self.replace_validation(data)
        with self.assertRaisesRegex(ValueError, "fields do not match the schema"):
            self.load()

    def test_legacy_normalization_preserves_independent_evaluation_agreement(self):
        evaluation_path = self.validation_path.parent / "evaluation/source/000/evaluation.json"
        data = read_object(evaluation_path)
        data["evaluation"].update(uncertainties=[], unsupported_field=[])
        write_json(evaluation_path, data)
        with self.assertRaisesRegex(ValueError, "Independent evaluation"):
            self.load()

    def test_legacy_uncertain_status_still_cannot_enter_stage3(self):
        evaluation_path = self.validation_path.parent / "evaluation/source/000/evaluation.json"
        validation = read_object(self.validation_path)
        archived = read_object(evaluation_path)
        changes = {"target_status": "uncertain", "uncertainties": ["Required evidence is missing."]}
        validation["cases"][0]["evaluation"].update(changes)
        archived["evaluation"].update(changes)
        self.replace_validation(validation)
        write_json(evaluation_path, archived)
        self.load()

    def test_requested_revision_cannot_be_inherited_despite_old_accepted_label(self):
        queue = self.accepted_with_unresolved_evaluation(revision_action="revise_patch")
        budget = next((self.fixture.run_dir / "stage2/budgets").glob("*.json"))
        before = budget.read_bytes()
        load_accepted_queue(queue, adapter_factory=lambda _: self.fixture.adapter)
        self.assertEqual(budget.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
