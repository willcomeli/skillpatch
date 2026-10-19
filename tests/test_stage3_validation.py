"""Fresh Stage3 comparisons on a foreign benchmark, without Docker or models."""

import copy
import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skillpatch.models import EvaluationFeedback
from skillpatch.patch_models import TargetEvaluation
from skillpatch.patches import tree_hashes
from skillpatch.stage3_config import Stage3Config
from skillpatch.stage3_validation import collect_cases, validate_patch_sets
from skillpatch.storage import hash_manifest, write_json
import test_stage2 as table_fixture
from test_stage2 import TableAdapter, TableBackend


def assess_actual_behavior(source, baseline, candidate, config, output, environment, **kwargs):
    def behavior(context):
        return next(item for item in context.evidence if item.evidence_id == "T000002")
    before, after = behavior(baseline), behavior(candidate)
    absent = before.text == "Preserved duplicate rows."
    repaired = after.text == "Preserved duplicate rows."
    result = TargetEvaluation("absent" if absent else "present", "repaired" if repaired else "not_repaired",
        "根据实际执行记录判断是否仍保留重复行。",
        [{"evidence_id": before.evidence_id, "quote": before.text}],
        [{"evidence_id": after.evidence_id, "quote": after.text}], [],
        "none" if repaired else "revise_patch")
    write_json(output / "evaluation.json", {"evaluation": result})
    return result


class DifferentTaskBackend(TableBackend):
    """The new source requires an extra clarification; the old repair still matters."""
    def run(self, request, task, run_dir, environment):
        result = super().run(request, task, run_dir, environment)
        if "incoming-case" in task.task_document.read_text():
            good = "handle incoming case" in (task.skills_dir / "table/SKILL.md").read_text()
            behavior = "Preserved duplicate rows." if good else "Removed duplicate rows."
            result.summary.update(loss=0 if good else 1, behavior=behavior)
            result.normalized_trajectory_path.write_text("Read the table.\n" + behavior + "\nSaved output.\n")
        return result


class PromptChangingAdapter(TableAdapter):
    def collect_evidence(self, run_dir, *, record_path=None):
        context = super().collect_evidence(run_dir, record_path=record_path)
        if "candidate" in run_dir.name:
            context.evidence[0] = replace(context.evidence[0], text="Changed actual execution prompt.")
        return context


class ProtectedInputChangingAdapter(TableAdapter):
    def prepare(self, request, destination):
        prepared = super().prepare(request, destination)
        if "candidate" in destination.parent.name:
            prepared.task_document.write_text("Changed task instructions.\n")
            prepared.file_hashes = tree_hashes(prepared.task_dir)
            prepared.task_digest = hash_manifest(prepared.file_hashes)
        return prepared


class UninformativeVerifierAdapter(TableAdapter):
    def evaluate(self, execution):
        # A coarse passing verifier must not mask the lost behavioral repair.
        return EvaluationFeedback("scored", {"loss": 0}, True, [{"name": "rows", "status": "passed"}])


class Stage3ValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = table_fixture.Stage2Fixture("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root / "stage3-tests"
        self.root.mkdir()
        self.source = self.fixture.source
        self.original = self.root / "original"
        shutil.copytree(self.source.prepared.skills_dir / "table", self.original)
        self.before, self.after = self.root / "before", self.root / "after"
        for directory in (self.before, self.after):
            shutil.copytree(self.original, directory)
            with (directory / "SKILL.md").open("a") as stream:
                stream.write("\nWhen requested, preserve duplicates.\n")
        self.adapter = TableAdapter()
        self.backend = TableBackend()
        project = Path(__file__).resolve().parents[1]
        self.config = Stage3Config(project / "configs/consolidation.toml",
            project / "configs/consolidation-evaluation.toml", project / "configs/patch-evaluation.toml")
        self.case = {"id": "source-old", "role": "source", "source": self.source,
            "request": replace(self.source.request, task_dir=self.source.prepared.task_dir,
                               skills_dir=self.source.prepared.skills_dir, diagnosis_config=None),
            "pack": self.source.prepared.skills_dir, "target_relative_path": "table/SKILL.md",
            "incoming": False}

    def validate(self, cases=None, *, mode="preservation"):
        with patch("skillpatch.stages.consolidation_evaluation.evaluate_preservation", side_effect=assess_actual_behavior) as preserve:
            with patch("skillpatch.stage3_validation.evaluate_target", side_effect=assess_actual_behavior) as repair:
                result = validate_patch_sets(cases or [self.case], self.before, self.after, self.original,
                    self.config, self.root / "validation", {}, mode=mode,
                    adapter_factory=lambda _: self.adapter, backend_factory=lambda _: self.backend)
        return result, preserve, repair

    def incoming_case(self):
        task = self.root / "incoming-task"
        shutil.copytree(self.source.prepared.task_dir, task)
        (task / "instructions.txt").write_text("Preserve duplicate records. incoming-case\n")
        source = copy.deepcopy(self.source)
        source.context.run_id = "incoming-source"
        source.context.input_digest = "sha256:incoming-source"
        source.conflict.conflict_id = "C000002"
        request = replace(source.request, task_dir=task, skills_dir=task / "skills", diagnosis_config=None)
        source.request = request
        return {"id": "source-incoming", "role": "source", "source": source, "request": request,
                "pack": task / "skills", "target_relative_path": "table/SKILL.md", "incoming": True}

    def test_preserved_behavior_and_unchanged_verifier_accept(self):
        original_hashes = tree_hashes(self.original)
        result, preserve, repair = self.validate()
        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["cases"][0]["comparison"]["status"], "unchanged")
        self.assertEqual(result["cases"][0]["evaluation"]["baseline_status"], "absent")
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(preserve.call_count, 1)
        repair.assert_not_called()
        for request, prepared, run_dir in self.backend.calls:
            self.assertIsNone(request.diagnosis_config)
            self.assertIn("preserve duplicates", (prepared.skills_dir / "table/SKILL.md").read_text())
            self.assertEqual(prepared.task_document.read_text(), self.source.prepared.task_document.read_text())
        self.assertNotEqual(self.backend.calls[0][2], self.backend.calls[1][2])
        self.assertEqual(tree_hashes(self.original), original_hashes)

    def test_behavior_regression_rejects_even_when_verifier_stays_successful(self):
        self.adapter = UninformativeVerifierAdapter()
        (self.after / "SKILL.md").write_bytes((self.original / "SKILL.md").read_bytes())
        result, preserve, _ = self.validate()
        self.assertEqual(result["cases"][0]["comparison"]["status"], "unchanged")
        self.assertEqual(result["cases"][0]["evaluation"]["target_status"], "not_repaired")
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(preserve.call_count, 1)

    def test_unknown_markdown_in_later_case_is_rejected_before_any_execution(self):
        other = self.root / "unknown-pack"
        shutil.copytree(self.source.prepared.skills_dir, other)
        with (other / "table/SKILL.md").open("a") as stream:
            stream.write("\nUnregistered task-specific instruction.\n")
        changed = dict(self.case, id="source-unknown", pack=other)
        with self.assertRaisesRegex(ValueError, "another Skill resource version"):
            self.validate([self.case, changed])
        self.assertEqual(self.backend.calls, [])
        self.assertFalse((self.root / "validation/runs").exists())

    def test_ignored_patch_with_forged_adapter_digest_stops_before_backend(self):
        class IgnoringDeploymentAdapter(TableAdapter):
            def prepare(self, request, destination):
                prepared = super().prepare(replace(request, skills_dir=None), destination)
                # Claim the requested digest while keeping the task's original
                # Skill bytes. Acceptance cannot rely on this adapter claim.
                prepared.skill_digest = hash_manifest(tree_hashes(request.skills_dir))
                return prepared
        self.adapter = IgnoringDeploymentAdapter()
        result, preserve, repair = self.validate()
        self.assertEqual(result["decision"], "pending")
        self.assertEqual(self.backend.calls, [])
        preserve.assert_not_called()
        repair.assert_not_called()
        directory = self.root / "validation/runs/source-old-baseline-000"
        record = json.loads((directory / "result.json").read_text())
        self.assertEqual(record["status"], "preparation_error")
        check = json.loads((directory / "skill_deployment_verification.json").read_text())
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["reported_skill_digest"], check["expected_skill_digest"])
        self.assertNotEqual(check["actual_skill_digest"], check["expected_skill_digest"])

    def test_changed_actual_task_prompt_prevents_acceptance_before_evaluation(self):
        self.adapter = PromptChangingAdapter()
        result, preserve, repair = self.validate()
        self.assertEqual(result["decision"], "pending")
        self.assertEqual(result["cases"][0]["error"], "Actual task prompts differ")
        self.assertEqual(len(self.backend.calls), 2)
        preserve.assert_not_called()
        repair.assert_not_called()

    def test_different_backend_versions_remain_pending_without_evaluation(self):
        class ChangingVersionBackend(TableBackend):
            def run(self, request, task, run_dir, environment):
                result = super().run(request, task, run_dir, environment)
                result.backend_version = "backend-1" if "baseline" in run_dir.name else "backend-2"
                return result
        self.backend = ChangingVersionBackend()
        result, preserve, repair = self.validate()
        self.assertEqual(result["decision"], "pending")
        self.assertEqual(result["cases"][0]["error"], "Baseline and candidate backend versions differ")
        self.assertEqual(len(self.backend.calls), 2)
        preserve.assert_not_called()
        repair.assert_not_called()

    def test_changed_protected_task_file_prevents_acceptance(self):
        self.adapter = ProtectedInputChangingAdapter()
        result, preserve, repair = self.validate()
        self.assertEqual(result["decision"], "pending")
        self.assertEqual(result["cases"][0]["error"], "Protected inputs or deployed Skill digests differ")
        preserve.assert_not_called()
        repair.assert_not_called()

    def test_combination_requires_old_repair_preservation_and_new_repair_improvement(self):
        self.backend = DifferentTaskBackend()
        with (self.after / "SKILL.md").open("a") as stream:
            stream.write("\nAlso handle incoming case.\n")
        result, preserve, repair = self.validate([self.case, self.incoming_case()], mode="combination")
        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(len(self.backend.calls), 4)
        records = {record["case_id"]: record for record in result["cases"]}
        self.assertEqual(records["source-old"]["mode"], "preservation")
        self.assertEqual(records["source-old"]["comparison"]["status"], "unchanged")
        self.assertEqual(records["source-incoming"]["mode"], "repair")
        self.assertEqual(records["source-incoming"]["comparison"]["status"], "improved")
        self.assertEqual((preserve.call_count, repair.call_count), (1, 1))
        self.assertEqual(repair.call_args.args[0].conflict.conflict_id, "C000002")

    def test_incoming_improvement_cannot_hide_loss_of_old_repair(self):
        self.backend = DifferentTaskBackend()
        (self.after / "SKILL.md").write_text((self.original / "SKILL.md").read_text() + "\nHandle only new; handle incoming case.\n")
        result, _, _ = self.validate([self.case, self.incoming_case()], mode="combination")
        self.assertEqual(result["decision"], "rejected")
        records = {record["case_id"]: record for record in result["cases"]}
        self.assertEqual(records["source-incoming"]["comparison"]["status"], "improved")
        self.assertEqual(records["source-old"]["evaluation"]["target_status"], "not_repaired")

    def test_regression_requests_with_distinct_models_are_not_deduplicated(self):
        requests = []
        for index, model in enumerate(("model-a", "model-b")):
            path = self.root / f"regression-{index}.json"
            write_json(path, replace(self.case["request"], model=model))
            requests.append({"role": "regression", "request_path": str(path)})
        incoming = SimpleNamespace(queue_digest="queue-1", source=self.source,
            patch=SimpleNamespace(target=self.source.conflict.target, conflict_id=self.source.conflict.conflict_id),
            cases=requests)
        cases = collect_cases([], incoming, self.config, self.root / "cases",
                              adapter_factory=lambda _: self.adapter)
        regressions = [case for case in cases if case["role"] == "regression"]
        self.assertEqual({case["request"].model for case in regressions}, {"model-a", "model-b"})
        self.assertEqual(len(regressions), 2)


if __name__ == "__main__":
    unittest.main()
