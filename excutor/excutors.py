"""One new run per invocation; adapters/backend are replaceable independently."""

from pathlib import Path
from typing import Callable, Mapping

from excutor.executor_adapter.base import ExecutionAdapter
from set_up.environment import redact, redact_artifacts, secret_values
from excutor.executor_adapter.models import ExecutionResult, RunRequest
from excutor.executor_adapter.storage import tree_hashes
from excutor.excutor_runners.base import RunBackend
from excutor.executor_adapter.storage import hash_manifest, jsonable, now, sha256_file, write_json


class _SkillDeploymentError(ValueError):
    """A requested patch pack was not prepared exactly; never start execution."""


def _verify_skill_deployment(prepared, expected: str, run_dir: Path, phase: str) -> None:
    record = {"schema_version": 1, "phase": phase, "status": "failed",
              "expected_skill_digest": expected, "reported_skill_digest": prepared.skill_digest,
              "actual_skill_digest": None, "skills_dir": prepared.skills_dir}
    try:
        if prepared.skills_dir is None:
            raise ValueError("Prepared task has no Skill pack")
        if not prepared.skills_dir.resolve().is_relative_to((run_dir / "input").resolve()):
            raise ValueError("Prepared Skill pack is outside the independent execution snapshot")
        actual = hash_manifest(tree_hashes(prepared.skills_dir))
        record["actual_skill_digest"] = actual
        if actual != expected or prepared.skill_digest != actual:
            raise ValueError("Prepared Skill contents or adapter digest do not match the pinned deployment")
    except (OSError, ValueError) as exc:
        record["error"] = str(exc)
        write_json(run_dir / "skill_deployment_verification.json", record)
        raise _SkillDeploymentError(str(exc)) from exc
    record["status"] = "verified"
    write_json(run_dir / "skill_deployment_verification.json", record)


class Executor:
    def __init__(self, adapter: ExecutionAdapter, backend: RunBackend):
        self.adapter = adapter
        self.backend = backend


    def run(self, request: RunRequest, run_dir: Path, environment: Mapping[str, str],
            *, prepare_only: bool = False,
            progress: Callable[[str], None] | None = None,
            expected_skill_digest: str | None = None,
            patch_skills_dir: Path | None = None) -> ExecutionResult:
        run_dir = run_dir.expanduser().resolve()
        # Refuse reuse: BenchFlow otherwise supports resuming historical jobs.
        run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        secrets = secret_values(environment)
        result = ExecutionResult(run_id=run_dir.name, benchmark=request.benchmark,
                                 task_id=request.task_dir.name, status="preparing", started_at=now())

        def save() -> None:
            write_json(run_dir / "result.json", redact(jsonable(result), secrets))

        def report(message: str) -> None:
            if progress:
                progress(message)

        write_json(run_dir / "request.json", redact(jsonable(request), secrets))
        # Only explicit non-secret settings: never persist the whole process env.
        settings = {name: environment[name] for name in (
            "DEEPSEEK_THINKING", "DEEPSEEK_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL"
        ) if name in environment}
        write_json(run_dir / "runtime_environment.json", redact({
            "provider_settings": settings,
            "credential_values_saved": False,
        }, secrets))
        save()
        try:
            if patch_skills_dir is not None:
                from excutor.patch_delivery.packs import prepare_patch_request
                request, deployment = prepare_patch_request(request, patch_skills_dir)
                digest = deployment["deployed_pack_digest"]
                if expected_skill_digest is not None and expected_skill_digest != digest:
                    raise ValueError("Selected patch pack differs from expected_skill_digest")
                expected_skill_digest = digest
                write_json(run_dir / "patch_deployment.json", deployment)
                write_json(run_dir / "request.json", redact(jsonable(request), secrets))
            report("Preparing independent task and Skill snapshots")
            result.prepared = self.adapter.prepare(request, run_dir / "input")
            result.task_id = result.prepared.task_id
            # This opt-in full-pack contract is used for pinned patch sets only;
            # ordinary adapters may keep their existing native digest schemes.
            if expected_skill_digest is not None:
                _verify_skill_deployment(result.prepared, expected_skill_digest, run_dir, "after_adapter_prepare")
            prepare_prompt = getattr(self.backend, "prepare_prompt", None)
            if callable(prepare_prompt):
                report("Preparing the task execution prompt")
                prepare_prompt(request, result.prepared, run_dir)
                if expected_skill_digest is not None:
                    _verify_skill_deployment(result.prepared, expected_skill_digest, run_dir, "after_prompt_prepare")
            result.status = "prepared" if prepare_only else "running"
            save()
            if not prepare_only:
                report("Starting one fresh backend run; logs are in the run directory")
                execution = self.backend.run(request, result.prepared, run_dir, environment)
                result.execution = execution
                result.status = execution.status
                result.errors.extend(execution.errors)
                report("Collecting benchmark evaluation feedback")
                result.feedback = self.adapter.evaluate(execution)
                if result.status == "completed":
                    if result.feedback.status == "error":
                        result.status = "verifier_error"
                    elif result.feedback.status == "unavailable":
                        result.status = "artifact_error"
                        result.errors.append({"stage": "evaluation", "message": "No valid benchmark evaluation was produced"})
                for relative, expected in result.prepared.file_hashes.items():
                    path = result.prepared.task_dir / relative
                    if not path.is_file() or sha256_file(path) != expected:
                        result.errors.append({"stage": "provenance", "message": f"Prepared input changed during execution: {relative}"})
                        if result.status == "completed":
                            result.status = "artifact_error"
        except KeyboardInterrupt:
            result.status = "cancelled"
            result.errors.append({"stage": "execution", "message": "Interrupted by user"})
        except Exception as exc:
            preparation_error = result.prepared is None or isinstance(exc, _SkillDeploymentError)
            stage = "preparation" if preparation_error else "execution"
            result.status = "preparation_error" if preparation_error else "backend_error"
            result.errors.append({"stage": stage, "message": f"{type(exc).__name__}: {exc}"})
        finally:
            result.finished_at = now()
            try:
                changed = redact_artifacts(run_dir, secrets)
                write_json(run_dir / "redaction.json", {"method": "known environment credential values", "files": changed})
            except OSError as exc:
                result.errors.append({"stage": "redaction", "message": f"{type(exc).__name__}: {exc}"})
                result.status = "artifact_error"
            save()
        return result
