"""Export existing archives to upstream AgentTrace without rerunning tasks."""

from pathlib import Path
from uuid import uuid4

from ..archive import write_json
from .adapter import archive_trace


def export_results(sources, output):
    sources = list(sources)
    if not sources:
        raise ValueError('Supply at least one execution archive')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows, seen = [], set()
    for source in sources:
        trace, archive = archive_trace(source)
        if archive.source in seen:
            raise ValueError('Duplicate source archive')
        seen.add(archive.source)
        path = output / 'traces' / (uuid4().hex + '.json')
        write_json(path, trace.model_dump(mode='json'))
        rows.append(dict(source=str(archive.source), task_id=archive.task_id,
                         benchmark=archive.benchmark, success=archive.success,
                         score=archive.score, trace_path=str(path)))
    write_json(output / 'adapter_manifest.json', rows)
    return rows
