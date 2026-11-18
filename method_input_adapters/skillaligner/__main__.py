"""Run adaptation or a task manifest through SkillPatch's shared executor."""
import argparse
import json
import re
from pathlib import Path

from method.skillaligner import adapt, render_guide
from .adapter import build_method_input, execute_guide
from ..chat_compat import ChatClient
from ..archive import write_json


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--env-file', type=Path)
    p.add_argument('--model', required=True)
    p.add_argument('--api-key-env', default='METHOD_API_KEY')
    p.add_argument('--base-url-env', default='METHOD_BASE_URL')
    p.add_argument('--max-tokens', type=int, default=4096)
    p.add_argument('--adapt-only', action='store_true')
    args = p.parse_args(argv)
    from set_up.environment import load_environment
    from ..execution import load_tasks, SharedExecutorBridge
    from openai import OpenAI
    environment = load_environment(args.env_file)
    raw = json.loads(args.manifest.read_text())
    entries = raw if args.adapt_only else load_tasks(args.manifest)
    if not isinstance(entries, list) or not entries:
        raise ValueError('Manifest must contain tasks')
    ids = set()
    prepared = []
    for row in entries:
        identity = row.get('id', '')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', identity) or identity in ids:
            raise ValueError('Task ids must be safe and unique')
        ids.add(identity)
        _, _, skills = build_method_input(row)
        prepared.append((row, skills))
    args.output.mkdir(parents=True, exist_ok=False)
    bridge = SharedExecutorBridge(environment)
    with OpenAI(api_key=environment[args.api_key_env], base_url=environment[args.base_url_env],
                timeout=1800, max_retries=0) as client:
        client = ChatClient(client)
        results = []
        for row, skills in prepared:
            folder = args.output / row['id']
            folder.mkdir()
            guide = adapt(row['query'], row['execution_specification'], skills, folder / 'adaptation',
                          client, model=args.model, max_tokens=args.max_tokens)
            text = render_guide(guide)
            path = folder / 'guide.md'
            path.write_text(text, encoding='utf-8')
            result = {'id': row['id'], 'guide': str(path.resolve()), 'adaptation_model': args.model}
            if not args.adapt_only:
                archive = execute_guide(row, guide, folder / 'execution', bridge)
                from ..archive import load_archive
                record = load_archive(archive)
                result.update(archive=str(archive), success=record.success, score=record.score)
            results.append(result)
            write_json(args.output / 'results.json', results)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
