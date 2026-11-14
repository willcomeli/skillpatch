"""Public command line for execution, SkillPatch and benchmark comparisons."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from excutor.executor_adapter.registry import get_execution_adapter
from excutor.excutor_runners.benchflow import BenchFlowBackend
from excutor.excutors import Executor
from set_up.config import load_config
from set_up.native_config import NATIVE_BENCHMARKS, read_benchmark, load_native_config
from set_up.environment import load_environment, redact, secret_values
from method.skillpatch.patch_generation.models import INDUCTIONS


def main(argv: list[str] | None = None) -> int:
    # 创建解析器，登记准备和执行两种命令。
    parser = argparse.ArgumentParser(prog="skillpatch")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--output", type=Path, help="New run directory")
        command.add_argument("--env-file", type=Path)
        command.add_argument("--task-manifest", type=Path, help="Override the task JSON manifest for native datasets")
        command.add_argument("--skip-model-budget-errors", action="store_true",
                             help="ScienceWorld: record tasks that exhaust the model output budget and continue")
        patches = command.add_mutually_exclusive_group()
        patches.add_argument("--patch-skills-dir", type=Path, help="SkillsBench: full patched Skill pack")
        patches.add_argument("--patch-skill-dir", type=Path, help="Native datasets: one patched Skill directory")
    diagnosis = subcommands.add_parser("diagnose", aliases=["stage1"], help="Offline SkillPatch diagnosis using a local agent")
    diagnosis.add_argument("--run-dir", required=True, type=Path, help="SkillsBench run directory or native task result JSON")
    diagnosis.add_argument("--config", required=True, type=Path)
    diagnosis.add_argument("--output", type=Path)
    diagnosis.add_argument("--env-file", type=Path)
    diagnosis.add_argument("--prepare-only", action="store_true")
    stage2 = subcommands.add_parser("stage2", help="Generate a candidate patch from unified Stage1 outputs")
    stage2.add_argument("--diagnosis-dir", required=True, type=Path)
    stage2.add_argument("--issue-id", "--conflict-id", required=True)
    stage2.add_argument("--config", required=True, type=Path)
    stage2.add_argument("--output", required=True, type=Path)
    stage2.add_argument("--env-file", type=Path)
    stage2.add_argument("--induction", choices=INDUCTIONS, default="standard",
                        help="Stage2 patch induction method")
    stage3 = subcommands.add_parser("stage3", help="Propose consolidation of candidate patches")
    stage3.add_argument("--existing", required=True, action="append", type=Path,
                        help="Existing generation/patch.json; repeat for multiple patches")
    stage3.add_argument("--incoming", required=True, type=Path)
    stage3.add_argument("--skill-key", required=True)
    stage3.add_argument("--config", required=True, type=Path)
    stage3.add_argument("--output", required=True, type=Path)
    stage3.add_argument("--env-file", type=Path)
    validation = subcommands.add_parser("validate-patch", help="Validate an existing patch with SkillsBench")
    validation.add_argument("--patch", required=True, type=Path)
    validation.add_argument("--config", required=True, type=Path)
    validation.add_argument("--output", required=True, type=Path)
    validation.add_argument("--env-file", type=Path)
    validation.add_argument("--prepare-only", action="store_true")
    appworld_validation = subcommands.add_parser("validate-appworld-patch", help="Replay a patch's source AppWorld task; no dev split")
    appworld_validation.add_argument("--patch", required=True, type=Path)
    appworld_validation.add_argument("--output", required=True, type=Path)
    appworld_validation.add_argument("--env-file", type=Path)
    appworld_validation.add_argument("--prepare-only", action="store_true")
    add_experiment_commands(subcommands)
    args = parser.parse_args(argv)
    environment = {}
    try:
        if handler := getattr(args, "handler", None):
            return handler(args)
        # 读取环境和运行配置，选择适配器与后端。
        environment = load_environment(args.env_file)
        if args.command == "validate-appworld-patch":
            from method.skillpatch.patch_validation.appworld_patch_validation.pipeline import run_validation
            result = run_validation(args.patch, args.output, environment, prepare_only=args.prepare_only)
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            return 130 if result["status"] == "cancelled" else (0 if result["status"] in {"prepared", "completed"} else 2)
        if args.command == "validate-patch":
            from method.skillpatch.patch_validation.skillsbench_patch_validation.config import load_validation_config
            from method.skillpatch.patch_validation.skillsbench_patch_validation.pipeline import run_validation
            result = run_validation(args.patch, load_validation_config(args.config), args.output,
                environment, prepare_only=args.prepare_only,
                progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            return 130 if result["status"] == "cancelled" else (0 if result["status"] in {"prepared", "completed"} else 2)
        if args.command == "stage3":
            from method.skillpatch.patch_consolidation.config import load_consolidation_config
            from method.skillpatch.patch_consolidation.pipeline import run_consolidation
            config = load_consolidation_config(args.config, environment=environment)
            result = run_consolidation(args.existing, args.incoming, args.skill_key,
                config, args.output, environment,
                progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            return 130 if result["status"] == "cancelled" else (0 if result["status"] == "proposed" else 2)
        if args.command == "stage2":
            from method.skillpatch.patch_generation.config import load_stage2_config
            from method.skillpatch.patch_generation.pipeline import run_stage2
            config = load_stage2_config(args.config, environment=environment)
            result = run_stage2(args.diagnosis_dir, args.issue_id, config, args.output, environment,
                                induction=args.induction, progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            return 130 if result["status"] == "cancelled" else (0 if result["status"] in {
                "completed", "generated", "unpatchable", "insufficient_evidence"} else 2)
        if args.command in {"diagnose", "stage1"}:
            from method.skillpatch.patch_diagnosis.config import load_discovery_config
            from method.skillpatch.patch_diagnosis.discovery import run_discovery
            config = load_discovery_config(args.config, environment=environment)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            source = args.run_dir.resolve()
            parent = source if source.is_dir() else source.parent
            output = args.output or parent / "discovery" / f"{stamp}-{uuid4().hex[:8]}"
            result = run_discovery(source, config, output, environment,
                                   prepare_only=args.prepare_only,
                                   progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            return 130 if result["status"] == "cancelled" else (0 if result["status"] in {"completed", "prepared", "skipped"} else 2)
        benchmark = read_benchmark(args.config)
        if args.skip_model_budget_errors and benchmark != "scienceworld":
            raise ValueError("--skip-model-budget-errors is available only for ScienceWorld")
        if benchmark in NATIVE_BENCHMARKS:
            from excutor.runner_common.launcher import run_native
            if args.patch_skills_dir is not None:
                raise ValueError("Native datasets use --patch-skill-dir (one Skill), not --patch-skills-dir")
            config = load_native_config(args.config, environment)
            if args.task_manifest is not None:
                config.tasks_path = args.task_manifest.expanduser().resolve()
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            output = args.output or config.output_root / f"{stamp}-{benchmark}-{uuid4().hex[:8]}"
            print(f"Run directory: {output.resolve()}", flush=True)
            result = run_native(config, output, environment,
                                prepare_only=args.command == "prepare", patch_skill_dir=args.patch_skill_dir,
                                skip_model_budget_errors=args.skip_model_budget_errors)
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            if result["status"] == "cancelled":
                return 130
            return 0 if result["status"] in {"completed", "completed_with_skips", "prepared"} else 2
        if args.patch_skill_dir is not None:
            raise ValueError("SkillsBench uses --patch-skills-dir (full Skill pack)")
        if args.task_manifest is not None:
            raise ValueError("--task-manifest is available only for native datasets")
        request, output_root = load_config(args.config)
        adapter = get_execution_adapter(request.benchmark)
        if request.backend != "benchflow":
            raise ValueError(f"Unsupported backend: {request.backend}")
        backend = BenchFlowBackend()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = args.output or output_root / f"{stamp}-{request.task_dir.name}-{uuid4().hex[:8]}"
        print(f"Run directory: {output.resolve()}", flush=True)
        # 调用通用执行器；prepare 命令只准备，不执行任务。
        result = Executor(adapter, backend).run(
            request, output, environment, prepare_only=args.command == "prepare",
            progress=lambda message: print(message, flush=True),
            patch_skills_dir=args.patch_skills_dir,
        )
        summary = {
            "run_id": result.run_id, "status": result.status,
            "evaluation": result.feedback.status if result.feedback else None,
            "passed": result.feedback.passed if result.feedback else None,
            "metrics": result.feedback.metrics if result.feedback else {},
            "evaluation_issues": result.feedback.issues if result.feedback else [],
            "errors": result.errors,
            "result_path": str(output.resolve() / "result.json"),
        }
        print(json.dumps(redact(summary, secret_values(environment)), ensure_ascii=False, indent=2))
        if result.status == "cancelled":
            return 130
        return 0 if result.status in {"completed", "prepared"} else 2
    except KeyboardInterrupt:
        return 130
    except ModuleNotFoundError as exc:
        if exc.name not in {"skillopt", "yaml", "openai"}:
            raise
        print(f"error: missing optional dependency {exc.name}. See the comparison-method setup in README.md.",
              file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(redact(f"error: {exc}", secret_values(environment)), file=sys.stderr)
        return 2


def add_experiment_commands(subcommands):
    from method_input_adapters.__main__ import add_commands as add_baselines
    from scripts.skillsbench import experiment as skillsbench
    from set_up import (appworld_experiments, appworld_full, appworld_parallel_test, appworld_patch_test,
                        appworld_recover, appworld_resume, appworld_smoke, scienceworld_cli,
                        scienceworld_eval_supervisor)

    scienceworld = subcommands.add_parser("scienceworld", help="ScienceWorld collection, evolution and evaluation")
    scienceworld_cli.add_commands(scienceworld, supervisor=scienceworld_eval_supervisor.configure_parser)

    appworld = subcommands.add_parser("appworld", help="AppWorld training and test workflows")
    commands = appworld.add_subparsers(dest="appworld_command", required=True)
    appworld_experiments.add_commands(commands)
    for name, help_text, module in (
        ("experiment", "Run the full AppWorld comparison", appworld_full),
        ("smoke", "Run the small AppWorld comparison", appworld_smoke),
        ("evaluate", "Test trained SkillPatch on test_normal", appworld_patch_test),
        ("evaluate-baselines", "Test saved original and Trace2Skill Skills", appworld_parallel_test),
        ("resume-collection", "Resume an interrupted training collection", appworld_resume),
        ("recover", "Recover a saved comparison and evaluate held-out tasks", appworld_recover),
    ):
        module.configure_parser(commands.add_parser(name, help=help_text))

    skillsbench_group = subcommands.add_parser("skillsbench", help="SkillsBench comparison workflow")
    commands = skillsbench_group.add_subparsers(dest="skillsbench_command", required=True)
    skillsbench.configure_parser(commands.add_parser("experiment", help="Run shared baselines and selected methods"))

    baselines = subcommands.add_parser("baselines", help="Export, train or evolve optional comparison methods")
    add_baselines(baselines.add_subparsers(dest="baseline_command", required=True))


if __name__ == "__main__":
    raise SystemExit(main())
