"""Compatibility entry for older Boundary-Aware evaluation commands."""

import argparse
from pathlib import Path
import sys

from excutor.executor_adapter.storage import write_json
from method_input_adapters.archive import read_json
from set_up.environment import load_environment, redact, secret_values
from set_up.scienceworld_evaluation import CONFIG, ROOT, TEST_TASKS, compare, evaluate, index_tasks
from set_up.scienceworld_evolution import evolve


def compare_with_saved_initial(output, initial_eval):
    if read_json(output / "test_plan.json") != read_json(initial_eval / "test_plan.json"):
        raise ValueError("The saved original-Skill evaluation used different test inputs or execution settings")
    tasks = index_tasks(read_json(output / "test_tasks.json"), "test")
    initial = read_json(initial_eval / "initial/test/result.json")
    patched = read_json(output / "patched/test/result.json")
    rows = [{row["task_id"]: row for row in result["rows"]} for result in (initial, patched)]
    write_json(output / "test_comparison.json", compare(tasks, *rows))


def run_boundary_evaluation(args, evolution_env, execution_env, *, client=None):
    output = args.output.expanduser().resolve()
    stage3 = evolve(output, args.source_plan, args.config, args.evolution_config, args.test_manifest,
                    evolution_env, execution_env, induction="boundary-aware", workers=args.workers, client=client)
    evaluation = output / "evaluation"
    eval_args = argparse.Namespace(**{**vars(args), "output": evaluation, "stage3_run": stage3})
    code = evaluate(eval_args, "patched", execution_env)
    if code != 130 and args.initial_eval is not None and not args.prepare_only:
        compare_with_saved_initial(evaluation, args.initial_eval.expanduser().resolve(strict=True))
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-plan", required=True, type=Path, help="Stage 2 plan.json with Stage 1 issue paths")
    parser.add_argument("--output", required=True, type=Path, help="New or resumable Boundary-Aware run directory")
    parser.add_argument("--evolution-config", type=Path, default=CONFIG / "evolution.toml")
    parser.add_argument("--evolution-env-file", type=Path, default=CONFIG / "scienceworld_evolve.env")
    parser.add_argument("--config", type=Path, default=CONFIG / "execution.toml")
    parser.add_argument("--env-file", type=Path, default=CONFIG / "scienceworld.env")
    parser.add_argument("--test-manifest", type=Path, default=TEST_TASKS)
    parser.add_argument("--initial-eval", type=Path, help="Reuse original-Skill scores from an existing evaluation")
    parser.add_argument("--workers", type=int, default=2, help="Parallel method sessions in Stage 2 and Stage 3")
    parser.add_argument("--attempts", type=int, default=3, help="Maximum new execution attempts per evaluation phase")
    parser.add_argument("--server-port-start", type=int, default=9011)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.attempts < 1 or not 1 <= args.server_port_start <= 65535:
        parser.error("--workers and --attempts must be positive and --server-port-start must be valid")
    evolution_env, execution_env = {}, {}
    try:
        evolution_env = load_environment(args.evolution_env_file)
        execution_env = load_environment(args.env_file)
        return run_boundary_evaluation(args, evolution_env, execution_env)
    except KeyboardInterrupt:
        return 130
    except BlockingIOError:
        print(f"Boundary-Aware evaluation is already running in {args.output}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError) as error:
        secrets = {*secret_values(evolution_env), *secret_values(execution_env)}
        print(redact(str(error), secrets), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
