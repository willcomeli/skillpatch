"""Run one fresh benchmark task through the BenchFlow CLI."""

import json
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from excutor.executor_adapter.models import BackendRun, PreparedTask, RunRequest
from prompts.prompt_augmenters.skill_invocation import audit_skill_reads, explicit_prompt, prepare_prompt_artifacts
from excutor.executor_adapter.storage import write_json


def build_config(request: RunRequest, task: PreparedTask, run_dir: Path) -> dict[str, Any]:
    """Use native task-bundled skills; explicit skills_dir means an override."""
    if task.skills_dir is not None and task.skills_dir.resolve() != (
        task.task_dir / "environment" / "skills"
    ).resolve():
        raise ValueError("Prepared skills must be inside task_dir/environment/skills")
    config = {
        "tasks_dir": str(task.task_dir.resolve()),
        "include": [task.task_id],
        "jobs_dir": str((run_dir / "jobs").resolve()),
        "agent": request.agent,
        "model": request.model,
        "reasoning_effort": request.reasoning_effort,
        "environment": request.sandbox,
        "sandbox_user": request.sandbox_user,
        "sandbox_setup_timeout": request.sandbox_setup_timeout,
        "agent_idle_timeout_sec": request.agent_idle_timeout,
        "usage_tracking": request.usage_tracking,
        "skill_mode": request.skill_mode,
        "skills_dir": None,
        "concurrency": 1,
        "build_concurrency": 1,
        "max_retries": 0,
        "loop_strategy": "single-shot",
    }
    prompt = explicit_prompt(request, task, run_dir=run_dir)
    if prompt is not None:
        config["prompts"] = [prompt]
    return config


def _issue(result: BackendRun, category: str, code: str, message: str) -> None:
    result.errors.append({"category": category, "code": code, "message": message})


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the supported finite range")
    return number


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant,
                       parse_float=_finite_float)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


class BenchFlowBackend:
    def __init__(self, termination_grace_sec: float = 5.0):
        self.termination_grace_sec = termination_grace_sec

    @staticmethod
    def prepare_prompt(request: RunRequest, task: PreparedTask, run_dir: Path) -> None:
        """Materialize a reviewable prompt without launching the backend."""
        prepare_prompt_artifacts(request, task, run_dir)

    def _stop(self, process: subprocess.Popen, result: BackendRun) -> None:
        """Allow asyncio cancellation/finally before escalating this process group."""
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                break
            except OSError as exc:
                _issue(result, "backend", "termination_failed", str(exc))
                return
            deadline = time.monotonic() + self.termination_grace_sec
            while time.monotonic() < deadline:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            else:
                continue
            break
        try:
            process.wait(timeout=max(1.0, self.termination_grace_sec))
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            _issue(result, "backend", "termination_unconfirmed", "Process termination could not be confirmed")

    @staticmethod
    def _cleanup_docker(task: PreparedTask, jobs_dir: Path, backend_dir: Path,
                        environment: Mapping[str, str], result: BackendRun) -> None:
        """Remove containers only after project identity and a run-local mount agree."""
        report: dict[str, Any] = {"status": "unconfirmed", "removed_container_ids": []}
        report_path = backend_dir / "cleanup.json"
        result.artifacts["cleanup"] = str(report_path)

        def docker(*arguments: str) -> str:
            command = subprocess.run(["docker", *arguments], env=dict(environment), cwd=backend_dir,
                                     capture_output=True, text=True, timeout=15, check=False)
            if command.returncode:
                raise ValueError(f"Docker {' '.join(arguments[:2])} failed with exit {command.returncode}")
            return command.stdout

        try:
            # Docker's session_id is the fresh rollout basename; never use task_id alone.
            trials = [p for p in jobs_dir.glob("*/*") if p.is_dir()
                      and re.fullmatch(re.escape(task.task_id) + r"__[0-9a-f]{8}", p.name)
                      and p.resolve().is_relative_to(jobs_dir)]
            if len(trials) != 1:
                raise ValueError("No unique fresh rollout identity available for Docker cleanup")
            trial = trials[0]
            config_path = trial / "config.json"
            if not config_path.resolve().is_relative_to(jobs_dir):
                raise ValueError("Rollout config resolves outside this run")
            config = _read_object(config_path)
            if config.get("environment") != "docker" or Path(str(config.get("task_path", ""))).name != task.task_id:
                raise ValueError("Rollout config does not identify this Docker task")
            project = re.sub(r"[^a-z0-9_-]", "-", trial.name.lower())
            if not re.match(r"^[a-z0-9]", project):
                project = "0" + project
            report["project"] = project
            selector = f"label=com.docker.compose.project={project}"
            ids = docker("ps", "-aq", "--no-trunc", "--filter", selector).split()
            if any(not re.fullmatch(r"[0-9a-f]{64}", cid) for cid in ids):
                raise ValueError("Unexpected container identity returned by Docker")
            if ids:
                # Deliberately exclude .Config.Env, commands, and other credentials.
                template = ('{"id":{{json .Id}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                            '"owned":{{json (index .Config.Labels "benchflow.owned")}},'
                            '"working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
                            '"config_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
                            '"mounts":{{json .Mounts}}}')
                records = [json.loads(docker("container", "inspect", "--format", template, cid)) for cid in ids]
                def mounted_here(record: dict[str, Any]) -> bool:
                    return any(isinstance(mount.get("Source"), str)
                               and Path(mount["Source"]).resolve().is_relative_to(trial.resolve())
                               for mount in record.get("mounts", []) if isinstance(mount, dict))
                anchors = [record for record in records if record.get("project") == project
                           and record.get("owned") == "true" and mounted_here(record)]
                if not anchors:
                    raise ValueError("No owned container is mounted to this rollout; refusing cleanup")
                anchor = anchors[0]
                for cid, record in zip(ids, records):
                    same_compose = (bool(anchor.get("working_dir")) and bool(anchor.get("config_files"))
                                    and record.get("working_dir") == anchor["working_dir"]
                                    and record.get("config_files") == anchor["config_files"])
                    if record.get("id") != cid or record.get("project") != project or not (mounted_here(record) or same_compose):
                        raise ValueError("Container ownership cannot be confirmed; refusing cleanup")
                docker("rm", "-f", *ids)
                report["removed_container_ids"] = ids
            if docker("ps", "-aq", "--no-trunc", "--filter", selector).split():
                raise ValueError("Containers remain for this rollout after cleanup")
            report["status"] = "containers_absent"
            report["scope"] = "Only this rollout's containers; images, volumes, and networks are retained"
        except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            report["issue"] = str(exc) or type(exc).__name__
            _issue(result, "environment", "cleanup_unconfirmed", report["issue"])
        finally:
            write_json(report_path, report)

    def _execute(self, command: list[str], cwd: Path, environment: Mapping[str, str],
                 stdout: Path, stderr: Path, deadline: float | None, result: BackendRun,
                 *, rollout: bool) -> int | None:
        process = None
        try:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            with stdout.open("wb") as out, stderr.open("wb") as err:
                process = subprocess.Popen(command, cwd=cwd, env=dict(environment),
                                           stdout=out, stderr=err, start_new_session=True)
                return process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            result.status = "timeout"
            _issue(result, "backend", "execution_timeout", "Execution exceeded the configured wall-clock timeout")
        except KeyboardInterrupt:
            result.status = "cancelled"
            _issue(result, "backend", "cancelled", "Execution was interrupted")
        except OSError as exc:
            result.status = "backend_error"
            _issue(result, "backend", "launch_failed", str(exc))
        finally:
            if process is not None and result.status in {"timeout", "cancelled"}:
                self._stop(process, result)
        return process.returncode if process is not None else None

    def run(self, request: RunRequest, task: PreparedTask, run_dir: Path,
            environment: Mapping[str, str]) -> BackendRun:
        result = BackendRun(status="running")
        run_dir = run_dir.resolve()
        backend_dir = run_dir / "backend"
        jobs_dir = run_dir / "jobs"
        try:
            if not request.bench_command:
                raise ValueError("bench_command must not be empty")
            # Reject any prior backend/job directory instead of reusing its evidence.
            if backend_dir.exists() or jobs_dir.exists():
                raise ValueError("Backend and jobs directories must be new for each execution")
            self.prepare_prompt(request, task, run_dir)
            config = build_config(request, task, run_dir)
            backend_dir.mkdir(parents=True)
            jobs_dir.mkdir()
            config_path = backend_dir / "config.json"
            write_json(config_path, config)
        except (OSError, ValueError) as exc:
            result.status = "backend_error"
            _issue(result, "backend", "invalid_run", str(exc))
            return result

        version_out = backend_dir / "version.stdout.log"
        version_err = backend_dir / "version.stderr.log"
        stdout = backend_dir / "stdout.log"
        stderr = backend_dir / "stderr.log"
        # LiteLLM maps DEBUG to a boolean CLI setting. Do not let an unrelated
        # host value (for example a build profile) prevent its proxy from starting.
        child_environment = dict(environment)
        removed_variables = []
        valid_debug = {"", "0", "1", "f", "false", "n", "no", "off", "on", "t", "true", "y", "yes"}
        if "DEBUG" in child_environment and child_environment["DEBUG"].lower() not in valid_debug:
            del child_environment["DEBUG"]
            removed_variables.append({"name": "DEBUG", "reason": "Not a supported boolean for LiteLLM's DEBUG option"})
        filter_path = backend_dir / "environment_filter.json"
        write_json(filter_path, {"removed": removed_variables})
        result.artifacts = {"config": str(config_path), "jobs_dir": str(jobs_dir),
                            "version_stdout": str(version_out), "version_stderr": str(version_err),
                            "stdout": str(stdout), "stderr": str(stderr),
                            "environment_filter": str(filter_path)}
        command = [*request.bench_command, "eval", "run", "--config", str(config_path)]
        write_json(backend_dir / "command.json", {"argv": command, "cwd": str(run_dir)})
        version_rc = self._execute([*request.bench_command, "--version"], run_dir, child_environment,
                                   version_out, version_err, time.monotonic() + 15,
                                   result, rollout=False)
        if result.status != "running":
            return result
        version_text = version_out.read_text(encoding="utf-8", errors="replace")
        version_text += "\n" + version_err.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.]+)?\b", version_text)
        result.backend_version = match.group(0) if match else None
        write_json(backend_dir / "version.json", {"version": result.backend_version, "return_code": version_rc})
        if version_rc != 0 or not match:
            result.status = "backend_error"
            _issue(result, "backend", "version_unavailable", "Could not establish the BenchFlow version")
            return result
        if request.expected_backend_version and result.backend_version != request.expected_backend_version:
            result.status = "backend_error"
            _issue(result, "backend", "version_mismatch",
                   f"Expected {request.expected_backend_version}; found {result.backend_version}")
            return result
        result.return_code = self._execute(command, run_dir, child_environment, stdout, stderr,
                                          None, result, rollout=True)
        if result.status in {"timeout", "cancelled"}:
            if request.sandbox == "docker":
                self._cleanup_docker(task, jobs_dir, backend_dir, environment, result)
            else:
                _issue(result, "environment", "cleanup_unconfirmed",
                       "Remote sandbox cleanup could not be confirmed after interruption")
        self._collect(result, task, jobs_dir, backend_dir)
        try:
            audit = backend_dir / "skill_reads.json"
            write_json(audit, audit_skill_reads(task, result.normalized_trajectory_path))
            result.artifacts["skill_reads"] = str(audit)
        except (OSError, ValueError, TypeError) as exc:
            # An audit failure is independent of the benchmark score.
            write_json(backend_dir / "skill_reads.json", {"status": "unavailable", "error": str(exc)})
        if result.status == "running":
            result.status = "completed"
            if result.return_code != 0:
                _issue(result, "backend", "nonzero_exit", f"BenchFlow exited with code {result.return_code}")
            if result.errors:
                categories = {error["category"] for error in result.errors}
                category = next((item for item in ("environment", "agent", "verifier", "backend", "artifact")
                                 if item in categories), "backend")
                result.status = f"{category}_error"
        return result

    def _collect(self, result: BackendRun, task: PreparedTask, jobs_dir: Path, backend_dir: Path) -> None:
        candidates = sorted(jobs_dir.rglob("result.json"))
        if len(candidates) != 1:
            code = "missing_result" if not candidates else "ambiguous_results"
            _issue(result, "artifact", code, f"Expected exactly one new rollout result; found {len(candidates)}")
            # Interrupted runs can have a trajectory before result.json is written.
            if not candidates:
                traces = sorted(jobs_dir.glob("**/trajectory/acp_trajectory.jsonl"))
                if len(traces) == 1 and traces[0].parent.parent.name.startswith(task.task_id + "__"):
                    self._normalize(result, traces[0], backend_dir)
                    verifier = traces[0].parent.parent / "verifier"
                    result.verifier_dir = verifier if verifier.is_dir() and verifier.resolve().is_relative_to(jobs_dir) else None
                elif traces:
                    _issue(result, "artifact", "unidentified_trajectory", "Cannot identify a unique trajectory for this task")
            return
        path = candidates[0]
        if not path.resolve().is_relative_to(jobs_dir):
            _issue(result, "artifact", "external_result", "Result resolves outside this run's jobs directory")
            return
        try:
            summary = _read_object(path)
        except (OSError, ValueError) as exc:
            _issue(result, "artifact", "malformed_result", str(exc))
            return
        if summary.get("task_name") != task.task_id:
            _issue(result, "artifact", "task_mismatch", "Result task_name does not match the requested task")
            return
        result.result_path = path
        result.summary = summary
        partial = summary.get("partial_trajectory")
        if not isinstance(partial, bool):
            trajectory_summary = summary.get("trajectory_summary")
            partial = trajectory_summary.get("partial_trajectory") if isinstance(trajectory_summary, dict) else None
        result.trajectory_complete = not partial if isinstance(partial, bool) else None
        if result.status in {"timeout", "cancelled"}:
            result.trajectory_complete = False if partial is True else None
        self._summary_errors(result, summary)
        self._normalize(result, path.parent / "trajectory" / "acp_trajectory.jsonl", backend_dir)
        trajectory_summary = summary.get("trajectory_summary")
        expected_steps = trajectory_summary.get("steps") if isinstance(trajectory_summary, dict) else None
        if isinstance(expected_steps, int) and not isinstance(expected_steps, bool) and expected_steps != result.trajectory_events:
            result.trajectory_complete = False
            _issue(result, "artifact", "trajectory_count_mismatch",
                   f"Summary declares {expected_steps} events; parsed {result.trajectory_events}")
        verifier = path.parent / "verifier"
        if verifier.is_dir() and verifier.resolve().is_relative_to(jobs_dir):
            result.verifier_dir = verifier
        else:
            _issue(result, "verifier", "missing_verifier", "This rollout has no verifier directory")

    @staticmethod
    def _summary_errors(result: BackendRun, summary: dict[str, Any]) -> None:
        if summary.get("error"):
            raw_category = str(summary.get("error_category") or "unknown")
            category = "agent"
            if summary.get("sandbox_startup_info") or any(word in raw_category.lower() for word in ("sandbox", "environment", "container", "build")):
                category = "environment"
            _issue(result, category, raw_category, str(summary["error"]))
        for field, category in (("verifier_error", "verifier"), ("export_error", "artifact")):
            if summary.get(field):
                _issue(result, category, str(summary.get(field + "_category") or field), str(summary[field]))
        for field, category in (("agent_timeout_info", "agent"), ("idle_timeout_info", "agent"),
                                ("verifier_timeout_info", "verifier")):
            if summary.get(field):
                _issue(result, category, field, json.dumps(summary[field], ensure_ascii=False))
                if result.status == "running":
                    result.status = "timeout"

    @staticmethod
    def _normalize(result: BackendRun, source: Path, backend_dir: Path) -> None:
        jobs_dir = backend_dir.parent / "jobs"
        if not source.is_file() or not source.resolve().is_relative_to(jobs_dir):
            result.trajectory_complete = None
            _issue(result, "artifact", "missing_trajectory", "Canonical ACP trajectory is missing or outside this run")
            return
        result.trajectory_path = source
        destination = backend_dir / "events.jsonl"
        result.normalized_trajectory_path = destination
        malformed = False
        try:
            with source.open(encoding="utf-8") as stream, destination.open("w", encoding="utf-8") as normalized:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line, parse_constant=_reject_constant, parse_float=_finite_float)
                        if not isinstance(event, dict):
                            raise ValueError("Event must be a JSON object")
                    except ValueError as exc:
                        malformed = True
                        _issue(result, "artifact", "malformed_trajectory", f"Line {number}: {exc}")
                        continue
                    result.trajectory_events += 1
                    record = {"event_id": f"T{result.trajectory_events:06d}",
                              "source": {"path": str(source), "line": number}, "event": event}
                    normalized.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        except (OSError, UnicodeError) as exc:
            malformed = True
            _issue(result, "artifact", "trajectory_read_failed", str(exc))
        if not result.trajectory_events:
            malformed = True
            _issue(result, "artifact", "empty_trajectory", "Canonical trajectory contains no valid events")
        if malformed:
            result.trajectory_complete = False
