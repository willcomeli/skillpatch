"""Boundary-Aware evaluation across generation, consolidation and scoring."""

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import write_json
from excutor.excutor_runners.patch_sidecars import load_sidecars
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from excutor.runner_common.launcher import FileSkillBank
from harness.agent_client import AgentResult
from method_input_adapters.archive import read_json
from set_up.native_config import NativeRunConfig
from set_up.scienceworld_boundary_evaluation import ROOT, run_boundary_evaluation
from set_up.scienceworld_evolution import diagnose_training


class FakeAgent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.prompts = []

    def run(self, config, prompt, materials, output, environment, **kwargs):
        self.requests.append(read_json(materials[0]))
        self.prompts.append(prompt)
        report = output / "workspace/report.json"
        write_json(report, next(self.responses))
        return AgentResult("completed", output, report)


class BoundaryEvaluationTests(unittest.TestCase):
    def test_evaluates_frozen_replay_cohort_and_resumes_with_saved_patches(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        train = root / "train"
        skill = train / "input/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Observe after acting.\n", encoding="utf-8")
        evolution = root / "evolution.toml"
        prompts = ROOT / "prompts/skillpatch"
        evolution.write_text(
            '[agent]\nmodel = "fixture"\nbase_url = "http://fixture.invalid"\n'
            f'[stage1]\nprompt_path = "{prompts / "discovery.md"}"\n'
            f'[stage2]\nprompt_path = "{prompts / "patch_generation.md"}"\n'
            f'[stage3]\nprompt_path = "{prompts / "patch_consolidation.md"}"\n'
            f'[boundary_aware.stage2]\ninductor_prompt_path = "{root / "inductor.md"}"\n'
            f'proposer_prompt_path = "{root / "proposer.md"}"\n'
            f'[boundary_aware.stage3]\nconsolidation_prompt_path = "{root / "consolidate.md"}"\n',
            encoding="utf-8",
        )
        for name in ("inductor", "proposer", "consolidate"):
            (root / f"{name}.md").write_text(name, encoding="utf-8")
        refs = [
            {"task_id": task_id, "split": split, "task": "task-1-boil",
             "variation_idx": index, "simplification": "easy"}
            for index, (task_id, split) in enumerate((("one", "train"), ("two", "train"),
                                                     ("three", "train"), ("test", "test")))
        ]
        test_file = root / "test.json"
        write_json(test_file, refs[-1:])
        config = NativeRunConfig(
            benchmark="scienceworld", tasks_path=train / "input/tasks.json", skill_path=skill, model="fixture",
            api_key_env="FIXTURE_KEY", base_url_env="FIXTURE_URL", base_url=None,
            temperature=0.0, max_tokens=4096, timeout_sec=300, runner_options={}, output_root=root,
        )
        write_json(config.tasks_path, refs[:-1])
        write_json(train / "config.json", config)
        reports = []
        task_results = []
        for index, ref in enumerate(refs[:-1], 1):
            task = train / "tasks" / f"task_{index:06d}.json"
            write_json(task, {**ref, "dataset": "scienceworld", "instruction": "Observe the changed state.",
                              "success": False, "score": 0.0, "steps": 1,
                              "retrieved_memory_text": skill.read_text(),
                              "trajectory": [{"action": "wait", "next_observation": "The state changed."}]})
            task_results.append(str(task))
            report = {"summary": "Clarify when to observe.", "improvements": [{
                "target": {"skill_ref": "S0001", "quote": "Observe after acting."},
                "diagnosis": "An action changed the state.", "suggestion": "Inspect the changed state.",
                "evidence": [{"evidence_id": "T000001", "quote": "The state changed."}],
            }]}
            reports.append(report)
        write_json(train / "result.json", {"task_results": task_results})

        diagnosis_client = FakeAgent(reports)
        with patch("set_up.scienceworld_evolution.load_native_config", return_value=config):
            source_plan = diagnose_training(root / "diagnoses", [train], root / "execution.toml",
                                            evolution, test_file, {}, {}, workers=1, client=diagnosis_client)
            self.assertEqual(
                diagnose_training(root / "diagnoses", [train], root / "execution.toml",
                                  evolution, test_file, {}, {}, workers=1, client=diagnosis_client),
                source_plan,
            )
        self.assertEqual(len(diagnosis_client.requests), 3)
        issue_index = read_json(source_plan)
        self.assertEqual([row["task_id"] for row in issue_index["issues"]], ["one", "two", "three"])

        initial = {"status": "candidate", "reason": "Clarify the observable decision.",
                   "note": "Inspect after an action that changes the state.",
                   "example": "A change was reported. APPLY inspection."}
        initialized = {"decision": {"target_step": "Inspect the state.", "decision_ref": "T000001"}, "patch": initial}
        reference = {"pre_decision_state": "A change is reported.", "expected_use": "APPLY",
                     "expected_behavior": "Inspect the new state.", "evidence_refs": ["S0001"]}
        cases = {"reference": reference, "unresolved": [], "boundary_cases": [{
            **reference, "case_id": "INV-1", "type": "INV", "changed_factor": "changed object",
            "held_fixed": ["state changed"], "pre_decision_state": "Another object changed.",
        }]}
        no_cases = {"reference": None, "boundary_cases": [], "unresolved": ["No additional case is supported."]}
        no_patch = {"decision": None, "patch": {"status": "insufficient_evidence", "reason": "Ambiguous change.",
                                               "note": None, "example": None}}
        merged = {"action": "merge", "reason": "Both notes apply after a state change.",
                  "note": "Verify the resulting state after a state-changing action.",
                  "example": "A new object appeared. APPLY the inspection step."}
        client = FakeAgent([initialized, cases, initial, initialized, no_cases, no_patch, merged])

        args = argparse.Namespace(
            output=root / "run", source_plan=source_plan, config=root / "execution.toml",
            evolution_config=evolution, workers=1, test_manifest=test_file, prepare_only=False,
            server_port_start=9011, attempts=1, initial_eval=None,
        )
        executed = []

        def execute(native_config, output, environment, *, patch_skill_dir, skip_model_budget_errors):
            self.assertTrue(skip_model_budget_errors)
            manifest = load_sidecars(patch_skill_dir)
            self.assertEqual(manifest["schema_version"], 2)
            bank = PatchedSkillBank(FileSkillBank(native_config.skill_path), patch_skill_dir)
            delivered = bank.format_for_prompt({})
            self.assertIn(merged["note"], delivered)
            self.assertIn(merged["example"], delivered)
            tasks = read_json(native_config.tasks_path)
            executed.append([task["task_id"] for task in tasks])
            write_json(output / "input/tasks.json", tasks)
            for index, ref in enumerate(tasks, 1):
                write_json(output / "tasks" / f"task_{index:06d}.json", {
                    **ref, "dataset": "scienceworld", "instruction": "Observe the changed state.",
                    "success": True, "score": 1.0, "steps": 0, "trajectory": [],
                    "retrieved_memory_text": delivered, "patch_deployment": {"skill_digest": bank.digest},
                })
            return {"status": "completed"}

        with patch("set_up.scienceworld_evolution.load_native_config", return_value=config), \
             patch("set_up.scienceworld_evaluation.load_native_config", return_value=config), \
             patch("set_up.scienceworld_evaluation.run_native", side_effect=execute):
            self.assertEqual(run_boundary_evaluation(args, {}, {}, client=client), 0)
            saved_initial = root / "saved_initial"
            write_json(saved_initial / "test_plan.json", read_json(args.output / "evaluation/test_plan.json"))
            write_json(saved_initial / "initial/test/result.json", {
                "rows": [{"task_id": "test", "success": False, "score": 0.25}],
            })
            args.initial_eval = saved_initial
            self.assertEqual(run_boundary_evaluation(args, {}, {}, client=client), 0)

        self.assertEqual(executed, [["one", "two", "three"], ["test"]])
        self.assertEqual(len(client.requests), 7)
        consolidation = client.requests[-1]
        self.assertEqual(consolidation["existing"][0]["boundary_cases"], cases["boundary_cases"])
        candidates = read_json(args.output / "stage2/candidates.json")
        self.assertEqual([row["task_id"] for row in candidates], ["one", "two"])
        replay = read_json(args.output / "evaluation/patched/replay/result.json")
        self.assertEqual(replay["comparison"]["paired"], 3)
        test = read_json(args.output / "evaluation/patched/test/result.json")
        self.assertEqual(test["summary"]["mean_score"], 1.0)
        paired = read_json(args.output / "evaluation/test_comparison.json")
        self.assertEqual(paired["paired"], 1)
        self.assertEqual(paired["mean_score_delta"], 0.75)
        self.assertEqual(client.prompts, ["inductor", "proposer", "inductor", "inductor",
                                          "proposer", "inductor", "consolidate"])

        from set_up.cli import main
        standard_candidate = {"status": "candidate", "reason": "Act on the observed change.",
                              "when_to_use": "After a change.", "how_to_interpret": "Inspect the state."}
        no_standard_candidate = {"status": "insufficient_evidence", "reason": "No action warranted.",
                                 "when_to_use": None, "how_to_interpret": None}
        standard_group = {"action": "merge", "reason": "The changes are equivalent.",
                          "when_to_use": "After an action changes state.",
                          "how_to_interpret": "Inspect its result."}
        standard_client = FakeAgent([standard_candidate, standard_candidate, no_standard_candidate, standard_group])
        profile = root / "experiment.toml"
        profile.write_text(
            '[execution]\nconfig = "execution.toml"\nenv_file = "execution.env"\n'
            f'action_prompt = "{ROOT / "prompts/execution/scienceworld_action.md"}"\n'
            'train_manifest = "train/input/tasks.json"\ntest_manifest = "test.json"\n'
            '[evolution]\nconfig = "evolution.toml"\nenv_file = "evolution.env"\n',
            encoding="utf-8",
        )
        (root / "execution.env").touch()
        (root / "evolution.env").touch()

        standard = root / "standard"
        with patch("set_up.scienceworld_evolution.load_native_config", return_value=config), \
             patch("harness.agent_client.AgentClient.run", side_effect=standard_client.run):
            command = ["scienceworld", "evolve", "--experiment", str(profile), "--issues", str(source_plan),
                       "--workers", "1", "--output", str(standard)]
            self.assertEqual(main(command), 0)
            self.assertEqual(main(command), 0)

        self.assertEqual(len(standard_client.requests), 4)
        self.assertEqual(load_sidecars(standard / "stage3/candidate_skill")["schema_version"], 1)
        self.assertEqual(
            read_json(standard / "stage3/plan.json")["replay_sources"],
            read_json(args.output / "stage3/plan.json")["replay_sources"],
        )
        phases = []

        def execute_standard(native_config, output, environment, *, patch_skill_dir, skip_model_budget_errors):
            tasks = read_json(native_config.tasks_path)
            phases.append([task["task_id"] for task in tasks])
            bank = FileSkillBank(native_config.skill_path)
            if patch_skill_dir is not None:
                bank = PatchedSkillBank(bank, patch_skill_dir)
            write_json(output / "input/tasks.json", tasks)
            for index, ref in enumerate(tasks, 1):
                row = {**ref, "dataset": "scienceworld", "instruction": "Observe the state.",
                       "success": True, "score": 1.0, "steps": 0, "trajectory": [],
                       "retrieved_memory_text": bank.format_for_prompt({})}
                if patch_skill_dir is not None:
                    row["patch_deployment"] = {"skill_digest": bank.digest}
                write_json(output / "tasks" / f"task_{index:06d}.json", row)
            return {"status": "completed"}

        with patch("set_up.scienceworld_evaluation.load_native_config", return_value=config), \
             patch("set_up.scienceworld_evaluation.run_native", side_effect=execute_standard):
            for variant, stage3 in (("standard", standard / "stage3"), ("vanilla", None)):
                command = ["scienceworld", "evaluate", "--experiment", str(profile),
                           "--output", str(root / variant / "evaluation")]
                if stage3 is not None:
                    command.extend(["--stage3-run", str(stage3), "--phase", "replay"])
                    self.assertEqual(main(command), 0)
                    command[-1] = "test"
                self.assertEqual(main(command), 0)

        report = root / "comparison.json"
        self.assertEqual(main(["scienceworld", "compare", "--output", str(report),
                               "--vanilla", str(root / "vanilla/evaluation"),
                               "--standard", str(standard / "evaluation"),
                               "--boundary-aware", str(args.output / "evaluation")]), 0)
        self.assertEqual(read_json(report)["paired"], 1)
        self.assertEqual(phases, [["one", "two", "three"], ["test"], ["test"]])



if __name__ == "__main__":
    unittest.main()
