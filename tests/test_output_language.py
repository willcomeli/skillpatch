"""New authored output is English; source evidence and legacy archives are intact."""

import copy
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from skillpatch.discovery_validation import validate_report, validate_saved_report
from skillpatch.model_client import DiagnosisConfig
from skillpatch.output_language import validate_output_language
from skillpatch.patches import validate_generation
from skillpatch.stages.consolidation_evaluation import evaluate_preservation
from skillpatch.stages.consolidation_model import propose_consolidation, validate_proposal
from skillpatch.stages.discovery import render_report, run_discovery
from skillpatch.stages.induction import generate_patch
from skillpatch.stages.patch_evaluation import evaluate_target, validate_evaluation
from test_discovery import FakeClient
from test_discovery_validation import make_context, make_payload
from test_stage2_evidence import TurnClient, evaluation


def generation():
    return {"status": "candidate", "reason": "Clarify the existing applicability boundary.",
            "when_to_use": "When uniqueness is required.",
            "how_to_interpret": "Preserve duplicates when the task requires them."}


class LanguageContractTests(unittest.TestCase):
    def test_rejects_every_authored_discovery_field_but_not_reference_text(self):
        payload = {"summary": "Summary.", "coverage_notes": ["Coverage."],
                   "conflicts": [{"diagnosis": "Diagnosis.", "uncertainty": ["Uncertainty."],
                                  "target": {"quote": "原始技能"},
                                  "decision_evidence": [{"quote": "任务原文"}]}],
                   "other_findings": [{"description": "An observed issue.",
                                       "evidence": [{"quote": "执行轨迹原文"}]}]}
        validate_output_language(payload, "discovery")
        mutations = [lambda p: p.update(summary="新诊断"),
                     lambda p: p.update(coverage_notes=["未完成"]),
                     lambda p: p["conflicts"][0].update(diagnosis="新诊断"),
                     lambda p: p["conflicts"][0].update(uncertainty=["不确定"]),
                     lambda p: p["other_findings"][0].update(description="其他问题")]
        for mutate in mutations:
            changed = copy.deepcopy(payload)
            mutate(changed)
            with self.assertRaisesRegex(ValueError, "English"):
                validate_output_language(changed, "discovery")

    def test_generation_and_consolidation_guard_every_authored_field(self):
        for kind in ("generation", "consolidation"):
            for field in ("reason", "when_to_use", "how_to_interpret"):
                with self.subTest(kind=kind, field=field):
                    payload = generation()
                    payload[field] = "English followed by 中文."
                    with self.assertRaisesRegex(ValueError, field):
                        validate_output_language(payload, kind)

    def test_evaluation_guard_includes_explanation_and_reverse_conflicts(self):
        for field in ("explanation", "reverse_conflicts"):
            payload = evaluation()
            payload[field] = ({"explanation": "结论",
                               "reverse_conflicts": [{"explanation": "新的误用", "evidence": []}]}[field])
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                validate_output_language(payload, "evaluation")

    def test_chinese_exact_quotes_and_bound_metadata_remain_unchanged(self):
        context, payload = make_context(), make_payload()
        context.evidence[0] = replace(context.evidence[0], text="保留重复记录。")
        context.evidence[1] = replace(context.evidence[1], text="仅在要求唯一记录时删除重复记录。")
        context.evidence[3] = replace(context.evidence[3], text="已删除重复记录。")
        conflict = payload["conflicts"][0]
        conflict["target"]["quote"] = context.evidence[1].text
        conflict["decision_evidence"][0]["quote"] = context.evidence[0].text
        conflict["behavior_evidence"][0]["quote"] = context.evidence[3].text
        original = asdict(context)
        report = validate_report(payload, context, require_english=True)
        self.assertEqual(report.conflicts[0].target.quote, context.evidence[1].text)
        self.assertEqual(report.conflicts[0].behavior_evidence[0].quote, context.evidence[3].text)
        self.assertEqual(asdict(context), original)
        self.assertIn("Applicability reason:", report.conflicts[0].diagnosis)

    def test_historical_chinese_report_reopens_without_translation(self):
        context, payload = make_context(), make_payload()
        payload["summary"] = "归档诊断摘要。"
        payload["conflicts"][0]["applicability_reason"] = "任务要求保留重复记录。"
        report = validate_report(payload, context)
        saved = asdict(report)
        self.assertEqual(asdict(validate_saved_report(saved, context)), saved)
        self.assertIn("适用性", report.conflicts[0].diagnosis)
        self.assertEqual(validate_report(payload, context, require_english=True).summary, payload["summary"])

    def test_archived_patch_and_evaluation_contracts_remain_language_compatible(self):
        context = make_context()
        source = SimpleNamespace(context=context, conflict=validate_report(make_payload(), context).conflicts[0])
        patch_payload = dict(generation(), reason="旧补丁说明。")
        self.assertIsNotNone(validate_generation(patch_payload, source, 0))
        proposal = dict(patch_payload, action="merge")
        proposal.pop("status")
        self.assertEqual(validate_proposal(proposal)["reason"], "旧补丁说明。")
        candidate = copy.deepcopy(context)
        candidate.evidence[3] = replace(candidate.evidence[3], text="Preserved duplicate rows.")
        payload = dict(evaluation(), explanation="历史评价说明。")
        self.assertEqual(validate_evaluation(payload, context, candidate).explanation, payload["explanation"])
        with self.assertRaisesRegex(ValueError, "English"):
            validate_evaluation(payload, context, candidate, require_english=True)

    def test_markdown_shell_and_new_legacy_conversion_are_english(self):
        context = make_context()
        report = validate_report(make_payload(), context, require_english=True)
        markdown = render_report(report, context)
        self.assertIn("# Stage 1 Diagnosis Report", markdown)
        self.assertNotRegex(markdown, "[\u4e00-\u9fff]")


class LanguageSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        context = make_context()
        self.source = SimpleNamespace(context=context,
            conflict=validate_report(make_payload(), context).conflicts[0])
        self.baseline, self.candidate = make_context(), make_context()
        self.candidate.evidence[3] = replace(self.candidate.evidence[3], text="Preserved duplicate rows.")
        self.config = DiagnosisConfig("fixture", "https://not-called.invalid", "FIXTURE_KEY",
            Path(__file__).resolve().parents[1] / "prompts/induction.md")

    def client(self, payload, field):
        invalid = dict(payload, **{field: "需要使用英文。"})
        return TurnClient([{"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}
                           for value in (invalid, payload)])

    def assert_repaired(self, client, output, valid_payload, field):
        self.assertEqual(len(client.calls), 2)
        self.assertIn("English", client.calls[1][0][-1]["content"])
        first = json.loads((output / "calls/001/assistant.json").read_text())
        self.assertEqual(json.loads(first["content"])[field], "需要使用英文。")
        self.assertEqual(json.loads((output / "payload.json").read_text()), valid_payload)

    def test_generation_retries_chinese_reason_and_keeps_raw_response(self):
        payload, output = generation(), self.root / "generation"
        client = self.client(payload, "reason")
        result, _ = generate_patch(self.source, self.config, output, {}, client=client)
        self.assertIsNotNone(result)
        self.assertEqual(len(client.calls), 1)

    def test_evaluation_retries_chinese_explanation(self):
        payload, output = evaluation(), self.root / "evaluation"
        client = self.client(payload, "explanation")
        result = evaluate_target(self.source, self.baseline, self.candidate, self.config, output, {}, client=client)
        self.assertEqual(result.explanation, "需要使用英文。")
        self.assertEqual(len(client.calls), 1)

    def test_preservation_retries_chinese_explanation(self):
        payload, output = evaluation(), self.root / "preservation"
        payload.update(baseline_status="absent", baseline_evidence=payload["candidate_evidence"])
        client = self.client(payload, "explanation")
        result = evaluate_preservation(self.source, self.candidate, self.candidate, self.config, output, {}, client=client)
        self.assertEqual(result.explanation, payload["explanation"])
        self.assert_repaired(client, output, payload, "explanation")

    def test_consolidation_retries_chinese_reason(self):
        payload, output = generation(), self.root / "consolidation"
        payload.pop("status")
        payload["action"] = "merge"
        entry = {"target": {"target_id": "original-step", "skill_key": "table", "base_digest": "original"},
                 "when_to_use": "When uniqueness is required.", "how_to_interpret": "Preserve requested duplicates."}
        client = self.client(payload, "reason")
        result = propose_consolidation(self.source, [entry], entry, self.config, output, {}, client=client)
        self.assertEqual(result, payload)
        self.assert_repaired(client, output, payload, "reason")

    def test_offline_discovery_accepts_language_without_hard_gate(self):
        payload = make_payload()
        payload["summary"] = "新生成的中文摘要。"
        run_dir, output = self.root / "run", self.root / "diagnosis"
        run_dir.mkdir()
        result = run_discovery(run_dir, self.config, output, {}, client=FakeClient(response=payload),
                               input_builder=lambda _: self.source.context)
        self.assertEqual(result["status"], "completed")
        self.assertTrue((output / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
