"""Offline tests of citation repair guidance and unchanged evidence access."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from skillpatch.evidence_repair import build_evidence_repair_context
from skillpatch.evidence_views import EvidenceViews
from skillpatch.models import DiscoveryInput, EvidenceItem


class EvidenceRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def view(self, collection, text, *, identifier="T000021", presented=None, kind="trajectory"):
        context = DiscoveryInput(collection, "test", "task", "task-hash", "skill-hash", True,
                                 [EvidenceItem(identifier, kind, text, "trace.jsonl")], [], "")
        view = EvidenceViews(context, self.root / collection)
        if presented is not None:
            view.presented[identifier] = presented
        return view

    @staticmethod
    def payload(quote, *, collection="baseline", identifier="T000021"):
        return {f"{collection}_evidence": [{"evidence_id": identifier, "quote": quote}]}

    def repair(self, views, quote, *, error="baseline: evidence T000021 is not an exact quote from this run", **kwargs):
        value = build_evidence_repair_context(error, self.payload(quote), views, **kwargs)
        return json.loads(value)["evidence_repair_context"]

    def test_missing_diff_line_prefix_is_shown_without_relaxing_validation(self):
        text = "+1|import pandas as pd\n+2|from collections import defaultdict\n"
        quote = "import pandas as pd\nfrom collections import defaultdict"
        view = self.view("baseline", text, presented=[(0, len(text))])
        result = self.repair({"baseline": view}, quote)
        ref = result["references"][0]
        self.assertEqual(ref["excerpt"]["text"], text)
        self.assertEqual(ref["submitted_quote"], quote)
        self.assertIn("not an exact substring", ref["issue"])
        self.assertIn("restore them", ref["format_hint"])
        self.assertNotIn(quote, text)
        self.assertIn("does not establish", result["instructions"])

    def test_wrong_run_same_id_shows_declared_baseline_only(self):
        source = "Removed the rows in the original failed run."
        baseline = "Retained every row in the new baseline."
        views = {"source": self.view("source", source, presented=[(0, len(source))]),
                 "baseline": self.view("baseline", baseline, presented=[(0, len(baseline))])}
        result = self.repair(views, source)
        ref, = result["references"]
        self.assertEqual(ref["collection"], "baseline")
        self.assertEqual(ref["excerpt"]["text"], baseline)
        self.assertEqual(ref["other_displayed_collections_with_this_quote"], ["source"])
        self.assertEqual(ref["next_tool"]["arguments"]["collection"], "baseline")
        self.assertIn("does not support this declared run", ref["run_warning"])
        self.assertNotIn("format_hint", ref)

    def test_undisplayed_other_run_does_not_produce_cross_run_match_hint(self):
        source, baseline = "A source-only quote.", "Fresh baseline."
        views = {"source": self.view("source", source),
                 "baseline": self.view("baseline", baseline, presented=[(0, len(baseline))])}
        ref = self.repair(views, source)["references"][0]
        self.assertNotIn("other_displayed_collections_with_this_quote", ref)

    def test_coverage_error_recommends_next_unseen_page_without_reading_it(self):
        text = "a" * 20000 + "DO NOT EXPOSE THIS UNREAD PAGE"
        view = self.view("source", text, identifier="T000071", presented=[(0, 20000)])
        error = "source: read the cited behavior and subsequent trajectory before a supported judgment; undisplayed items include T000071"
        before = copy.deepcopy((view.presented, view.pending, view.context))
        output = build_evidence_repair_context(error, {"status": "candidate"}, {"source": view})
        ref = json.loads(output)["evidence_repair_context"]["references"][0]
        self.assertEqual(ref["next_tool"]["arguments"]["start_char"], 20000)
        self.assertNotIn("DO NOT EXPOSE", output)
        self.assertFalse(ref["fully_presented"])
        self.assertEqual(before, (view.presented, view.pending, view.context))

    def test_pending_tool_output_is_not_previously_displayed(self):
        view = self.view("baseline", "UNREAD TEXT")
        view.call("read_evidence", {"evidence_id": "T000021"})
        result = self.repair({"baseline": view}, "wrong")
        ref = result["references"][0]
        self.assertIsNone(ref["excerpt"])
        self.assertEqual(ref["next_tool"]["arguments"]["start_char"], 0)
        self.assertNotIn("UNREAD TEXT", json.dumps(result))
        self.assertEqual(view.presented, {})
        self.assertTrue(view.pending)

    def test_unseen_exact_quote_requires_normal_evidence_tool(self):
        text = "seen prefix\nunseen evidence"
        view = self.view("baseline", text, presented=[(0, 12)])
        ref = self.repair({"baseline": view}, "unseen evidence",
                          error="baseline: citation refers to undisplayed evidence T000021; read that passage before citing it")["references"][0]
        self.assertEqual(ref["excerpt"]["text"], "seen prefix\n")
        self.assertEqual(ref["next_tool"]["arguments"]["start_char"], 12)
        self.assertIn("has not been displayed", ref["issue"])

    def test_disjoint_spans_never_join_across_unseen_gap(self):
        text = "AAAAHIDDENBBBB"
        view = self.view("baseline", text, presented=[(0, 4), (10, 14)])
        ref = self.repair({"baseline": view}, "BBBB wrong")["references"][0]
        excerpt = ref["excerpt"]
        self.assertNotIn("HIDDEN", excerpt["text"])
        self.assertEqual(ref["next_tool"]["arguments"]["start_char"], 4)
        self.assertTrue(any(a <= excerpt["start_char"] <= excerpt["end_char"] <= b
                            for a, b in view.presented["T000021"]))

    def test_long_lines_and_large_quotes_remain_bounded_and_explicitly_partial(self):
        text = "p" * 60000 + "+1|import pandas as pd\n" + "z" * 60000
        view = self.view("baseline", text, presented=[(0, len(text))])
        quote = "import pandas as pd\n" + "not an exact quote" * 10000
        output = build_evidence_repair_context("baseline: evidence T000021 is not an exact quote from this run",
                                              self.payload(quote), {"baseline": view}, max_chars=2200)
        self.assertLessEqual(len(output), 2200)
        ref = json.loads(output)["evidence_repair_context"]["references"][0]
        self.assertTrue(ref["submitted_quote_truncated"])
        self.assertTrue(ref["excerpt"]["partial"])
        self.assertEqual(ref["excerpt"]["text"], text[ref["excerpt"]["start_char"]:ref["excerpt"]["end_char"]])

    def test_bounded_budget_accounts_for_json_escaping(self):
        text = "\t\n\"\\" * 5000
        view = self.view("baseline", text, presented=[(0, len(text))])
        output = build_evidence_repair_context("baseline: evidence T000021 is not an exact quote from this run",
                                              self.payload("\"wrong\"" * 5000), {"baseline": view}, max_chars=2200)
        self.assertTrue(output)
        self.assertLessEqual(len(output), 2200)
        json.loads(output)

    def test_terminal_normalized_view_is_used_without_further_reformatting(self):
        raw = "\x1b[31m+1|\timport pandas as pd\x1b[0m\r\n+2|print('ok')\r\n"
        view = self.view("baseline", raw)
        normalized = view.context.evidence[0].text
        view.presented["T000021"] = [(0, len(normalized))]
        ref = self.repair({"baseline": view}, "import pandas as pd\nprint('ok')")["references"][0]
        self.assertEqual(ref["excerpt"]["text"], normalized)
        self.assertIn("+1|\t", ref["excerpt"]["text"])
        self.assertIn("\r\n", ref["excerpt"]["text"])
        self.assertEqual(view.raw[0].text, raw)

    def test_discovery_report_path_selects_the_correct_reference(self):
        text = "Original Skill clause."
        view = self.view("source", text, identifier="S000001", kind="skill", presented=[(0, len(text))])
        payload = {"conflicts": [{"target": {"skill_ref": "S000001", "quote": "wrong Skill clause"},
                                  "behavior_evidence": [{"evidence_id": "T000001", "quote": "also wrong"}]}]}
        output = build_evidence_repair_context("report.conflicts[0].target.quote: not an exact substring of the referenced Skill",
                                              payload, {"source": view}, collection_argument=False)
        ref, = json.loads(output)["evidence_repair_context"]["references"]
        self.assertEqual(ref["evidence_id"], "S000001")
        self.assertEqual(ref["report_path"], "report.conflicts[0].target")
        self.assertNotIn("collection", ref["next_tool"]["arguments"])

    def test_discovery_nested_reference_index_is_respected(self):
        view = self.view("source", "True text", presented=[(0, 9)])
        payload = {"conflicts": [{"behavior_evidence": [
            {"evidence_id": "T000020", "quote": "unrelated"},
            {"evidence_id": "T000021", "quote": "wrong"}]}]}
        output = build_evidence_repair_context("report.conflicts[0].behavior_evidence[1].quote: not an exact substring of the referenced evidence",
                                              payload, {"source": view})
        refs = json.loads(output)["evidence_repair_context"]["references"]
        self.assertEqual([ref["evidence_id"] for ref in refs], ["T000021"])

    def test_reverse_conflict_evidence_belongs_to_candidate(self):
        view = self.view("candidate", "actual", presented=[(0, 6)])
        payload = {"reverse_conflicts": [{"evidence": [{"evidence_id": "T000021", "quote": "wrong"}]}]}
        output = build_evidence_repair_context("candidate.reverse_conflicts: evidence T000021 is not an exact quote from this run",
                                              payload, {"candidate": view})
        ref = json.loads(output)["evidence_repair_context"]["references"][0]
        self.assertEqual(ref["collection"], "candidate")

    def test_unknown_declared_evidence_id_is_not_substituted_from_other_run(self):
        baseline = self.view("baseline", "baseline", identifier="T000020", presented=[(0, 8)])
        source = self.view("source", "source evidence", presented=[(0, 15)])
        result = self.repair({"source": source, "baseline": baseline}, "source evidence")
        ref = result["references"][0]
        self.assertEqual(ref["collection"], "baseline")
        self.assertEqual(ref["next_tool"]["name"], "read_evidence_index")
        self.assertNotIn("excerpt", ref)

    def test_ambiguous_unqualified_same_id_does_not_guess_collection(self):
        views = {name: self.view(name, name, presented=[(0, len(name))]) for name in ("source", "baseline")}
        output = build_evidence_repair_context("Citation T000021 is not an exact quote", {"evidence_id": "T000021", "quote": "wrong"}, views)
        self.assertEqual(output, "")

    def test_generic_undisplayed_error_finds_stage1_reference(self):
        view = self.view("source", "unseen text")
        payload = {"conflicts": [{"behavior_evidence": [{"evidence_id": "T000021", "quote": "unseen text"}]}]}
        output = build_evidence_repair_context("Citation refers to undisplayed evidence text; read that passage before quoting it", payload, {"source": view})
        ref = json.loads(output)["evidence_repair_context"]["references"][0]
        self.assertEqual(ref["evidence_id"], "T000021")
        self.assertIsNone(ref["excerpt"])

    def test_generic_coverage_error_recommends_first_unread_trajectory(self):
        view = self.view("source", "read unread", presented=[(0, 5)])
        output = build_evidence_repair_context("Trajectory text coverage is incomplete; use insufficient_evidence or inspect remaining pages",
                                              {"status": "no_supported_conflict"}, {"source": view})
        ref = json.loads(output)["evidence_repair_context"]["references"][0]
        self.assertEqual(ref["next_tool"]["arguments"]["start_char"], 5)

    def test_namespaced_consolidation_coverage_ids_are_supported(self):
        view = self.view("source", "read unread", identifier="R0__T000021", presented=[(0, 5)])
        output = build_evidence_repair_context("source[0]: read cited behavior and subsequent trajectory; unread: R0__T000021",
                                              {"action": "merge"}, {"source": view})
        ref = json.loads(output)["evidence_repair_context"]["references"][0]
        self.assertEqual(ref["evidence_id"], "R0__T000021")
        self.assertEqual(ref["next_tool"]["arguments"]["start_char"], 5)

    def test_full_original_skill_coverage_error_selects_skill_items(self):
        view = self.view("source", "partial original Skill", kind="skill", identifier="ORIGINAL__S000001", presented=[(0, 7)])
        output = build_evidence_repair_context("source: read the complete canonical original Skill and target before consolidation",
                                              {"action": "merge"}, {"source": view})
        ref = json.loads(output)["evidence_repair_context"]["references"][0]
        self.assertEqual(ref["evidence_id"], "ORIGINAL__S000001")
        self.assertEqual(ref["next_tool"]["arguments"]["start_char"], 7)

    def test_unrelated_errors_and_tiny_context_do_not_add_guidance(self):
        view = self.view("baseline", "actual", presented=[(0, 6)])
        payload = self.payload("wrong")
        self.assertEqual(build_evidence_repair_context("Invalid revision action", payload, {"baseline": view}), "")
        self.assertEqual(build_evidence_repair_context("baseline: evidence T000021 is not an exact quote", payload,
                                                      {"baseline": view}, max_chars=300), "")

    def test_original_non_english_evidence_is_preserved_without_authored_non_english(self):
        text = "Original text: \u540d\u5b57"
        view = self.view("baseline", text, presented=[(0, len(text))])
        output = build_evidence_repair_context("baseline: evidence T000021 is not an exact quote from this run",
                                              self.payload("wrong"), {"baseline": view})
        self.assertTrue(output.isascii())
        self.assertEqual(json.loads(output)["evidence_repair_context"]["references"][0]["excerpt"]["text"], text)


if __name__ == "__main__":
    unittest.main()
