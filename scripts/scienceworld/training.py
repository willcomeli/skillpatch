"""Use SkillOpt's original trainer/checkpoints with resumable executor I/O."""

from pathlib import Path

from .splits import ROOT, digest, read, save
from .experiment import execute_many, transient, backoff, task_retry_limit
from excutor.executor_adapter.storage import sha256_file, tree_hashes


class OptionalEvaluationCancelled(RuntimeError):
    """An explicit per-run user decision cancels the optional last-Skill test."""


def check_optional_final_evaluation(out_dir, trainer_output, policy_path):
    if (Path(out_dir).resolve() != (Path(trainer_output) / 'test_eval_final').resolve()
            or not Path(policy_path).exists()):
        return
    policy = read(policy_path)
    if policy != {'enabled': False, 'source': 'user_instruction'}:
        raise ValueError('Invalid optional final-Skill evaluation policy')
    save(Path(out_dir) / 'cancellation.json', {
        'status': 'cancelled_by_user', 'policy_path': str(policy_path),
        'existing_task_records_preserved': True})
    # Upstream catches final-test exceptions and leaves its aggregate scores None.
    # Do not return partial rows or fabricate an evaluation score.
    raise OptionalEvaluationCancelled('Optional last-Skill test cancelled by user; '
                                      'use the completed best-Skill test for SkillPatch')


class Proxy:
    def __init__(self, target):
        self.target = target

    def __getattr__(self, name):
        return getattr(self.target, name)


class RetryingCompletions(Proxy):
    def create(self, **kwargs):
        # Upstream injects per-stage token budgets. This experiment uses the
        # provider default, so remove both spellings at the final SDK boundary.
        kwargs.pop('max_tokens', None)
        kwargs.pop('max_completion_tokens', None)
        while True:
            try:
                return self.target.create(**kwargs)
            except Exception as error:
                if not transient(error):
                    raise
                backoff()


class RetryingClient(Proxy):
    def __init__(self, target):
        super().__init__(target)
        self.chat = Proxy(target.chat)
        self.chat.completions = RetryingCompletions(target.chat.completions)


def apply_score_overrides(entries, sources, cache, out_dir, rows):
    """Keep user-assigned scores distinct from official execution archives."""
    import json
    by_id = {row['id']: row for row in rows}
    overridden = []
    for entry, source in zip(entries, sources):
        receipt = cache / entry['id'] / 'skipped.json'
        override = read(receipt).get('scoring_override') if source is None and receipt.exists() else None
        if override is None:
            continue
        if override != {'score': 0, 'success': False, 'source': 'user_instruction'}:
            raise ValueError(f'Unsupported explicit score override: {receipt}')
        ref = entry['task_ref']
        by_id[entry['id']] = {
            'id': entry['id'], 'hard': 0, 'soft': 0.0, 'n_turns': 0,
            'task_type': ref.get('task_family', 'scienceworld'),
            'task_description': ref['task_id'], 'benchmark': 'scienceworld',
            'original_task_id': ref['task_id'], 'source_archive': None,
            'score_origin': 'user_instruction', 'infrastructure_skipped': True,
            'skip_receipt': str(receipt)}
        save(out_dir / 'predictions' / entry['id'] / 'conversation.json', [
            {'role': 'system', 'content': 'Infrastructure failure; user assigned score 0. '
                                        'No completed trajectory or official score is available.'}])
        overridden.append(entry['id'])
    rows = [by_id[entry['id']] for entry in entries if entry['id'] in by_id]
    if overridden:
        (out_dir / 'results.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return rows, overridden


def train(inputs, output, environment):
    from skillopt.config import load_config, flatten_config
    from skillopt.engine.trainer import ReflACTTrainer
    from skillopt.model import configure_openai_compatible
    from skillopt.model import openai_compatible_backend as backend
    from method_input_adapters.skillopt.adapter import SharedExecutionAdapter
    from method_input_adapters.skillopt.results import export_results
    from method_input_adapters.execution import load_tasks
    from method_input_adapters.chat_compat import skillopt_chat_parameters

    tasks = load_tasks(inputs / 'manifest.json')
    output.mkdir(parents=True, exist_ok=True)
    receipt = output / 'completed.json'
    trainer_output = output / 'trainer'
    if receipt.exists():
        result = read(receipt)
        if tree_hashes(trainer_output) != result['trainer_hashes']:
            raise ValueError('Completed SkillOpt training artifacts changed')
        return result, output

    class Adapter(SharedExecutionAdapter):
        def rollout(self, env_manager, skill_content, out_dir, **kwargs):
            check_optional_final_evaluation(out_dir, trainer_output,
                                            output / 'final_skill_test_policy.json')
            entries = list(env_manager)
            out_dir = Path(out_dir)
            binding = {'skill': digest(skill_content), 'entries': digest(entries)}
            cached = out_dir / 'orchestration_receipt.json'
            if cached.exists():
                record = read(cached)
                if record['binding'] == binding:
                    for relative, expected in record['files'].items():
                        if sha256_file(out_dir / relative) != expected:
                            raise ValueError(f'Rollout checkpoint changed: {out_dir / relative}')
                    if not (task_retry_limit(environment) is None and record.get('skipped_ids')):
                        return record['rows']
            # A new candidate can occupy the same trainer phase after an interrupted
            # update; key executions by exact Skill AND ordered task identities.
            cache = out_dir.with_name(out_dir.name + '-executions') / digest(binding)
            sources = execute_many([(entry, cache / entry['id']) for entry in entries],
                                   skill_content, environment)
            scored = [(entry, source) for entry, source in zip(entries, sources) if source is not None]
            skipped_ids = [entry['id'] for entry, source in zip(entries, sources) if source is None]
            if out_dir.exists():
                index = 1
                while out_dir.with_name(out_dir.name + f'.interrupted-{index:06d}').exists():
                    index += 1
                out_dir.rename(out_dir.with_name(out_dir.name + f'.interrupted-{index:06d}'))
            if scored:
                rows = export_results([source for _, source in scored], out_dir,
                                      ids=[entry['id'] for entry, _ in scored])
            else:
                out_dir.mkdir(parents=True)
                (out_dir / 'results.jsonl').write_text('')
                save(out_dir / 'adapter_manifest.json', [])
                rows = []
            rows, overridden = apply_score_overrides(entries, sources, cache, out_dir, rows)
            skipped_ids = [identifier for identifier in skipped_ids if identifier not in overridden]
            save(out_dir / 'skipped_tasks.json', {'skipped_ids': skipped_ids,
                                                'user_assigned_zero_ids': overridden,
                                                'excluded_from_scoring': True})
            save(cached, {'binding': binding, 'rows': rows, 'skipped_ids': skipped_ids,
                          'files': tree_hashes(out_dir)})
            return rows

    cfg = flatten_config(load_config(str(ROOT / 'method/skillopt/configs/_base_/default.yaml')))
    # These are wiring fields only. Epochs, optimizer, gates, slow/meta updates,
    # batch sizes, sampling, and final evaluation remain upstream defaults.
    cfg.update(out_root=str(trainer_output), skill_init=str(inputs / 'blank_skill.md'),
               train_size=sum(row['split'] == 'train' for row in tasks),
               model_backend='openai_compatible', optimizer_backend='openai_compatible',
               target_backend='openai_compatible',
               optimizer_model=environment.get('METHOD_MODEL', 'gpt-6.1-sol'),
               target_model='shared-executor', reasoning_effort=None)
    cfg.setdefault('optimizer_openai_compatible_timeout_seconds', 1800)
    configure_openai_compatible(base_url=environment['METHOD_BASE_URL'],
                               api_key=environment['METHOD_API_KEY'], model=cfg['optimizer_model'],
                               timeout_seconds=cfg['optimizer_openai_compatible_timeout_seconds'],
                               temperature=0)
    original = backend._build_client
    backend._build_client = lambda config: RetryingClient(original(config))
    try:
        while True:
            try:
                with skillopt_chat_parameters(cfg['optimizer_model']):
                    summary = ReflACTTrainer(dict(cfg), Adapter(tasks, None)).train()
                break
            except Exception as error:
                if not transient(error):
                    raise
                backoff()
    finally:
        backend._build_client = original
    result = {'status': 'completed', 'summary': summary, 'trainer_hashes': tree_hashes(trainer_output)}
    if (output / 'final_skill_test_policy.json').exists():
        result['cancelled_optional_evaluations'] = ['final_skill_test']
    save(receipt, result)
    return result, output
