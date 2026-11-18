"""Run both ScienceWorld evaluations and save periodic progress on the server."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from excutor.executor_adapter.storage import now, write_json
from method_input_adapters.archive import read_json
from set_up.scienceworld_evaluation import CONFIG, ROOT, TEST_TASKS


PHASES = {"initial": ("test",), "patched": ("replay", "test")}


@dataclass
class Worker:
    command: list[str]
    process: subprocess.Popen | None = None
    status: str = "pending"
    launches: int = 0
    exit_code: int | None = None
    restart_at: float | None = None
    saved_at_launch: int = 0
    no_progress_failures: int = 0


def phase_status(output, name, phase):
    folder = output / name / phase
    manifest = output / "test_tasks.json" if phase == "test" else output / name / "replay_tasks.json"
    report = folder / "result.json"
    summary = read_json(report) if report.is_file() else {}
    saved = {row["task_id"] for row in summary.get("rows", [])}
    latest = {}
    last_run = None
    for attempt in sorted(folder.glob("attempt-*/result.json")):
        latest = read_json(attempt)
        last_run = str(attempt)
        for path in latest.get("task_results", []):
            try:
                saved.add(read_json(path)["task_id"])
            except (OSError, ValueError, KeyError):
                continue
    return {
        "expected": len(read_json(manifest)) if manifest.is_file() else None,
        "saved": len(saved), "scored": summary.get("summary", {}).get("count", 0),
        "status": summary.get("status", latest.get("status", "pending")),
        "last_run": last_run, "last_run_status": latest.get("status"),
    }


def progress(output, name):
    return {phase: phase_status(output, name, phase) for phase in PHASES[name]}


def saved_count(phases):
    return sum(phase["saved"] for phase in phases.values())


def signal_worker(worker, sig):
    try:
        os.killpg(worker.process.pid, sig)
    except ProcessLookupError:
        pass


def stop_workers(workers):
    active = [worker for worker in workers.values() if worker.process is not None]
    for worker in active:
        signal_worker(worker, signal.SIGINT)
    for worker in active:
        try:
            worker.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            signal_worker(worker, signal.SIGKILL)
            worker.process.wait()
        worker.exit_code = worker.process.returncode
        worker.process = None
        worker.status = "cancelled"


def supervise(output, commands, interval, *, restart_delay=60):
    workers = {name: Worker(command) for name, command in commands.items()}
    started_at = now()
    next_check = time.monotonic() + interval

    def record(event, status="running"):
        delay = max(0, next_check - time.monotonic())
        scheduled = datetime.now(timezone.utc) + timedelta(seconds=delay)
        snapshot = {
            "event": event, "status": status, "pid": os.getpid(), "started_at": started_at,
            "checked_at": now(), "check_interval_sec": interval,
            "next_check_at": scheduled.isoformat() if status == "running" else None,
            "workers": {name: {
                "status": worker.status, "pid": worker.process.pid if worker.process else None,
                "exit_code": worker.exit_code, "launches": worker.launches,
                "no_progress_failures": worker.no_progress_failures, "phases": progress(output, name),
            } for name, worker in workers.items()},
        }
        comparison = output / "test_comparison.json"
        if comparison.is_file():
            summary = read_json(comparison)
            snapshot["test_comparison"] = {
                "path": str(comparison), "status": summary["status"], "paired": summary["paired"],
            }
        write_json(output / "status.json", snapshot)
        with (output / "checks.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        print(f"{snapshot['checked_at']} {event}", flush=True)

    def start(name, worker):
        worker.saved_at_launch = saved_count(progress(output, name))
        with (output / f"{name}.log").open("ab") as log:
            worker.process = subprocess.Popen(worker.command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                              stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        worker.launches += 1
        worker.restart_at = None
        worker.exit_code = None
        worker.status = "running"

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signals = [signal.SIGINT, signal.SIGTERM]
    if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:
        signals.append(signal.SIGHUP)
    handlers = {sig: signal.signal(sig, interrupt) for sig in signals}
    try:
        for name, worker in workers.items():
            start(name, worker)
        record("started")
        while True:
            current = time.monotonic()
            for name, worker in workers.items():
                if worker.restart_at is not None and current >= worker.restart_at:
                    start(name, worker)
                    record(f"{name}_restarted")
                if worker.process is None:
                    continue
                code = worker.process.poll()
                if code is None:
                    continue
                worker.process = None
                worker.exit_code = code
                if code == 0:
                    worker.status = "completed"
                elif code == 130:
                    worker.status = "cancelled"
                else:
                    if saved_count(progress(output, name)) > worker.saved_at_launch:
                        worker.no_progress_failures = 0
                    else:
                        worker.no_progress_failures += 1
                    if worker.no_progress_failures < 2:
                        worker.restart_at = current + restart_delay
                        worker.status = "waiting_to_restart"
                    else:
                        worker.status = "incomplete"
                record(f"{name}_exited")
            if all(worker.process is None and worker.restart_at is None for worker in workers.values()):
                finished = all(worker.status == "completed" for worker in workers.values())
                record("finished", "completed" if finished else "incomplete")
                return 0 if finished else 2
            if current >= next_check:
                next_check += interval
                record("periodic_check")
            wake_at = min([next_check, *(worker.restart_at for worker in workers.values()
                                         if worker.restart_at is not None)])
            time.sleep(max(0, min(5, wake_at - time.monotonic())))
    except KeyboardInterrupt:
        stop_workers(workers)
        record("stopped", "cancelled")
        return 130
    except Exception:
        stop_workers(workers)
        raise
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def configure_parser(parser):
    parser.add_argument("--stage3-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=CONFIG / "execution.toml")
    parser.add_argument("--env-file", type=Path, default=CONFIG / "scienceworld.env")
    parser.add_argument("--test-manifest", type=Path, default=TEST_TASKS)
    parser.add_argument("--check-interval", type=int, default=3600, help="Seconds between progress checks")
    parser.set_defaults(handler=run_command, command_parser=parser)


def run_command(args):
    if args.check_interval < 1:
        args.command_parser.error("--check-interval must be positive")
    output = args.output.expanduser().resolve()
    stage3 = args.stage3_run.expanduser().resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = ["--output", str(output), "--config", str(args.config.expanduser().resolve()),
               "--env-file", str(args.env_file.expanduser().resolve()),
               "--test-manifest", str(args.test_manifest.expanduser().resolve())]
    commands = {
        "initial": [sys.executable, "-Bu", "-m", "set_up.cli", "scienceworld", "evaluate", *options],
        "patched": [sys.executable, "-Bu", "-m", "set_up.cli", "scienceworld", "evaluate",
                    *options, "--stage3-run", str(stage3)],
    }
    with (output / ".supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            args.command_parser.error(f"An evaluation supervisor is already running in {output}")
        (output / "supervisor.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        return supervise(output, commands, args.check_interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    configure_parser(parser)
    return run_command(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
