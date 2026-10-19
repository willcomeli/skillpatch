"""Stage 2 evidence isolation and coverage checks without models or task runs."""

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
from skillpatch.model_client import DiagnosisConfig, load_diagnosis_config
from skillpatch.stages.induction import generate_patch
from skillpatch.stages.patch_evaluation import evaluate_target, validate_evaluation
from skillpatch.storage import jsonable, write_json
from test_discovery_validation import make_context, make_payload


def evaluation():
    return {"baseline_status": "present", "target_status": "repaired",
            "explanation": "The execution record shows that the candidate preserved duplicate rows.",
            "baseline_evidence": [{"evidence_id": "T000002", "quote": "Removed duplicate rows."}],
            "candidate_evidence": [{"evidence_id": "T000002", "quote": "Preserved duplicate rows."}],
            "reverse_conflicts": [], "revision_action": "none"}


def evidence_call(identifier, collection, evidence_id="T000002"):
    return {"id": identifier, "type": "function", "function": {"name": "read_evidence",
        "arguments": json.dumps({"collection": collection, "evidence_id": evidence_id})}}


class TurnClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def turn(self, config, messages, environment, output, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        write_json(output / "model_call.json", {"status": "completed",
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        return next(self.outputs)


class Stage2EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_context = make_context()
        self.source = SimpleNamespace(context=self.source_context,
            conflict=validate_report(make_payload(), self.source_context).conflicts[0])
        self.baseline = make_context()
        self.candidate = make_context()
        self.candidate.run_id = "candidate-run"
        self.candidate.evidence[3] = replace(self.candidate.evidence[3], text="Preserved duplicate rows.")
        self.config = DiagnosisConfig("fixture", "https://not-called.invalid", "FIXTURE_KEY",
                                      Path(__file__).resolve().parents[1] / "prompts/patch_evaluation.md")
        self.calls = []

    def views(self, contexts=None):
        return {name: EvidenceViews(copy.deepcopy(context), self.root / "views" / name)
                for name, context in (contexts or {"baseline": self.baseline,
                                                   "candidate": self.candidate}).items()}

    def show(self, view, identifiers=None):
        view.observe([item for item in view.context.evidence
                      if identifiers is None or item.evidence_id in identifiers])
        view.commit_visible()

    def validate(self, payload, views):
        return validate_evaluation(payload, views["baseline"].context,
                                   views["candidate"].context, views=views)

    def session(self, payload=None, *, fallback=False, leave_unread=None):
        def recorded(config, data, contexts, output, environment, **kwargs):
            self.calls.append({"data": data, "contexts": contexts, **kwargs})
            views = {name: EvidenceViews(copy.deepcopy(context), output / "evidence" / name)
                     for name, context in contexts.items()}
            if not fallback:
                for name, view in views.items():
                    self.show(view, {item.evidence_id for item in view.context.evidence
                                     if (name, item.evidence_id) != leave_unread})
            result = kwargs["budget_fallback"]("inspection budget exhausted") if fallback else copy.deepcopy(payload)
            kwargs["validate"](result, views)
            return result
        return recorded

    def selectors(self):
        payload = evaluation()
        for name in ("baseline", "candidate"):
            payload[name + "_evidence"] = [{"collection": name, "evidence_id": "T000002"}]
        return payload

    def test_selector_session_archives_resolved_quotes(self):
        client = TurnClient([{"role": "assistant", "content": json.dumps(self.selectors())}])
        output = self.root / "selector-session"
        result = evaluate_target(self.source, self.baseline, self.candidate, self.config,
                                 output, {}, client=client)
        self.assertEqual(result.target_status, "repaired")
        saved = json.loads((output / "evaluation.json").read_text())
        self.assertEqual(saved["evaluation"]["candidate_evidence"][0]["quote"],
                         "Preserved duplicate rows.")
        self.assertIn('"collection"', (output / "model_output.txt").read_text())

    def test_selectors_extract_each_runs_original_text(self):
        views = self.views()
        for view in views.values():
            self.show(view)
        payload = self.selectors()
        result = self.validate(payload, views)
        self.assertEqual(result.baseline_evidence[0]["quote"], "Removed duplicate rows.")
        self.assertEqual(result.candidate_evidence[0]["quote"], "Preserved duplicate rows.")
        self.assertNotIn("quote", payload["candidate_evidence"][0])

    def test_selectors_reject_wrong_run_and_invalid_ranges(self):
        views = self.views()
        for view in views.values():
            self.show(view)
        for update in ({"collection": "baseline"}, {"evidence_id": "T999999"},
                       {"start_char": -1, "end_char": 3},
                       {"start_char": 0, "end_char": 999},
                       {"start_char": True, "end_char": 3},
                       {"start_char": 3, "end_char": 3}, {"start_char": 0}):
            with self.subTest(update=update):
                payload = self.selectors()
                payload["candidate_evidence"][0].update(update)
                with self.assertRaises(ValueError):
                    self.validate(payload, views)

    def test_selector_range_must_be_displayed_even_if_text_repeats(self):
        views = self.views()
        self.show(views["baseline"])
        item = views["candidate"].context.evidence[3]
        views["candidate"].context.evidence[3] = replace(item, text="same same")
        views["candidate"].presented["T000002"] = [(0, 4)]
        payload = self.selectors()
        payload["candidate_evidence"][0].update(start_char=5, end_char=9)
        with self.assertRaisesRegex(ValueError, "undisplayed"):
            self.validate(payload, views)
        payload["candidate_evidence"][0].update(start_char=0, end_char=4)
        self.assertEqual(self.validate(payload, views).candidate_evidence[0]["quote"], "same")

    def test_selector_preserves_tool_line_prefixes(self):
        views = self.views()
        item = views["candidate"].context.evidence[3]
        text = "+1| first\n+2| second"
        views["candidate"].context.evidence[3] = replace(item, text=text)
        for view in views.values():
            self.show(view)
        payload = self.selectors()
        payload["candidate_evidence"][0].update(start_char=0, end_char=len(text))
        self.assertEqual(self.validate(payload, views).candidate_evidence[0]["quote"], text)

    def test_quote_from_other_run_with_same_id_is_rejected(self):
        views = self.views()
        for view in views.values():
            self.show(view)
        payload = evaluation()
        payload["candidate_evidence"] = payload["baseline_evidence"]
        with self.assertRaisesRegex(ValueError, "exact quote"):
            self.validate(payload, views)

    def test_real_but_undisplayed_quote_is_not_accepted(self):
        views = self.views()
        self.show(views["baseline"])
        self.show(views["candidate"], {"T000003"})
        with self.assertRaisesRegex(ValueError, "candidate.*undisplayed.*T000002"):
            self.validate(evaluation(), views)

    def test_partially_read_later_event_does_not_block_a_displayed_citation(self):
        self.candidate.evidence[4] = replace(self.candidate.evidence[4], text="Saved output.\nUnrelated later bookkeeping.")
        views = self.views()
        self.show(views["baseline"])
        self.show(views["candidate"], {"T000002"})
        views["candidate"].call("read_evidence", {"evidence_id": "T000003", "max_chars": 13})
        views["candidate"].commit_visible()
        # Provenance validation does not prove that the model checked all relevant counterevidence.
        self.assertEqual(self.validate(evaluation(), views).target_status, "repaired")
        self.assertFalse(views["candidate"].fully_presented(views["candidate"].context.evidence[4]))

    def test_displayed_quote_in_partial_event_does_not_require_remainder_or_later_events(self):
        self.candidate.evidence[3] = replace(self.candidate.evidence[3],
            text="Preserved duplicate rows.\n" + "Unrelated diagnostic detail.\n" * 1000)
        views = self.views()
        self.show(views["baseline"], {"T000002"})
        views["candidate"].call("read_evidence", {"evidence_id": "T000002",
            "max_chars": len("Preserved duplicate rows.")})
        views["candidate"].commit_visible()
        self.assertEqual(self.validate(evaluation(), views).target_status, "repaired")
        self.assertFalse(views["candidate"].fully_presented(views["candidate"].context.evidence[3]))
        self.assertNotIn("T000003", views["candidate"].presented)

    def test_undisplayed_quote_in_partially_read_event_is_rejected(self):
        self.candidate.evidence[3] = replace(self.candidate.evidence[3],
            text="Visible prefix.\nPreserved duplicate rows.")
        views = self.views()
        self.show(views["baseline"])
        views["candidate"].call("read_evidence", {"evidence_id": "T000002", "max_chars": 15})
        views["candidate"].commit_visible()
        with self.assertRaisesRegex(ValueError, "candidate.*undisplayed.*T000002"):
            self.validate(evaluation(), views)

    def test_supported_judgment_still_requires_trajectory_evidence(self):
        views = self.views()
        for view in views.values():
            self.show(view)
        payload = evaluation()
        payload["candidate_evidence"] = [{"evidence_id": "Q000001", "quote": "Preserve duplicate records"}]
        with self.assertRaisesRegex(ValueError, "candidate.*trajectory evidence"):
            self.validate(payload, views)

    def test_unrelated_initial_metadata_need_not_be_read(self):
        self.baseline.evidence[2] = replace(self.baseline.evidence[2], text="System Prompt: " + "x" * 100000)
        views = self.views()
        for view in views.values():
            self.show(view, {"T000002", "T000003"})
        self.assertEqual(self.validate(evaluation(), views).baseline_status, "present")
        self.assertFalse(views["baseline"].fully_presented(views["baseline"].context.evidence[2]))

    def test_incomplete_execution_cannot_establish_repair(self):
        self.candidate.trajectory_complete = False
        views = self.views()
        for view in views.values():
            self.show(view)
        payload = evaluation()
        with self.assertRaisesRegex(ValueError, "candidate.*incomplete execution"):
            self.validate(payload, views)

    def test_incomplete_execution_requires_uncertain_status_without_views(self):
        for collection, field in (("baseline", "baseline_status"), ("candidate", "target_status")):
            for complete in (False, None):
                with self.subTest(collection=collection, complete=complete):
                    baseline, candidate = copy.deepcopy(self.baseline), copy.deepcopy(self.candidate)
                    context = baseline if collection == "baseline" else candidate
                    context.trajectory_complete = complete
                    payload = evaluation()
                    with self.assertRaisesRegex(ValueError, collection + ".*incomplete execution"):
                        validate_evaluation(payload, baseline, candidate)
                    payload[field] = "uncertain"
                    payload["explanation"] = "The execution record is incomplete."
                    self.assertEqual(getattr(validate_evaluation(payload, baseline, candidate), field), "uncertain")

    def test_fresh_evaluation_rejects_removed_uncertainties_field(self):
        payload = evaluation()
        payload["uncertainties"] = []
        with self.assertRaisesRegex(ValueError, "fields do not match"):
            validate_evaluation(payload, self.baseline, self.candidate)

    def test_uncertainty_can_be_reported_without_inventing_behavior(self):
        views = self.views()
        payload = evaluation()
        payload.update(baseline_status="uncertain", target_status="uncertain",
                       baseline_evidence=[], candidate_evidence=[], explanation="Behavior records have not been read.")
        self.assertEqual(self.validate(payload, views).target_status, "uncertain")

    def test_reverse_conflict_cannot_quote_an_unread_skill(self):
        views = self.views()
        for view in views.values():
            self.show(view, {"T000002", "T000003"})
        payload = evaluation()
        payload["reverse_conflicts"] = [{"type": "under_use", "explanation": "A possible new conflict requires investigation.",
            "evidence": [*payload["candidate_evidence"], {"evidence_id": "S000001",
                         "quote": "Remove duplicate records when uniqueness is required."}]}]
        with self.assertRaisesRegex(ValueError, "candidate.*undisplayed.*S000001"):
            self.validate(payload, views)

    def test_normalized_quotes_and_digests_preserve_original_run_inputs(self):
        self.baseline.evidence[3] = replace(self.baseline.evidence[3], text="\x1b[31mRemoved duplicate rows.\x1b[0m")
        before = jsonable(self.baseline)
        output = self.root / "evaluation"
        with patch("skillpatch.stages.patch_evaluation.recorded_evidence_session", self.session(evaluation())):
            result = evaluate_target(self.source, self.baseline, self.candidate, self.config, output, {},
                                     session_timeout_sec=17)
        self.assertEqual(result.target_status, "repaired")
        saved = json.loads((output / "evaluation.json").read_text())
        self.assertNotEqual(saved["baseline_digest"], self.baseline.input_digest)
        self.assertEqual(saved["original_input_digests"]["baseline"], self.baseline.input_digest)
        self.assertEqual(jsonable(self.baseline), before)
        self.assertEqual(set(self.calls[0]["contexts"]), {"source", "baseline", "candidate"})
        self.assertEqual(self.calls[0]["session_timeout_sec"], 17)
        self.assertNotIn("\x1b[31m", self.calls[0]["data"])

    def test_generation_receives_compact_claim_and_original_version_binding(self):
        payload = {"status": "candidate", "reason": "Clarify the original clause applicability conditions.",
                   "when_to_use": "When uniqueness is required.",
                   "how_to_interpret": "Preserve duplicates when the task requires them."}
        self.source_context.evidence[2] = replace(self.source_context.evidence[2], text="unrelated large metadata" * 1000)
        with patch("skillpatch.stages.induction.recorded_evidence_session", self.session(payload)):
            generated, _ = generate_patch(self.source, self.config, self.root / "generation", {},
                                          history=[{"decision": "pending"}], session_timeout_sec=19)
        self.assertEqual(generated.source_input_digest, self.source_context.input_digest)
        self.assertEqual(generated.target, self.source.conflict.target)
        self.assertNotIn("unrelated large metadata", self.calls[0]["data"])
        self.assertEqual(self.calls[0]["priority_ids"], {"source": ["S000001"]})
        self.assertEqual(self.calls[0]["session_timeout_sec"], 19)
        self.assertTrue((self.root / "generation/patch.json").is_file())

    def test_generation_does_not_require_unquoted_later_source_events(self):
        payload = {"status": "candidate", "reason": "A candidate supplement.", "when_to_use": "Condition.",
                   "how_to_interpret": "Interpretation."}
        with patch("skillpatch.stages.induction.recorded_evidence_session",
                   self.session(payload, leave_unread=("source", "T000003"))):
            generated, _ = generate_patch(self.source, self.config, self.root / "generation", {})
        self.assertIsNotNone(generated)
        self.assertTrue((self.root / "generation/patch.json").exists())

    def test_generation_requires_displayed_source_behavior_quote(self):
        payload = {"status": "candidate", "reason": "A candidate supplement.", "when_to_use": "Condition.",
                   "how_to_interpret": "Interpretation."}
        with patch("skillpatch.stages.induction.recorded_evidence_session",
                   self.session(payload, leave_unread=("source", "T000002"))):
            generate_patch(self.source, self.config, self.root / "generation", {})
        self.assertTrue((self.root / "generation/patch.json").exists())

    def test_generation_cannot_establish_a_patch_from_incomplete_execution(self):
        self.source_context.trajectory_complete = False
        payload = {"status": "candidate", "reason": "A candidate supplement.", "when_to_use": "Condition.",
                   "how_to_interpret": "Interpretation."}
        with patch("skillpatch.stages.induction.recorded_evidence_session", self.session(payload)):
            generate_patch(self.source, self.config, self.root / "generation", {})
        self.assertTrue((self.root / "generation/patch.json").exists())

    def test_budget_fallback_stays_uncertain_and_has_no_fabricated_citations(self):
        with patch("skillpatch.stages.patch_evaluation.recorded_evidence_session", self.session(fallback=True)):
            result = evaluate_target(self.source, self.baseline, self.candidate, self.config,
                                     self.root / "evaluation", {})
        self.assertEqual((result.baseline_status, result.target_status), ("uncertain", "uncertain"))
        self.assertEqual(result.baseline_evidence + result.candidate_evidence + result.reverse_conflicts, [])
        self.assertIn("inspection budget exhausted", result.explanation)
        self.assertNotIn("uncertainties", jsonable(result))
        with patch("skillpatch.stages.induction.recorded_evidence_session", self.session(fallback=True)):
            generated, payload = generate_patch(self.source, self.config, self.root / "generation", {})
        self.assertIsNone(generated)
        self.assertEqual(payload["status"], "insufficient_evidence")
        self.assertFalse((self.root / "generation/patch.json").exists())

    def test_real_session_reads_three_collections_and_saves_evaluation(self):
        client = TurnClient([
            {"role": "assistant", "content": None, "tool_calls": [
                evidence_call("source-read", "source"), evidence_call("baseline-read", "baseline"),
                evidence_call("candidate-read", "candidate")]},
            {"role": "assistant", "content": json.dumps(evaluation())},
        ])
        before = jsonable(self.baseline)
        output = self.root / "actual-evaluation"
        result = evaluate_target(self.source, self.baseline, self.candidate, self.config,
                                 output, {}, client=client)
        self.assertEqual(result.target_status, "repaired")
        self.assertEqual(len(client.calls), 2)
        responses = {item["tool_call_id"]: json.loads(item["content"])
                     for item in client.calls[1][0] if item["role"] == "tool"}
        self.assertEqual(responses["source-read"]["collection"], "source")
        self.assertEqual(responses["baseline-read"]["result"]["text"], "Removed duplicate rows.")
        self.assertEqual(responses["candidate-read"]["result"]["text"], "Preserved duplicate rows.")
        self.assertEqual(json.loads((output / "evaluation.json").read_text())["evaluation"]["target_status"], "repaired")
        self.assertEqual(json.loads((output / "session.json").read_text())["tool_calls"], 3)
        self.assertEqual(jsonable(self.baseline), before)

    def test_real_evaluation_budget_fallback_leaves_stage2_pending_without_queue(self):
        from skillpatch.stages.patch_validation import run_stage2
        from test_stage2 import Stage2Fixture, candidate

        fixture = Stage2Fixture("test_prepare_only_never_calls_model_or_execution")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        client = TurnClient([
            {"role": "assistant", "content": json.dumps(candidate())},
            {"role": "assistant", "content": None,
             "tool_calls": [evidence_call("first-read", "baseline")]},
            {"role": "assistant", "content": None,
             "tool_calls": [evidence_call("over-budget", "candidate")]},
        ])
        output = fixture.root / "budget-episode"
        def bounded_config(path):
            return replace(load_diagnosis_config(path), max_rounds=2, max_tool_calls=1, max_report_repairs=0)
        with patch("skillpatch.stages.patch_validation.load_diagnosis_config", bounded_config):
            result = run_stage2(fixture.diagnosis, "C000001", fixture.config, output,
                {"FIXTURE_KEY": "fixture-secret-value"}, client=client,
                adapter_factory=lambda _: fixture.adapter, backend_factory=lambda _: fixture.backend)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["decision"], "accepted", result)
        self.assertTrue(result["ready_for_stage3"])
        self.assertTrue((output / "stage3_queue.json").exists())
        evaluation_dir = output / "attempts/000/evaluation/source/000"
        saved = json.loads((evaluation_dir / "evaluation.json").read_text())["evaluation"]
        self.assertNotIn("uncertainties", saved)
        self.assertEqual((saved["baseline_status"], saved["target_status"]), ("uncertain", "uncertain"))
        self.assertEqual(saved["baseline_evidence"] + saved["candidate_evidence"], [])
        self.assertTrue((evaluation_dir / "budget_fallback.json").is_file())
        self.assertEqual(json.loads((evaluation_dir / "session.json").read_text())["status"], "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
