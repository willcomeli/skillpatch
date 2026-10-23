"""Command-line entry for shared baseline execution."""

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


def main(argv: list[str] | None = None) -> int:
    # 创建解析器，登记准备和执行两种命令。
    parser = argparse.ArgumentParser(prog="baseline-executor")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--output", type=Path, help="New run directory")
        command.add_argument("--env-file", type=Path)
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
    args = parser.parse_args(argv)
    environment = {}
    try:
        # 读取环境和运行配置，选择适配器与后端。
        environment = load_environment(args.env_file)
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
            result = run_consolidation(args.existing, args.incoming, args.skill_key,
                load_consolidation_config(args.config), args.output, environment,
                progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            return 130 if result["status"] == "cancelled" else (0 if result["status"] == "proposed" else 2)
        if args.command == "stage2":
            from method.skillpatch.patch_generation.config import load_stage2_config
            from method.skillpatch.patch_generation.pipeline import run_stage2
            result = run_stage2(args.diagnosis_dir, args.issue_id, load_stage2_config(args.config),
                                args.output, environment,
                                progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            return 130 if result["status"] == "cancelled" else (0 if result["status"] in {
                "completed", "generated", "unpatchable", "insufficient_evidence"} else 2)
        if args.command in {"diagnose", "stage1"}:
            from method.skillpatch.patch_diagnosis.config import load_discovery_config
            from method.skillpatch.patch_diagnosis.discovery import run_discovery
            config = load_discovery_config(args.config)
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
        if benchmark in NATIVE_BENCHMARKS:
            from excutor.eviskill_runners.launcher import run_native
            if args.patch_skills_dir is not None:
                raise ValueError("Native datasets use --patch-skill-dir (one Skill), not --patch-skills-dir")
            config = load_native_config(args.config)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            output = args.output or config.output_root / f"{stamp}-{benchmark}-{uuid4().hex[:8]}"
            print(f"Run directory: {output.resolve()}", flush=True)
            result = run_native(config, output, environment,
                                prepare_only=args.command == "prepare", patch_skill_dir=args.patch_skill_dir)
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            if result["status"] == "cancelled":
                return 130
            return 0 if result["status"] in {"completed", "prepared"} else 2
        if args.patch_skill_dir is not None:
            raise ValueError("SkillsBench uses --patch-skills-dir (full Skill pack)")
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
    except (OSError, ValueError) as exc:
        print(redact(f"error: {exc}", secret_values(environment)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
