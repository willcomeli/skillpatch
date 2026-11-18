"""ScienceWorld evaluation must resume without replacing previously scored tasks."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from excutor.executor_adapter.storage import sha256_file, write_json
from excutor.excutor_runners.patch_sidecars import write_sidecars
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from excutor.runner_common.launcher import FileSkillBank
from method_input_adapters.archive import read_json
from set_up.native_config import NativeRunConfig
from set_up.scienceworld_evaluation import compare, index_tasks, run_phase


class ScienceWorldEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("Observe after acting.\n", encoding="utf-8")
        self.refs = [
            {"task_id": task_id, "split": "test", "task": "task-1-boil",
             "variation_idx": index, "simplification": "easy"}
            for index, task_id in enumerate(("one", "two"))
        ]
        self.task_file = self.root / "test_tasks.json"
        write_json(self.task_file, self.refs)
        self.tasks = index_tasks(self.refs, "test")
        self.config = NativeRunConfig(
            benchmark="scienceworld", tasks_path=self.task_file, skill_path=self.skill, model="fixture",
            api_key_env="FIXTURE_KEY", base_url_env="FIXTURE_URL", base_url=None,
            temperature=0.0, max_tokens=4096, timeout_sec=300, runner_options={}, output_root=self.root,
        )
        self.phase = self.root / "patched/test"
        self.bundle = self.root / "candidate_skill"
        self.bundle.mkdir()
        original = self.bundle / "SKILL.md"
        original.write_bytes(self.skill.read_bytes())
        write_sidecars(self.bundle, [{
            "entry_id": "P1", "target": {"relative_path": "SKILL.md", "scope": "substep",
                                         "quote": "Observe after acting.", "version": sha256_file(original)},
            "when_to_use": "When an action may change the state.",
            "how_to_interpret": "Inspect the resulting state.",
        }])

    def saved_run(self, config, output, *, accepted, status, bundle=None):
        refs = read_json(config.tasks_path)
        write_json(output / "input/tasks.json", refs)
        bank = FileSkillBank(config.skill_path)
        if bundle is not None:
            bank = PatchedSkillBank(bank, bundle)
        paths = []
        for index, ref in enumerate(refs, 1):
            if ref["task_id"] not in accepted:
                continue
            row = {**ref, "dataset": "scienceworld", "instruction": "Boil the substance.",
                   "success": False, "score": 0.0, "steps": 0, "trajectory": [],
                   "retrieved_memory_text": bank.format_for_prompt({})}
            if bundle is not None:
                row["patch_deployment"] = {"skill_digest": bank.digest}
            path = output / "tasks" / f"task_{index:06d}.json"
            write_json(path, row)
            paths.append(str(path))
        state = {"status": status, "task_results": paths, "model_requests": {"attempts": 1}}
        write_json(output / "result.json", state)
        return state

    def test_resume_preserves_scored_tasks_and_runs_only_missing_tasks(self):
        calls = []

        def execute(config, output, environment, *, patch_skill_dir, skip_model_budget_errors):
            self.assertTrue(skip_model_budget_errors)
            self.assertEqual(patch_skill_dir, self.bundle)
            calls.append([ref["task_id"] for ref in read_json(config.tasks_path)])
            if len(calls) == 1:
                return self.saved_run(config, output, accepted={"one"}, status="completed_with_skips",
                                      bundle=patch_skill_dir)
            return self.saved_run(config, output, accepted={"two"}, status="completed",
                                  bundle=patch_skill_dir)

        with patch("set_up.scienceworld_evaluation.run_native", side_effect=execute):
            def run():
                return run_phase(self.phase, self.task_file, self.tasks, self.config, {},
                                 attempts=1, prepare_only=False, bundle=self.bundle)

            self.assertEqual(run(), "incomplete")
            first = read_json(self.phase / "result.json")
            self.assertEqual(first["missing"], ["two"])
            self.assertEqual(run(), "completed")
            self.assertEqual(run(), "completed")

        self.assertEqual(calls, [["one", "two"], ["two"]])
        final = read_json(self.phase / "result.json")
        self.assertEqual(final["rows"][0], first["rows"][0])
        self.assertEqual(final["summary"]["count"], 2)
        self.assertEqual(final["summary"]["mean_score"], 0.0)

    def test_interrupted_task_is_deferred_behind_unattempted_tasks(self):
        calls = []

        def execute(config, output, environment, **kwargs):
            calls.append([ref["task_id"] for ref in read_json(config.tasks_path)])
            if len(calls) == 1:
                return self.saved_run(config, output, accepted=set(), status="backend_error")
            return self.saved_run(config, output, accepted={"one", "two"}, status="completed")

        with patch("set_up.scienceworld_evaluation.run_native", side_effect=execute):
            status = run_phase(self.phase, self.task_file, self.tasks, self.config, {},
                               attempts=2, prepare_only=False)
        self.assertEqual(status, "completed")
        self.assertEqual(calls, [["one", "two"], ["two", "one"]])

    def test_comparison_excludes_missing_results_from_both_denominators(self):
        initial = {"one": {"success": False, "score": 0.25}, "two": {"success": True, "score": 1.0}}
        patched = {"one": {"success": True, "score": 1.0}}
        result = compare(self.tasks, initial, patched)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["paired"], 1)
        self.assertEqual(result["patched_missing"], ["two"])
        self.assertEqual(result["initial"]["success_rate"], 0)
        self.assertEqual(result["mean_score_delta"], 0.75)


if __name__ == "__main__":
    unittest.main()
