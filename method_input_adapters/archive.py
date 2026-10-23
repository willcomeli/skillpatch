"""Read original execution archives; no additional execution-record file format."""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    json.dumps(value, allow_nan=False)
    return value


def write_json(path, value):
    from excutor.executor_adapter.storage import jsonable
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def output_id(benchmark, task_id):
    return hashlib.sha256(f'{benchmark}\0{task_id}'.encode()).hexdigest()[:24]


@dataclass
class Archive:
    source: Path
    benchmark: str
    task_id: str
    instruction: str
    success: bool
    score: float
    conversation: list
    n_turns: int
    task_type: str
    limitations: list


def _score(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('An official finite score in [0, 1] is required; missing scores are not zero')
    return float(value)


def load_archive(source):
    source = Path(source).expanduser().resolve(strict=True)
    path = source / 'result.json' if source.is_dir() else source
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError('Expected one task result, not a batch')
    if raw.get('benchmark') == 'skillsbench':
        # Reuse the existing archive-integrity checks, without the Stage1
        # eligibility filter or its flattened text as an intermediate format.
        from method_input_adapters.skillpatch.adapter.skillsbench import SkillsBenchDiagnosisAdapter
        checked = SkillsBenchDiagnosisAdapter().build_diagnosis_context(source)
        if raw.get('status') != 'completed' or raw.get('errors'):
            raise ValueError('Execution did not complete successfully')
        feedback = raw.get('feedback') or {}
        if feedback.get('status') != 'scored' or feedback.get('issues'):
            raise ValueError('Official evaluation is unavailable or invalid')
        score = _score(feedback.get('metrics', {}).get('reward'))
        events_path = Path(raw['execution']['normalized_trajectory_path'])
        if not events_path.is_absolute():
            events_path = source / events_path
        events = [json.loads(line)['event'] for line in events_path.read_text().splitlines() if line.strip()]
        conversation = []
        for event in events:
            role = 'user' if event.get('type') == 'user_message' else 'assistant'
            # Preserve every saved field, including tool identities and outputs.
            conversation.append({'role': role, 'content': json.dumps(event, ensure_ascii=False), 'event': event})
        conversation.append({'role': 'system', 'content': 'Official verifier feedback:\n' + json.dumps(feedback, ensure_ascii=False)})
        instruction = '\n\n'.join(e.get('text', '') for e in events if e.get('type') == 'user_message')
        if not instruction:
            instruction = Path(raw['prepared']['task_document']).read_text(encoding='utf-8')
        return Archive(source, 'skillsbench', raw['task_id'], instruction, score == 1, score,
                       conversation, sum(e.get('type') == 'tool_call' for e in events),
                       raw['task_id'], checked.limitations)
    benchmark = raw.get('dataset')
    if benchmark not in {'alfworld', 'scienceworld', 'appworld'}:
        raise ValueError(f'Unsupported execution dataset: {benchmark}')
    from method_input_adapters.skillpatch.eligibility import native_eligibility
    eligibility, reason = native_eligibility(raw, benchmark)
    if eligibility == 'invalid':
        raise ValueError('Invalid official execution feedback: ' + reason)
    score = _score(raw.get('score'))
    steps = raw.get('trajectory')
    if not isinstance(steps, list) or any(not isinstance(row, dict) for row in steps):
        raise ValueError('Expected a saved trajectory step list')
    if raw.get('steps', len(steps)) != len(steps):
        raise ValueError('Saved step count differs from trajectory length')
    task_id, instruction = raw.get('task_id'), raw.get('instruction')
    if not isinstance(task_id, str) or not task_id or not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('Task ID and instruction are required')
    conversation = [{'role': 'user', 'content': instruction}]
    for index, row in enumerate(steps):
        observed = str(row.get('observation', ''))
        metadata = {key: value for key, value in row.items()
                    if key not in {'observation', 'action', 'thought', 'next_observation'}}
        conversation.append({'role': 'user', 'content': observed + '\nSaved step metadata:\n' + json.dumps(metadata, ensure_ascii=False)})
        feedback = row.get('next_observation')
        if feedback is None and index + 1 < len(steps):
            feedback = steps[index + 1].get('observation')
        item = {'step': row.get('step', index), 'action': str(row.get('action', '')),
                'reasoning': str(row.get('thought', '')), 'env_feedback': '' if feedback is None else str(feedback)}
        conversation.append(item)
    visible = {k: raw[k] for k in ('success', 'score', 'evaluation_summary', 'task_completed') if k in raw}
    conversation.append({'role': 'system', 'content': 'Official verifier feedback:\n' + json.dumps(visible, ensure_ascii=False)})
    limitations = ['Saved native steps are not a full model protocol transcript.']
    if raw.get('trajectory_complete') is not True:
        limitations.append('Trajectory completeness is not confirmed by this archive.')
    return Archive(path, benchmark, task_id, instruction, raw['success'], score,
                   conversation, len(steps), str(raw.get('task_family') or benchmark), limitations)
