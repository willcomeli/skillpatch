"""Small, explicit command line for method adapters and baseline training."""

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog='method-input-adapters')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('skillopt-export', 'trace2skill-export'):
        sub = commands.add_parser(name)
        sub.add_argument('sources', nargs='+', type=Path)
        sub.add_argument('--output', required=True, type=Path)
    for name in ('skillopt-train', 'trace2skill-evolve'):
        sub = commands.add_parser(name)
        sub.add_argument('--output', required=True, type=Path)
        sub.add_argument('--env-file', type=Path)
        sub.add_argument('--model', default='deepseek-flash')
        sub.add_argument('--api-key-env', default='DEEPSEEK_API_KEY')
        sub.add_argument('--base-url-env', default='DEEPSEEK_BASE_URL')
        if name == 'skillopt-train':
            sub.add_argument('--manifest', required=True, type=Path)
            sub.add_argument('--initial-skill', required=True, type=Path)
            sub.add_argument('--config', type=Path)
        else:
            sub.add_argument('sources', nargs='+', type=Path)
            sub.add_argument('--initial-skill-dir', required=True, type=Path)
            sub.add_argument('--validation-manifest', type=Path)
            sub.add_argument('--max-output-tokens', type=int, default=32768)
    args = parser.parse_args(argv)
    if args.command.endswith('-export'):
        if args.command == 'skillopt-export':
            from .skillopt.results import export_results
        else:
            from .trace2skill.results import export_results
        rows = export_results(args.sources, args.output)
        print(json.dumps({'output': str(args.output.resolve()), 'tasks': len(rows)}, ensure_ascii=False))
        return 0
    from set_up.environment import load_environment
    environment = load_environment(args.env_file)
    options = dict(model=args.model, api_key_env=args.api_key_env, base_url_env=args.base_url_env)
    if args.command == 'skillopt-train':
        from .skillopt.training import train
        train(args.manifest, args.initial_skill, args.output, environment, config=args.config, **options)
    else:
        from .trace2skill.evolution import evolve
        evolve(args.sources, args.initial_skill_dir, args.output, environment,
               validation_manifest=args.validation_manifest, max_output_tokens=args.max_output_tokens, **options)
    print(f'Output: {args.output.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
