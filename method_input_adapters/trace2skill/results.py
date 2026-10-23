"""Write complete logs with Trace2Skill's outcome-bearing filename contract."""

import json
from pathlib import Path
from ..archive import load_archive, output_id, write_json


def export_results(sources, output):
    sources = list(sources)
    if not sources:
        raise ValueError('Supply at least one execution archive')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows, seen = [], set()
    for source in sources:
        archive = load_archive(source)
        tid = output_id(archive.benchmark, archive.task_id + '\0' + str(archive.source))
        if tid in seen:
            raise ValueError('Duplicate task archive; export repetitions in separate directories')
        seen.add(tid)
        outcome = 'SUCCEED' if archive.success else 'FAILED'
        log = output / 'logs' / f'shared_{tid}_{outcome}.md'
        log.parent.mkdir(exist_ok=True)
        parts = ['# Shared executor task log', '## User task\n' + archive.instruction]
        for index, event in enumerate(archive.conversation):
            parts.append(f'## Event {index + 1}\n' + json.dumps(event, ensure_ascii=False, indent=2))
        parts.append('## Archive limitations\n' + '\n'.join(archive.limitations))
        log.write_text('\n\n'.join(parts) + '\n', encoding='utf-8')
        rows.append({'instance_id': tid, 'task_id': archive.task_id, 'benchmark': archive.benchmark,
                     'success': archive.success, 'score': archive.score,
                     'source_archive': str(archive.source), 'log_path': str(log.resolve()),
                     'limitations': archive.limitations})
    write_json(output / 'adapter_manifest.json', rows)
    return rows
