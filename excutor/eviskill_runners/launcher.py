"""Compose native Runners, model clients and Skill banks outside the CLI."""

import json
import inspect
import shutil
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from excutor.executor_adapter.storage import write_json, tree_hashes, now
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from set_up.environment import redact, secret_values, redact_artifacts
from . import AlfWorldRunner, ScienceWorldRunner, AppWorldRunner
from .agents import AlfWorldActionAgent, ScienceWorldActionAgent, AppWorldActionAgent
from .model_errors import ModelActionBudgetExceeded

RUNNERS = {'alfworld': AlfWorldRunner, 'scienceworld': ScienceWorldRunner, 'appworld': AppWorldRunner}
AGENTS = {'alfworld': AlfWorldActionAgent, 'scienceworld': ScienceWorldActionAgent, 'appworld': AppWorldActionAgent}


class IncompleteModelResponse(ValueError):
    """No usable action was returned; retry the unchanged request within its budget."""
    def __init__(self, message, finish_reason=None):
        super().__init__(message)
        self.finish_reason = finish_reason


class ChatClient:
    """Chat Completions client with explicitly configured transient-request retries."""
    def __init__(self, config, environment):
        self.config = config
        self.key = environment.get(config.api_key_env)
        self.url = config.base_url or environment.get(config.base_url_env)
        self.request_attempts = 0
        self.retry_count = 0
        self.stream_required = str(environment.get('EXECUTION_STREAM', 'false')).lower() in {'true', '1', 'yes'}
        self.diagnostics_path = None
        self.secrets = secret_values(environment)
        if type(config.max_retries) is not int or config.max_retries < 0:
            raise ValueError('max_retries must be a nonnegative integer')
        if not self.key or not self.url:
            raise ValueError('Native execution requires configured API key and base URL')

    @staticmethod
    def _stream_response(response):
        parts, finish, done, reasoning_chars, usage = [], None, False, 0, None
        for raw in response:
            line = raw.decode('utf-8').strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                done = True
                break
            event = json.loads(payload)
            if event.get('usage') is not None:
                usage = event['usage']
            if event.get('error'):
                raise ConnectionError('Model stream returned an error before completion')
            choices = event.get('choices', [])
            if not choices:
                continue
            choice = choices[0]
            reasoning = choice.get('delta', {}).get('reasoning_content')
            if isinstance(reasoning, str):
                reasoning_chars += len(reasoning)
            text = choice.get('delta', {}).get('content')
            if text is not None:
                if not isinstance(text, str):
                    raise ValueError('Model stream content is not text')
                parts.append(text)
            if choice.get('finish_reason') is not None:
                finish = choice['finish_reason']
        if not done or finish is None:
            raise ConnectionError('Model stream disconnected before completion')
        return {'choices': [{'message': {'content': ''.join(parts)}, 'finish_reason': finish}],
                'usage': usage, 'reasoning_chars': reasoning_chars}

    def _checked_action(self, result):
        try:
            choice = result['choices'][0]
            content = choice['message']['content']
        except (KeyError, IndexError, TypeError):
            raise ValueError('Model response does not contain a text action') from None
        finish = choice.get('finish_reason')
        summary = {'request_attempt': self.request_attempts, 'model': self.config.model,
                   'finish_reason': finish, 'content_chars': len(content) if isinstance(content, str) else None,
                   'reasoning_chars': result.get('reasoning_chars'), 'usage': result.get('usage')}
        if self.diagnostics_path is not None:
            with self.diagnostics_path.open('a', encoding='utf-8') as log:
                log.write(json.dumps(redact(summary, self.secrets), ensure_ascii=False) + '\n')
        if finish == 'length' or (finish == 'stop' and (content is None or isinstance(content, str) and not content.strip())):
            raise IncompleteModelResponse('Model returned an incomplete or empty action: '
                f'finish_reason={finish!r}, content_chars={summary["content_chars"]}, '
                f'reasoning_chars={summary["reasoning_chars"]}', finish_reason=finish)
        if finish != 'stop' or not isinstance(content, str) or not content.strip():
            raise ValueError(f'Model returned an invalid action: finish_reason={finish!r}')
        return content

    def _request(self, body):
        while True:
            payload = {**body, **({'stream': True} if self.stream_required else {})}
            request = Request(self.url.rstrip('/') + '/chat/completions',
                              data=json.dumps(payload).encode(),
                              headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                                       'User-Agent': 'skillpatch-rebuild/1.0'})
            self.request_attempts += 1
            try:
                with urlopen(request, timeout=self.config.timeout_sec) as response:
                    return self._stream_response(response) if self.stream_required else json.load(response)
            except HTTPError as error:
                if error.code == 400 and not self.stream_required:
                    message = error.read(8192).decode('utf-8', errors='replace')
                    if '非流式请求已禁用' in message and 'stream=true' in message:
                        error.close()
                        self.stream_required = True
                        print('Model gateway requires streaming; using stream=true for this client.', flush=True)
                        continue
                raise

    def chat(self, messages):
        body = {'model': self.config.model, 'messages': messages,
                'temperature': self.config.temperature, 'max_tokens': self.config.max_tokens}
        for attempt in range(self.config.max_retries + 1):
            try:
                result = self._request(body)
                return self._checked_action(result)
            except HTTPError as error:
                status = error.code
                error.close()
                if status not in {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530} or attempt == self.config.max_retries:
                    raise ValueError(f'Model HTTP request failed: {status}') from None
            except (URLError, TimeoutError, ConnectionError):
                if attempt == self.config.max_retries:
                    raise
            except IncompleteModelResponse as error:
                if attempt == self.config.max_retries:
                    if error.finish_reason == 'length':
                        raise ModelActionBudgetExceeded(str(error)) from None
                    raise
            self.retry_count += 1
            print(f'Model request transient failure; retry {attempt + 1}/{self.config.max_retries}', flush=True)
            time.sleep(min(2 ** attempt, 4))


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
    client.diagnostics_path = output / 'model_responses.jsonl'
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
            model_client = getattr(getattr(runner, 'action_agent', None), 'llm', None)
            if isinstance(model_client, ChatClient):
                state['model_requests'] = {'attempts': model_client.request_attempts,
                                           'retries': model_client.retry_count}
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
