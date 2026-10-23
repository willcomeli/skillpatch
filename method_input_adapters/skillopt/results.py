"""Materialize SkillOpt's real results.jsonl / predictions contract."""

from pathlib import Path
import json
from ..archive import load_archive, output_id, write_json


def export_results(sources, output, *, ids=None):
    sources = list(sources)
    if not sources or ids is not None and len(ids) != len(sources):
        raise ValueError('Supply nonempty sources and one output ID per source')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows, manifest, seen = [], [], set()
    for index, source in enumerate(sources):
        archive = load_archive(source)
        tid = ids[index] if ids is not None else output_id(archive.benchmark, archive.task_id + '\0' + str(archive.source))
        if not isinstance(tid, str) or not tid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in tid) or tid in {'.', '..'}:
            raise ValueError('Unsafe task output ID')
        if tid in seen:
            raise ValueError('Duplicate output task ID; supply distinct IDs for repeated executions')
        seen.add(tid)
        row = {'id': tid, 'hard': int(archive.success), 'soft': archive.score,
               'n_turns': archive.n_turns, 'task_type': archive.task_type,
               'task_description': archive.instruction, 'benchmark': archive.benchmark,
               'original_task_id': archive.task_id, 'source_archive': str(archive.source)}
        write_json(output / 'predictions' / tid / 'conversation.json', archive.conversation)
        manifest.append({'id': tid, 'source': str(archive.source), 'limitations': archive.limitations})
        rows.append(row)
    (output / 'results.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    write_json(output / 'adapter_manifest.json', manifest)
    return rows
