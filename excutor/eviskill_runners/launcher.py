"""Compose native Runners, model clients and Skill banks outside the CLI."""

import json
import inspect
import shutil
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from excutor.executor_adapter.storage import write_json, tree_hashes, now
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from set_up.environment import redact, secret_values, redact_artifacts
from . import AlfWorldRunner, ScienceWorldRunner, AppWorldRunner
from .agents import AlfWorldActionAgent, ScienceWorldActionAgent, AppWorldActionAgent

RUNNERS = {'alfworld': AlfWorldRunner, 'scienceworld': ScienceWorldRunner, 'appworld': AppWorldRunner}
AGENTS = {'alfworld': AlfWorldActionAgent, 'scienceworld': ScienceWorldActionAgent, 'appworld': AppWorldActionAgent}


class ChatClient:
    """Small non-streaming Chat Completions-compatible client; no implicit retries."""
    def __init__(self, config, environment):
        self.config = config
        self.key = environment.get(config.api_key_env)
        self.url = config.base_url or environment.get(config.base_url_env)
        if not self.key or not self.url:
            raise ValueError('Native execution requires configured API key and base URL')

    def chat(self, messages):
        body = {'model': self.config.model, 'messages': messages,
                'temperature': self.config.temperature, 'max_tokens': self.config.max_tokens}
        request = Request(self.url.rstrip('/') + '/chat/completions',
                          data=json.dumps(body).encode(),
                          headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.config.timeout_sec) as response:
                result = json.load(response)
        except HTTPError as error:
            raise ValueError(f'Model HTTP request failed: {error.code}') from None
        try:
            choice = result['choices'][0]
            content = choice['message']['content']
            if choice.get('finish_reason') != 'stop' or not isinstance(content, str) or not content.strip():
                raise ValueError('Model returned an incomplete or empty action')
            return content
        except (KeyError, IndexError, TypeError):
            raise ValueError('Model response does not contain a text action') from None


class FileSkillBank:
    def __init__(self, path):
        self.text = path.read_text(encoding='utf-8') if path else ''

    def retrieve(self, query, **kwargs):
        return {'general_skills': [], 'task_specific_skills': []}

    def format_for_prompt(self, memory):
        return self.text

    def retrieved_skill_ids(self, memory):
        return ['skill_md'] if self.text else []


def runner_arguments(config, output):
    options = dict(config.runner_options)
    if config.benchmark == 'appworld':
        if 'output_dir' in options:
            raise ValueError('AppWorld output_dir is managed by the launcher')
        options['output_dir'] = str(output)
    cls = RUNNERS[config.benchmark]
    try:
        inspect.signature(cls).bind(object(), **options)
    except TypeError as error:
        raise ValueError(f'Invalid {config.benchmark} runner_options: {error}') from None
    return options


def create_runner(config, output, environment):
    client = ChatClient(config, environment)
    return RUNNERS[config.benchmark](AGENTS[config.benchmark](client), **runner_arguments(config, output))


def run_native(config, output: Path, environment, *, prepare_only=False, patch_skill_dir=None):
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    secrets = secret_values(environment)
    key = environment.get(config.api_key_env)
    if key:
        secrets = tuple(set(secrets) | {key})
    state = {'benchmark': config.benchmark, 'run_id': output.name, 'status': 'preparing',
             'started_at': now(), 'task_results': [], 'errors': []}
    runner = None
    try:
        tasks = json.loads(config.tasks_path.read_text(encoding='utf-8'))
        if not isinstance(tasks, list):
            tasks = [tasks]
        if not tasks or any(not isinstance(task, (str, int, dict)) or isinstance(task, bool) for task in tasks):
            raise ValueError('task.path must contain a nonempty task reference or task-reference list')
        options = runner_arguments(config, output)
        inputs = output / 'input'
        inputs.mkdir()
        write_json(inputs / 'tasks.json', tasks)
        bank = FileSkillBank(config.skill_path)
        skill_path = None
        if config.skill_path:
            skill_path = inputs / 'SKILL.md'
            skill_path.write_text(bank.text, encoding='utf-8')
            bank = FileSkillBank(skill_path)
        selected = None
        if patch_skill_dir is not None:
            if skill_path is None:
                raise ValueError('Patch delivery requires task.skill_path and with-skill')
            source = Path(patch_skill_dir).expanduser().absolute()
            if output.is_relative_to(source.resolve()):
                raise ValueError('Run output must not be inside the selected patch Skill')
            before = tree_hashes(source)
            selected = inputs / 'patched_skill'
            shutil.copytree(source, selected)
            if tree_hashes(source) != before or tree_hashes(selected) != before:
                raise ValueError('Patched Skill changed while copying')
            # Preflight pairing, version binding and prompt/base matching before model or server startup.
            checked = PatchedSkillBank(bank, selected)
            checked.format_for_prompt({})
        write_json(output / 'config.json', redact(config.__dict__, secrets))
        write_json(output / 'plan.json', {'benchmark': config.benchmark, 'task_count': len(tasks),
                                         'runner_options': redact(options, secrets),
                                         'patch_skill_dir': selected, 'skill_path': skill_path,
                                         'model_request_timeout_sec': config.timeout_sec})
        if prepare_only:
            state['status'] = 'prepared'
        else:
            runner = create_runner(config, output, environment)
            state['status'] = 'running'
            write_json(output / 'result.json', state)
            for index, task in enumerate(tasks, 1):
                kwargs = {'patch_skill_dir': selected} if selected is not None else {}
                row = runner.run_task(task, bank, **kwargs)
                path = output / 'tasks' / f'task_{index:06d}.json'
                write_json(path, redact(row, secrets))
                state['task_results'].append(str(path))
                write_json(output / 'result.json', state)
            state['status'] = 'completed'
    except KeyboardInterrupt:
        state['status'] = 'cancelled'
    except Exception as error:
        state['status'] = 'backend_error'
        state['errors'].append(redact(f'{type(error).__name__}: {error}', secrets))
    finally:
        if runner is not None:
            try:
                runner.close()
            except Exception as error:
                state['errors'].append(redact(f'Cleanup failed: {error}', secrets))
                if state['status'] == 'completed':
                    state['status'] = 'backend_error'
        state['finished_at'] = now()
        try:
            redact_artifacts(output, secrets)
        except OSError as error:
            state['status'] = 'artifact_error'
            state['errors'].append(redact(str(error), secrets))
        write_json(output / 'result.json', redact(state, secrets))
    return redact(state, secrets)
