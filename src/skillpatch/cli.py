"""CLI for fresh task execution and Stage 1 diagnosis of an Executor record."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from skillpatch.config import load_config
from skillpatch.environment import load_environment, redact, secret_values
from skillpatch.executor import Executor, is_valid_diagnosis_skip
from skillpatch.adapters.registry import get_adapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skillpatch")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        command = subcommands.add_parser(name, help="Snapshot inputs only" if name == "prepare" else "Execute a fresh task")
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--output", type=Path, help="New run directory; existing paths are rejected")
        command.add_argument("--env-file", type=Path, help="Literal KEY=VALUE file; shell variables take precedence")
        command.add_argument("--patch-registry", type=Path, help="Explicitly deploy one stored patch set")
        command.add_argument("--patch-skill-key")
        command.add_argument("--patch-version", help="Recorded version ID, 'original', or current when omitted")
        command.add_argument("--patch-skill-path", help="Optional task-pack relative Skill target, e.g. xlsx/SKILL.md")
    diagnosis = subcommands.add_parser("diagnose", help="Diagnose one Executor run using its actual evidence")
    diagnosis.add_argument("--run-dir", required=True, type=Path)
    diagnosis.add_argument("--config", required=True, type=Path)
    diagnosis.add_argument("--output", type=Path, help="New diagnosis directory; existing paths are rejected")
    diagnosis.add_argument("--env-file", type=Path)
    diagnosis.add_argument("--prepare-only", action="store_true", help="Save and check input without a model call")
    stage2 = subcommands.add_parser("stage2", help="Generate and validate one Stage 1 conflict patch")
    stage2.add_argument("--diagnosis-dir", required=True, type=Path)
    stage2.add_argument("--conflict-id", required=True)
    stage2.add_argument("--config", required=True, type=Path)
    stage2.add_argument("--output", type=Path)
    stage2.add_argument("--env-file", type=Path)
    stage2.add_argument("--patch-file", type=Path,
                        help="Revalidate an exact existing patch without generating or revising it")
    mode = stage2.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="Validate source and save budget plan without model/task calls")
    mode.add_argument("--generate-only", action="store_true", help="Generate and render an unvalidated candidate without task runs")
    stage3 = subcommands.add_parser("stage3", help="Consolidate accepted patches without rewriting the original Skill")
    stage3.add_argument("--queue", required=True, type=Path)
    stage3.add_argument("--skill-key", required=True, help="Explicit identity of this original Skill")
    stage3.add_argument("--registry", required=True, type=Path)
    stage3.add_argument("--config", required=True, type=Path)
    stage3.add_argument("--original-skill", type=Path, help="Optional explicit original single-Skill resource directory")
    stage3.add_argument("--output", type=Path)
    stage3.add_argument("--env-file", type=Path)
    stage3.add_argument("--prepare-only", action="store_true", help="Check evidence and plan without activation, tasks or models")
    sets = subcommands.add_parser("patch-set", help="Inspect, export or roll back versioned supplements")
    sets.add_argument("--registry", required=True, type=Path)
    sets.add_argument("--skill-key", required=True)
    sets.set_defaults(env_file=None)
    set_commands = sets.add_subparsers(dest="set_command", required=True)
    set_commands.add_parser("show")
    set_commands.add_parser("history")
    export = set_commands.add_parser("export")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--version")
    rollback = set_commands.add_parser("rollback")
    rollback.add_argument("--version", required=True, help="Recorded version ID or 'original'")
    rollback.add_argument("--expected-parent", required=True, help="Current version ID; refuses concurrent state changes")
    args = parser.parse_args(argv)
    environment = {}
    try:
        environment = load_environment(args.env_file)
        if args.command == "patch-set":
            from skillpatch.patch_sets import PatchSetStore
            store = PatchSetStore(args.registry, args.skill_key)
            if args.set_command == "show":
                result = store.current()
            elif args.set_command == "history":
                result = store.history()
            elif args.set_command == "export":
                result = store.export(args.output, args.version)
            elif args.version == "original":
                result = store.commit([], store.original_dir, {"mode": "explicit_rollback_to_original",
                    "previous_version": args.expected_parent}, expected_parent=args.expected_parent)
            else:
                result = store.rollback(args.version, expected_parent=args.expected_parent)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "stage3":
            from skillpatch.stage3_config import load_stage3_config
            from skillpatch.stages.consolidation import run_stage3
            config = load_stage3_config(args.config)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            output = args.output or args.queue.resolve().parent.parent.parent / "stage3" / f"{stamp}-{uuid4().hex[:8]}"
            result = run_stage3(args.queue, args.skill_key, args.registry, config, output, environment,
                original_skill=args.original_skill, prepare_only=args.prepare_only,
                progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            if result["status"] == "cancelled":
                return 130
            return 0 if result["status"] in {"completed", "prepared"} else 2
        if args.command == "stage2":
            from skillpatch.stage2_config import load_stage2_config
            from skillpatch.stages.patch_validation import run_stage2
            config = load_stage2_config(args.config)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            output = args.output or args.diagnosis_dir.parent.parent / "stage2" / f"{stamp}-{uuid4().hex[:8]}"
            result = run_stage2(args.diagnosis_dir, args.conflict_id, config, output, environment,
                                prepare_only=args.prepare_only, generate_only=args.generate_only,
                                patch_file=args.patch_file,
                                progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            if result["status"] == "cancelled":
                return 130
            return 0 if result["status"] in {"completed", "prepared"} else 2
        if args.command == "diagnose":
            from skillpatch.model_client import load_diagnosis_config
            from skillpatch.stages.discovery import run_discovery

            config = load_diagnosis_config(args.config)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            output = args.output or args.run_dir / "discovery" / f"{stamp}-{uuid4().hex[:8]}"
            result = run_discovery(args.run_dir, config, output, environment,
                                   prepare_only=args.prepare_only,
                                   progress=lambda message: print(message, flush=True))
            print(json.dumps(redact(result, secret_values(environment)), ensure_ascii=False, indent=2))
            if result["status"] == "cancelled":
                return 130
            return 0 if result["status"] in {"completed", "prepared"} else 2
        request, output_root = load_config(args.config)
        adapter = get_adapter(request.benchmark)
        if request.backend != "benchflow":
            raise ValueError(f"Unsupported backend: {request.backend}")
        from skillpatch.runners.benchflow import BenchFlowBackend

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = args.output or output_root / f"{stamp}-{request.task_dir.name}-{uuid4().hex[:8]}"
        patch_manifest = None
        if any((args.patch_registry, args.patch_skill_key, args.patch_version, args.patch_skill_path)):
            if args.patch_registry is None or not args.patch_skill_key:
                raise ValueError("Patch deployment requires both --patch-registry and --patch-skill-key")
            if output.exists() or output.is_symlink():
                raise ValueError("Patch deployment requires a new run output directory")
            from skillpatch.patch_sets import PatchSetStore
            from skillpatch.patch_deployment import prepare_patch_set_request
            request, patch_manifest = prepare_patch_set_request(request, adapter,
                PatchSetStore(args.patch_registry, args.patch_skill_key),
                output.with_name(output.name + "-patch-input"), version_id=args.patch_version,
                skill_path=args.patch_skill_path)
        print(f"Run directory: {output.resolve()}", flush=True)
        result = Executor(adapter, BenchFlowBackend()).run(
            request, output, environment, prepare_only=args.command == "prepare",
            progress=lambda message: print(message, flush=True),
            expected_skill_digest=patch_manifest["deployed_pack_digest"] if patch_manifest is not None else None,
        )
        if patch_manifest is not None:
            from skillpatch.storage import write_json
            verification_path = output / "skill_deployment_verification.json"
            patch_manifest["execution_deployment_verification"] = (
                json.loads(verification_path.read_text(encoding="utf-8")) if verification_path.is_file()
                else {"status": "unavailable", "reason": "Preparation did not reach deployment verification"})
            write_json(output / "patch_set.json", patch_manifest)
        summary = {"run_id": result.run_id, "status": result.status,
                   "evaluation": result.feedback.status if result.feedback else None,
                   "passed": result.feedback.passed if result.feedback else None,
                   "metrics": result.feedback.metrics if result.feedback else {},
                   "evaluation_issues": result.feedback.issues if result.feedback else [],
                   "errors": result.errors, "diagnosis": result.diagnosis,
                   "result_path": str(output.resolve() / "result.json")}
        print(json.dumps(redact(summary, secret_values(environment)), ensure_ascii=False, indent=2))
        if result.status == "cancelled":
            return 130
        if args.command == "run" and request.diagnosis_config is not None:
            if ((result.diagnosis or {}).get("status") != "completed"
                    and not is_valid_diagnosis_skip(result.diagnosis, result.feedback)):
                return 2
        return 0 if result.status in {"completed", "prepared"} else 2
    except (OSError, ValueError) as exc:
        print(redact(f"error: {exc}", secret_values(environment)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
