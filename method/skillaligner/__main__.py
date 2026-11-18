"""Run the method independently of SkillPatch's executor adapters."""
import argparse
import json
import os
from pathlib import Path

from .core import adapt, retrieve


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path,
                        help='JSON object: query, execution_specification, and retrieved_skills or skill_library')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--model', required=True)
    parser.add_argument('--api-key-env', default='METHOD_API_KEY')
    parser.add_argument('--base-url-env', default='METHOD_BASE_URL')
    parser.add_argument('--max-tokens', type=int, default=4096)
    args = parser.parse_args(argv)
    value = json.loads(args.input.read_text(encoding='utf-8'))
    if ('retrieved_skills' in value) == ('skill_library' in value):
        raise ValueError('Specify exactly one of retrieved_skills or skill_library')
    skills = value['retrieved_skills'] if 'retrieved_skills' in value else retrieve(value['query'], value['skill_library'])
    from openai import OpenAI
    with OpenAI(api_key=os.environ[args.api_key_env], base_url=os.environ[args.base_url_env],
                timeout=1800, max_retries=0) as client:
        adapt(value['query'], value['execution_specification'], skills, args.output,
              client, model=args.model, max_tokens=args.max_tokens)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
