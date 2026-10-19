"""Preservation accepts a maintained repair, without requiring another improvement."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skillpatch.discovery_validation import validate_report
from skillpatch.evidence_views import EvidenceViews
from skillpatch.model_client import DiagnosisConfig
from skillpatch.patch_models import FeedbackComparison, TargetEvaluation
from skillpatch.stages.consolidation_evaluation import decide_preservation, evaluate_preservation
from test_discovery_validation import make_context, make_payload
from test_stage2_evidence import TurnClient


def preserved():
    return {"baseline_status": "absent", "target_status": "repaired",
            "explanation": "Both collections preserve task-required duplicate rows, retaining the previous repair.",
            "baseline_evidence": [{"evidence_id": "T000002", "quote": "Preserved duplicate rows."}],
            "candidate_evidence": [{"evidence_id": "T000002", "quote": "Preserved duplicate rows."}],
            "reverse_conflicts": [], "revision_action": "none"}


class PreservationDecisionTests(unittest.TestCase):
    def decide(self, payload=None, comparisons=None, **kwargs):
        return decide_preservation([TargetEvaluation(**(payload or preserved()))],
            comparisons or [FeedbackComparison("unchanged")], **kwargs)

    def test_unchanged_and_improved_both_preserve(self):
        for status in ("unchanged", "improved"):
            with self.subTest(status=status):
                result = self.decide(comparisons=[FeedbackComparison(status)])
                self.assertEqual(result["decision"], "accepted")
                self.assertTrue(result["ready_for_activation"])

    def test_unmerged_baseline_conflict_cannot_establish_preservation(self):
        result = self.decide(dict(preserved(), baseline_status="present"))
        self.assertEqual(result["decision"], "pending")
        self.assertFalse(result["ready_for_activation"])
        self.assertIn("baseline", result["reason"])

    def test_uncertain_status_revision_and_unavailable_metrics_do_not_activate(self):
        for payload in (dict(preserved(), baseline_status="uncertain"),
                        dict(preserved(), target_status="uncertain"),
                        dict(preserved(), revision_action="revise_patch")):
            self.assertEqual(self.decide(payload)["decision"], "pending")
        self.assertEqual(self.decide(comparisons=[FeedbackComparison("unavailable")])["decision"], "pending")

    def test_known_behavior_or_verifier_regressions_reject(self):
        self.assertEqual(self.decide(dict(preserved(), target_status="not_repaired"))["decision"], "rejected")
        self.assertEqual(self.decide(comparisons=[FeedbackComparison("regressed")])["decision"], "rejected")
        payload = dict(preserved(), reverse_conflicts=[{"type": "misuse"}])
        self.assertEqual(self.decide(payload)["decision"], "rejected")

    def test_regression_tasks_also_require_behavior_and_objective_evidence(self):
        good = TargetEvaluation(**preserved())
        self.assertEqual(self.decide(regression_comparisons=[FeedbackComparison("unchanged")],
                                     regression_evaluations=[good])["decision"], "accepted")
        self.assertEqual(self.decide(regression_comparisons=[FeedbackComparison("unchanged")])["decision"], "pending")
        bad = replace(good, baseline_status="present")
        self.assertEqual(self.decide(regression_comparisons=[FeedbackComparison("improved")],
                                     regression_evaluations=[bad])["decision"], "pending")
        self.assertEqual(self.decide(regression_comparisons=[FeedbackComparison("regressed")],
                                     regression_evaluations=[good])["decision"], "rejected")

    def test_missing_execution_or_source_pairs_stay_pending(self):
        self.assertEqual(self.decide(executions_ok=False)["decision"], "pending")
        self.assertEqual(decide_preservation([], [FeedbackComparison("unchanged")])["decision"], "pending")


class PreservationSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        context = make_context()
        self.source = SimpleNamespace(context=context,
            conflict=validate_report(make_payload(), context).conflicts[0])
        self.baseline, self.candidate = make_context(), make_context()
        for name, item in (("baseline", self.baseline), ("candidate", self.candidate)):
            item.run_id = name
            item.evidence[3] = replace(item.evidence[3], text="Preserved duplicate rows.")
        self.config = DiagnosisConfig("fixture", "https://not-called.invalid", "FIXTURE_KEY",
            Path(__file__).resolve().parents[1] / "prompts/consolidation_evaluation.md")

    def session(self, *, unread=None, partial=None, fallback=False):
        def recorded(config, data, contexts, output, environment, **kwargs):
            views = {name: EvidenceViews(copy.deepcopy(context), output / "collections" / name)
                     for name, context in contexts.items()}
            self.views = views
            if not fallback:
                for name, view in views.items():
                    view.observe([item for item in view.context.evidence if (name, item.evidence_id) != unread
                                  and (partial is None or (name, item.evidence_id) != partial[:2])])
                    if partial is not None and name == partial[0]:
                        _, identifier, quote = partial
                        item = next(item for item in view.context.evidence if item.evidence_id == identifier)
                        view.call("read_evidence", {"evidence_id": identifier,
                                  "start_char": item.text.index(quote), "max_chars": len(quote)})
                    view.commit_visible()
            payload = kwargs["budget_fallback"]("tool limit") if fallback else preserved()
            kwargs["validate"](payload, views)
            return payload
        return recorded

    def test_preservation_session_resolves_selectors(self):
        payload = preserved()
        for name in ("baseline", "candidate"):
            payload[name + "_evidence"] = [
                {"collection": name, "evidence_id": ref["evidence_id"]}
                for ref in payload[name + "_evidence"]]
        client = TurnClient([{"role": "assistant", "content": json.dumps(payload)}])
        result = evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                       self.root / "selectors", {}, client=client)
        self.assertEqual(result.target_status, "repaired")
        self.assertTrue(result.candidate_evidence[0]["quote"])

    def test_real_session_with_fake_model_uses_preservation_prompt_and_isolated_runs(self):
        client = TurnClient([{"role": "assistant", "content": json.dumps(preserved())}])
        result = evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                        self.root / "evaluation", {}, client=client)
        self.assertEqual((result.baseline_status, result.target_status), ("absent", "repaired"))
        messages = client.calls[0][0]
        self.assertIn("BASELINE already includes", messages[0]["content"])
        self.assertIn("baseline already contains", messages[1]["content"])
        saved = json.loads((self.root / "evaluation/evaluation.json").read_text())
        self.assertEqual(saved["mode"], "preservation")
        self.assertEqual(set(saved["input_digests"]), {"source", "baseline", "candidate"})
        self.assertNotIn("uncertainties", saved["evaluation"])

    def test_unread_uncited_later_candidate_event_does_not_block_preservation(self):
        with patch("skillpatch.stages.consolidation_evaluation.recorded_evidence_session",
                   self.session(unread=("candidate", "T000003"))):
            result = evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                           self.root / "evaluation", {})
        self.assertEqual((result.baseline_status, result.target_status), ("absent", "repaired"))
        self.assertNotIn("T000003", self.views["candidate"].presented)

    def test_displayed_candidate_quote_does_not_require_reading_the_entire_event(self):
        self.candidate.evidence[3] = replace(self.candidate.evidence[3],
            text="Earlier metadata. Preserved duplicate rows. Other log output.")
        with patch("skillpatch.stages.consolidation_evaluation.recorded_evidence_session",
                   self.session(unread=("candidate", "T000003"),
                                partial=("candidate", "T000002", "Preserved duplicate rows."))):
            result = evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                           self.root / "evaluation", {})
        self.assertEqual(result.target_status, "repaired")
        self.assertFalse(self.views["candidate"].fully_presented(self.candidate.evidence[3]))

    def test_undisplayed_candidate_citation_still_blocks_preservation(self):
        with patch("skillpatch.stages.consolidation_evaluation.recorded_evidence_session",
                   self.session(unread=("candidate", "T000002"))):
            with self.assertRaisesRegex(ValueError, "candidate.*undisplayed evidence T000002"):
                evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                        self.root / "evaluation", {})

    def test_missing_original_clause_cannot_use_candidate_patch_as_criterion(self):
        with patch("skillpatch.stages.consolidation_evaluation.recorded_evidence_session",
                   self.session(unread=("source", "S000001"))):
            with self.assertRaisesRegex(ValueError, "source.*undisplayed"):
                evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                        self.root / "evaluation", {})

    def test_incomplete_current_execution_prevents_supported_judgment(self):
        self.baseline.trajectory_complete = False
        with patch("skillpatch.stages.consolidation_evaluation.recorded_evidence_session", self.session()):
            with self.assertRaisesRegex(ValueError, "baseline: incomplete execution requires baseline_status=uncertain"):
                evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                        self.root / "evaluation", {})

    def test_inspection_fallback_cannot_activate(self):
        with patch("skillpatch.stages.consolidation_evaluation.recorded_evidence_session", self.session(fallback=True)):
            result = evaluate_preservation(self.source, self.baseline, self.candidate, self.config,
                                            self.root / "evaluation", {})
        self.assertEqual((result.baseline_status, result.target_status), ("uncertain", "uncertain"))
        self.assertFalse(result.baseline_evidence or result.candidate_evidence)
        self.assertIn("tool limit", result.explanation)
        decision = decide_preservation([result], [FeedbackComparison("unchanged")])
        self.assertEqual(decision["decision"], "pending")


if __name__ == "__main__":
    unittest.main()
