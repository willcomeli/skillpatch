"""BenchFlow 0.6.3 bridge from a retained sandbox to the portable verified hook.

The optional SDK is imported only by run_worker. Stage 1 receives a common
VerifiedSession and never sees Rollout or another BenchFlow object.
"""

import argparse
import asyncio
import copy
import importlib.metadata
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from skillpatch.environment import redact, secret_values
from skillpatch.lifecycle import VerifiedCallback, VerifiedSession, invoke_verified_callback
from skillpatch.model_client import diagnosis_secrets, load_diagnosis_config
from skillpatch.models import BackendRun, PreparedTask, RunRequest
from skillpatch.runners.benchflow import BenchFlowBackend, _read_object, build_config
from skillpatch.skill_invocation import audit_skill_reads
from skillpatch.storage import now, sha256_file, write_json


def _diagnosis_secret_values(request: RunRequest, environment: Mapping[str, str]) -> tuple[str, ...]:
    """Include explicitly configured credentials whose variable names are arbitrary."""
    if request.diagnosis_config is None:
        return secret_values(environment)
    return diagnosis_secrets(load_diagnosis_config(request.diagnosis_config), environment)


def load_worker_inputs(request_path: Path, run_dir: Path) -> tuple[RunRequest, PreparedTask]:
    root = run_dir.resolve()
    if request_path.resolve() != root / "request.json":
        raise ValueError("Worker request must belong to this run")
    record = _read_object(root / "result.json")
    if record.get("run_id") != root.name or record.get("status") != "running":
        raise ValueError("Worker requires this run's active executor record")
    values = _read_object(request_path)
    for field in ("task_dir", "skills_dir", "bench_python", "diagnosis_config"):
        if values.get(field) is not None:
            values[field] = Path(values[field])
    values["bench_command"] = tuple(values["bench_command"])
    request = RunRequest(**values)
    prepared = dict(record.get("prepared") or {})
    for field in ("task_dir", "task_document", "skills_dir", "manifest_path"):
        if prepared.get(field) is not None:
            path = Path(prepared[field])
            if not path.resolve().is_relative_to(root / "input"):
                raise ValueError(f"Prepared {field} is outside this run's input")
            prepared[field] = path
    task = PreparedTask(**prepared)
    if task.task_id != record.get("task_id") or task.benchmark != request.benchmark:
        raise ValueError("Prepared task identity differs from the executor record")
    for relative, expected in task.file_hashes.items():
        path = task.task_dir / relative
        if path.is_symlink() or not path.resolve().is_relative_to(task.task_dir.resolve()):
            raise ValueError("Prepared file is outside the task snapshot")
        if sha256_file(path) != expected:
            raise ValueError(f"Prepared snapshot changed: {relative}")
    return request, task


def rollout_config_values(request: RunRequest, task: PreparedTask, run_dir: Path) -> dict[str, Any]:
    config = build_config(request, task, run_dir)
    return {
        "task_path": task.task_dir, "agent": request.agent, "model": request.model,
        "reasoning_effort": request.reasoning_effort, "prompts": config.get("prompts"),
        "environment": request.sandbox, "sandbox_user": request.sandbox_user,
        "sandbox_setup_timeout": request.sandbox_setup_timeout,
        "agent_idle_timeout": request.agent_idle_timeout, "usage_tracking": request.usage_tracking,
        "skills_dir": None, "skill_mode": request.skill_mode,
        "jobs_dir": str(run_dir / "jobs"), "loop_strategy": "single-shot",
    }


def _locations(rollout) -> tuple[str, str]:
    # These two deployment getters are absent from the 0.6.3 public API.
    # Keep this version-specific, read-only bridge entirely inside the backend.
    workspace = getattr(rollout, "_agent_cwd", None)
    skills = getattr(rollout, "_effective_skills_sandbox_dir", None)
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        raise ValueError("BenchFlow did not expose a resolved sandbox workspace")
    return workspace, skills or ""


async def _snapshot_files(rollout, request) -> dict[str, Any]:
    from skillpatch.inspection import snapshot_file_state
    workspace, skills = _locations(rollout)
    return await snapshot_file_state(rollout.env, workspace, skills, user=request.sandbox_user)


def freeze_verified_execution(run_dir: Path, task: PreparedTask, version: str) -> BackendRun:
    """Copy score, verifier feedback and ACP before any diagnosis can run."""
    backend = run_dir / "backend"
    execution = BackendRun(status="running", backend_version=version)
    BenchFlowBackend()._collect(execution, task, run_dir / "jobs", backend)
    if execution.status == "running":
        execution.status = "completed" if not execution.errors else "artifact_error"
    write_json(backend / "verified_execution.json", execution)
    frozen = backend / "frozen_verification"
    frozen.mkdir(exist_ok=False)
    candidates: list[tuple[str, Path]] = []
    for name, path in (("result.json", execution.result_path),
                       ("acp_trajectory.jsonl", execution.trajectory_path),
                       ("events.jsonl", execution.normalized_trajectory_path)):
        if path is not None:
            candidates.append((name, path))
    if execution.verifier_dir is not None:
        for name in ("test-stdout.txt", "test-stderr.txt", "reward.txt", "reward.json", "ctrf.json"):
            path = execution.verifier_dir / name
            if path.is_file():
                candidates.append(("verifier/" + name, path))
    files = []
    for name, source in candidates:
        if source.is_symlink() or not source.resolve().is_relative_to(run_dir):
            raise ValueError("Verified evidence resolves outside this run")
        digest = sha256_file(source)
        destination = frozen / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if sha256_file(source) != digest or sha256_file(destination) != digest:
            raise ValueError("Verified evidence changed while being frozen")
        files.append({"source_path": str(source), "frozen_path": str(destination), "sha256": digest})
    write_json(backend / "evidence_freeze.json", {
        "schema_version": 1, "run_id": run_dir.name, "phase": "post_verification_pre_diagnosis",
        "created_at": now(), "files": files,
        "limitations": ["Pre-cleanup usage telemetry can be incomplete; final execution telemetry "
                        "is saved by BenchFlow after cleanup."]})
    write_json(backend / "skill_reads.json", audit_skill_reads(task, execution.normalized_trajectory_path))
    return execution


def _create_session(rollout, execution: BackendRun, verified_at: str) -> VerifiedSession:
    from skillpatch.inspection import LiveInspector
    root = rollout.skillpatch_run_dir
    initial = _read_object(root / "backend" / "initial_files.json")
    workspace, skills = _locations(rollout)
    inspector = LiveInspector(
        rollout.env, workspace, skills, root / "discovery" / "live",
        # Verification hardening changes workspace ownership to root; fixed
        # inspection operations must still read the preserved execution files.
        # Keep original permissions and execution-user provenance unchanged.
        initial_hashes=initial.get("hashes", {}), user="root",
        secrets=rollout.skillpatch_secrets,
    )
    return VerifiedSession(execution=execution, inspector=inspector, metadata={
        "verified_at": verified_at, "phase": "post_verification_pre_cleanup",
        "workspace": workspace, "skills_dir": skills,
        "environment_kind": rollout.skillpatch_request.sandbox,
        "inspection_user": "root",
        "initial_inspection_user": rollout.skillpatch_request.sandbox_user,
        "inspection_reason": "BenchFlow verification hardening transfers workspace ownership to root; "
                             "only the fixed read-only inspection transport uses root after verification.",
        "limitations": [*initial.get("limitations", []),
            "Post-verification inspection uses root for fixed read-only operations because verifier hardening "
            "changes workspace ownership. Newly readable files do not establish prior execution-Agent access."],
    })


def live_rollout_class(base):
    """Subclass factory also permits lifecycle tests without installing the SDK."""
    class LiveDiagnosticRollout(base):
        def configure_diagnosis(self, run_dir, request, prepared, environment, version,
                                *, on_verified: VerifiedCallback | None = None):
            self.skillpatch_run_dir = Path(run_dir)
            self.skillpatch_request = request
            self.skillpatch_prepared = prepared
            self.skillpatch_environment = dict(environment)
            self.skillpatch_secrets = _diagnosis_secret_values(request, environment)
            self.skillpatch_version = version
            self.skillpatch_callback = on_verified

        def _persist_diagnostic(self, name: str, value: dict[str, Any]) -> None:
            try:
                write_json(self.skillpatch_run_dir / "backend" / name,
                           redact(value, self.skillpatch_secrets))
            except (OSError, ValueError, TypeError) as exc:
                print(f"SkillPatch diagnostic record unavailable: {type(exc).__name__}", file=sys.stderr)

        async def install_agent(self):
            await super().install_agent()
            try:
                state = await _snapshot_files(self, self.skillpatch_request)
            except Exception as exc:
                state = {"hashes": {}, "limitations": [
                    f"Initial sandbox snapshot unavailable: {type(exc).__name__}: {exc}"]}
            self._persist_diagnostic("initial_files.json", {
                **state, "schema_version": 1, "run_id": self.skillpatch_run_dir.name,
                "phase": "post_install_pre_execution", "created_at": now()})

        async def verify(self):
            rewards = await super().verify()
            original_rewards = copy.deepcopy(rewards)
            started_at = now()
            output = self.skillpatch_run_dir / "backend" / "live_discovery.json"
            try:
                # Public result property persists canonical trajectory and verifier score.
                if self.result is None:
                    raise ValueError("Verified Rollout result is unavailable")
                execution = freeze_verified_execution(self.skillpatch_run_dir,
                    self.skillpatch_prepared, self.skillpatch_version)
                session = _create_session(self, execution, started_at)
                self._persist_diagnostic("inspection_access.json", session.metadata)
                callback = self.skillpatch_callback
                if callback is None:
                    from skillpatch.integration import create_diagnosis_callback
                    callback = create_diagnosis_callback(self.skillpatch_request,
                        self.skillpatch_prepared, self.skillpatch_run_dir, self.skillpatch_environment)
                await invoke_verified_callback(callback, session, output,
                    timeout_sec=self.skillpatch_request.diagnosis_timeout_sec,
                    secrets=self.skillpatch_secrets)
            except asyncio.CancelledError:
                self._persist_diagnostic("live_discovery.json", {
                    "status": "cancelled", "started_at": started_at, "finished_at": now(),
                    "phase": "post_verification_pre_cleanup", "error": "Worker interrupted during diagnosis"})
                raise
            except Exception as exc:
                # Diagnostic failures are not benchmark agent or verifier failures.
                self._persist_diagnostic("live_discovery.json", {
                    "status": "failed", "started_at": started_at, "finished_at": now(),
                    "phase": "post_verification_pre_cleanup", "error": f"{type(exc).__name__}: {exc}"})
            return original_rewards

        async def cleanup(self):
            started_at = now()
            try:
                return await super().cleanup()
            finally:
                self._persist_diagnostic("lifecycle_cleanup.json", {
                    "started_at": started_at, "finished_at": now(),
                    "phase": "cleanup", "note": "Cleanup was attempted by the original backend lifecycle."})

    return LiveDiagnosticRollout


async def run_worker(request_path: Path, run_dir: Path, environment: Mapping[str, str]) -> int:
    request, task = load_worker_inputs(request_path, run_dir)
    if request.diagnosis_config is None or request.bench_python is None:
        raise ValueError("Live worker requires diagnosis_config and bench_python")
    version = importlib.metadata.version("benchflow")
    if version != "0.6.3" or (request.expected_backend_version and version != request.expected_backend_version):
        raise ValueError(f"Live worker supports BenchFlow 0.6.3; found {version}")
    from benchflow.rollout import Rollout, RolloutConfig
    config = RolloutConfig.from_legacy(**rollout_config_values(request, task, run_dir))
    rollout = await live_rollout_class(Rollout).create(config)
    rollout.configure_diagnosis(run_dir, request, task, environment, version)
    # Rollout.run keeps its existing finally: cleanup(), also on hook failure.
    await rollout.run()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_worker(args.request, args.run_dir.resolve(), os.environ))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(redact(f"SkillPatch worker failed: {type(exc).__name__}: {exc}", secret_values(os.environ)),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
