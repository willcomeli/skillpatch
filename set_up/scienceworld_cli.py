"""ScienceWorld commands registered by the shared CLI."""

import argparse
import json
from pathlib import Path
import sys
import tomllib

from excutor.executor_adapter.storage import sha256_file
from excutor.scienceworld_runners.prompts import SCIENCEWORLD_ACTION_PROMPT_PATH
from method.skillpatch.config import load_discovery_config
from method.skillpatch.patch_consolidation.config import load_consolidation_config
from method.skillpatch.patch_generation.config import load_stage2_config
from method.skillpatch.patch_generation.models import INDUCTIONS, NOTE_EXAMPLE_FORMAT
from set_up.environment import load_environment, redact, secret_values
from set_up.native_config import load_native_config
from set_up.scienceworld_evaluation import CONFIG, collect_train, compare_test_runs, evaluate
from set_up.scienceworld_evolution import diagnose_training, evolve, generation_prompts


def add_commands(parser, *, supervisor):
    commands = parser.add_subparsers(dest="scienceworld_command", required=True)
    definitions = (
        ("describe", "Show the resolved models, prompts and task manifests"),
        ("collect", "Collect or resume unpatched train trajectories"),
        ("diagnose", "Generate a shared Stage 1 issue index from saved train runs"),
        ("evolve", "Generate Stage 2 candidates and deliver the Stage 3 Skill bundle"),
        ("evaluate", "Replay source tasks and/or score the held-out test manifest"),
        ("compare", "Compare methods on the same saved test tasks"),
    )
    for name, help_text in definitions:
        command = commands.add_parser(name, help=help_text)
        if name == "compare":
            command.add_argument("--vanilla", type=Path, help="Original-Skill evaluation root")
            command.add_argument("--standard", type=Path, help="Standard SkillPatch evaluation root")
            command.add_argument("--boundary-aware", type=Path, help="Boundary-Aware evaluation root")
            command.add_argument("--output", required=True, type=Path, help="Comparison JSON file")
            command.set_defaults(handler=run)
            continue

        command.add_argument("--experiment", type=Path, default=CONFIG / "experiment.toml")
        command.add_argument("--config", type=Path, help="Override the execution TOML")
        command.add_argument("--env-file", type=Path, help="Override the execution environment file")
        if name != "describe":
            command.add_argument("--output", type=Path, required=True)
        if name != "collect":
            command.add_argument("--test-manifest", type=Path)
        if name in {"describe", "diagnose", "evolve"}:
            command.add_argument("--evolution-config", type=Path)
            command.add_argument("--evolution-env-file", type=Path)
        if name in {"collect", "evaluate"}:
            command.add_argument("--attempts", type=int, default=3)
            command.add_argument("--server-port-start", type=int, help="Base port for the local environment service")
            command.add_argument("--prepare-only", action="store_true")
        if name in {"diagnose", "evolve"}:
            command.add_argument("--workers", type=int, default=2)
        if name in {"collect", "describe"}:
            command.add_argument("--train-manifest", type=Path)
        elif name == "diagnose":
            command.add_argument("--train-run", type=Path, action="append", required=True,
                                 help="Collected or existing native train archive; repeat for disjoint shards")
        elif name == "evolve":
            command.add_argument("--issues", type=Path, required=True,
                                 help="Stage 1 issues.json or an existing Stage 2 source plan")
            command.add_argument("--induction", choices=INDUCTIONS, default="standard")
        elif name == "evaluate":
            command.add_argument("--stage3-run", type=Path, help="Stage 3 bundle directory; omit for Vanilla")
            command.add_argument("--phase", choices=("replay", "test", "both"),
                                 help="Default: test for Vanilla; replay and test with a bundle")
        command.set_defaults(handler=run)
    supervisor(commands.add_parser("supervise", help="Keep evaluation running and save progress"))


def experiment_inputs(args):
    profile = args.experiment.expanduser().resolve(strict=True)
    with profile.open("rb") as stream:
        document = tomllib.load(stream)

    def paths(name, expected):
        section = document.get(name)
        if (not isinstance(section, dict) or set(section) != expected
                or any(not isinstance(path, str) or not path.strip() for path in section.values())):
            raise ValueError(f"Expected [{name}] paths in {profile}: {', '.join(sorted(expected))}")
        return {key: (profile.parent / value).resolve() for key, value in section.items()}

    execution = paths("execution", {"config", "env_file", "action_prompt", "train_manifest", "test_manifest"})
    evolution = paths("evolution", {"config", "env_file"})
    if execution["action_prompt"] != SCIENCEWORLD_ACTION_PROMPT_PATH:
        raise ValueError("The ScienceWorld experiment must reference the native action prompt")
    return argparse.Namespace(
        **{**vars(args), "config": args.config or execution["config"],
           "env_file": args.env_file or execution["env_file"], "action_prompt": execution["action_prompt"],
           "train_manifest": getattr(args, "train_manifest", None) or execution["train_manifest"],
           "test_manifest": getattr(args, "test_manifest", None) or execution["test_manifest"],
           "evolution_config": getattr(args, "evolution_config", None) or evolution["config"],
           "evolution_env_file": getattr(args, "evolution_env_file", None) or evolution["env_file"]},
    )


def describe(args, execution_env, evolution_env):
    execution = load_native_config(args.config, execution_env)
    discovery = load_discovery_config(args.evolution_config, environment=evolution_env)
    generation = load_stage2_config(args.evolution_config, environment=evolution_env)
    consolidation = load_consolidation_config(args.evolution_config, environment=evolution_env)

    def prompt(path):
        return {"path": str(path), "sha256": sha256_file(path)}

    def stage(config):
        return {"model": config.agent.model, "harness": config.agent.harness, "prompt": prompt(config.prompt_path)}

    boundary = generation.agent_config(evolution_env)
    return {
        "benchmark": execution.benchmark,
        "collect_replay_test": {"model": execution.model, "agent": "ScienceWorldActionAgent",
                                "prompt": prompt(args.action_prompt), "skill": str(execution.skill_path),
                                "train_manifest": str(args.train_manifest), "test_manifest": str(args.test_manifest)},
        "stage1": stage(discovery),
        "standard": {"stage2": stage(generation.agent_config(evolution_env)),
                     "stage3": stage(consolidation.proposal_config(evolution_env))},
        "boundary_aware": {
            "stage2": {"model": boundary.agent.model, "harness": boundary.agent.harness,
                       "prompts": {name: prompt(path) for name, path in
                                   generation_prompts(generation, evolution_env, "boundary-aware").items()}},
            "stage3": stage(consolidation.proposal_config(evolution_env, patch_format=NOTE_EXAMPLE_FORMAT)),
        },
    }


def run(args):
    command = args.scienceworld_command
    execution_env, evolution_env = {}, {}
    try:
        if command == "compare":
            runs = {
                name: (folder, variant)
                for name, folder, variant in (
                    ("vanilla", args.vanilla, "initial"), ("standard", args.standard, "patched"),
                    ("boundary-aware", args.boundary_aware, "patched"),
                ) if folder is not None
            }
            report = compare_test_runs(runs, args.output)
            print(json.dumps({key: report[key] for key in ("status", "paired", "expected", "methods")},
                             ensure_ascii=False, indent=2))
            return 0

        args = experiment_inputs(args)
        if command in {"collect", "evaluate"}:
            if args.server_port_start is None:
                args.server_port_start = 8711 if command == "collect" else 8911 if args.stage3_run else 8811
            if args.attempts < 1 or not 1 <= args.server_port_start <= 65535:
                raise ValueError("--attempts must be positive and --server-port-start must be a valid port")
        if command in {"diagnose", "evolve"} and args.workers < 1:
            raise ValueError("--workers must be positive")
        execution_env = load_environment(args.env_file)
        if command in {"describe", "diagnose", "evolve"}:
            evolution_env = load_environment(args.evolution_env_file)

        if command == "describe":
            print(json.dumps(describe(args, execution_env, evolution_env), ensure_ascii=False, indent=2))
        elif command == "collect":
            return collect_train(args, execution_env)
        elif command == "diagnose":
            manifest = diagnose_training(args.output, args.train_run, args.config, args.evolution_config,
                                         args.test_manifest, evolution_env, execution_env, workers=args.workers)
            print(f"Stage 1 issues: {manifest}", flush=True)
        elif command == "evolve":
            stage3 = evolve(args.output, args.issues, args.config, args.evolution_config, args.test_manifest,
                            evolution_env, execution_env, induction=args.induction, workers=args.workers)
            print(f"Stage 3 bundle: {stage3}", flush=True)
        elif command == "evaluate":
            variant = "patched" if args.stage3_run else "initial"
            phases = None if args.phase is None else ("replay", "test") if args.phase == "both" else (args.phase,)
            return evaluate(args, variant, execution_env, phases=phases)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError) as error:
        secrets = {*secret_values(execution_env), *secret_values(evolution_env)}
        print(redact(f"error: {error}", secrets), file=sys.stderr)
        return 2
