#!/usr/bin/env bash
# Run the 87 reference tasks through the existing Stage1 and Stage2 CLIs.
set -euo pipefail
SP_PROJECT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$SP_PROJECT/src${PYTHONPATH:+:$PYTHONPATH}"
SP_PYTHON=${SP_PYTHON:-python3}
SP_CONCURRENCY=${CONCURRENCY:-4}
SP_ENV_FILE=${ENV_FILE:-/home/lihanlun123/workspace/experiments/skillsbench-with-skill/.env.deepseek-v4-flash}
SP_STAGE2_CONFIG="$SP_PROJECT/configs/stage2.toml"

[[ "$SP_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || { echo 'CONCURRENCY must be positive'; exit 2; }
"$SP_PYTHON" - "$SP_PROJECT" <<'PY'
import sys
from pathlib import Path
from skillpatch.config import load_config
from skillpatch.model_client import load_diagnosis_config
from skillpatch.stage2_config import load_stage2_config
root = Path(sys.argv[1])
configs = root / 'configs/stage1-stage2-full'
tasks = (configs / 'tasks.txt').read_text().splitlines()
assert len(tasks) == len(set(tasks)) == 87, 'Expected 87 unique tasks'
for task in tasks:
    request, _ = load_config(configs / f'{task}.toml')
    assert request.task_dir.name == task and request.task_dir.is_dir()
    assert request.diagnosis_config is not None, f'{task}: Stage1 disabled'
    load_diagnosis_config(request.diagnosis_config)
load_stage2_config(root / 'configs/stage2.toml')
print('Checked 87 tasks: reference execution + live Stage1 + full Stage2')
PY
if [[ "${1:-}" == --check ]]; then exit 0; fi
[[ $# == 0 ]] || { echo 'Usage: run_stage1_stage2_full.sh [--check]'; exit 2; }
[[ -f "$SP_ENV_FILE" ]] || { echo 'Environment file not found'; exit 2; }
# Use the credential in the chosen file, rather than an old shell export.
unset DEEPSEEK_API_KEY
SP_BATCH=$(mktemp -d "$SP_PROJECT/runs/stage1-stage2-full-XXXXXX")
mkdir -p "$SP_BATCH/logs" "$SP_BATCH/status"
cp -a "$SP_PROJECT/configs/stage1-stage2-full" "$SP_BATCH/configs"
export SP_PROJECT SP_PYTHON SP_ENV_FILE SP_STAGE2_CONFIG SP_BATCH
printf 'Batch directory: %s\nConcurrency: %s\n' "$SP_BATCH" "$SP_CONCURRENCY"

sp_run_task() {
    # Record a failed task and continue scheduling the remaining tasks.
    set +e
    local task="$1" run="$SP_BATCH/$1" prefix="$SP_BATCH/logs/$1" rc
    "$SP_PYTHON" -u -m skillpatch run \
        --config "$SP_BATCH/configs/$task.toml" --env-file "$SP_ENV_FILE" \
        --output "$run" >"$prefix.stage1.log" 2>&1
    rc=$?
    printf '%s\n' "$rc" >"$prefix.stage1.exitcode"
    "$SP_PYTHON" - "$run" "$rc" "$SP_STAGE2_CONFIG" "$SP_ENV_FILE" "$prefix" \
        "$SP_BATCH/status/$task.json" <<'PY'
import json
import subprocess
import sys
from pathlib import Path
from skillpatch.evolution_eligibility import reward_eligibility
run, rc, config, env_file, prefix, status_path = sys.argv[1:]
run = Path(run)
state = {'task': run.name, 'run_exitcode': int(rc), 'stage2': []}
code = 0
try:
    record = json.loads((run / 'result.json').read_text()) if (run / 'result.json').exists() else {}
    feedback = record.get('feedback') or {}
    diagnosis = record.get('diagnosis') or {}
    state.update(task_passed=feedback.get('passed'), diagnosis_status=diagnosis.get('status'))
    if int(rc) != 0:
        state['status'] = 'stage1_or_execution_error'
        code = 1
    elif reward_eligibility(feedback) == 'succeeded':
        state['status'] = 'task_succeeded_diagnosis_skipped'
    elif reward_eligibility(feedback) != 'eligible':
        state['status'] = 'no_scored_failure_for_stage2'
    elif diagnosis.get('status') != 'completed':
        state['status'] = 'diagnosis_unavailable'
        code = 1
    else:
        directory = run / 'discovery/live'
        report = json.loads((directory / 'report.json').read_text())
        state['conflict_count'] = len(report['conflicts'])
        state['status'] = 'completed' if report['conflicts'] else 'no_conflict'
        for index, conflict in enumerate(report['conflicts']):
            # Model-supplied IDs are arguments, never filesystem paths.
            output = run.parent / 'stage2' / run.name / f'conflict-{index:03d}'
            logfile = Path(f'{prefix}.stage2-{index:03d}.log')
            command = [sys.executable, '-u', '-m', 'skillpatch', 'stage2',
                '--diagnosis-dir', str(directory), '--conflict-id=' + conflict['conflict_id'],
                '--config', config, '--env-file', env_file, '--output', str(output)]
            with logfile.open('w') as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            Path(f'{prefix}.stage2-{index:03d}.exitcode').write_text(str(result.returncode) + '\n')
            item = {'conflict_id': conflict['conflict_id'], 'exitcode': result.returncode,
                    'output_dir': str(output)}
            if (output / 'result.json').exists():
                detail = json.loads((output / 'result.json').read_text())
                item.update(status=detail.get('status'), decision=detail.get('decision'),
                            ready_for_stage3=detail.get('ready_for_stage3'))
            state['stage2'].append(item)
            if result.returncode:
                state['status'] = 'stage2_error'
                code = 1
except (OSError, ValueError, KeyError, TypeError) as exc:
    state.update(status='pipeline_error', error=type(exc).__name__)
    code = 1
finally:
    Path(status_path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
print(f"{run.name}: {state['status']}; Stage2 conflicts processed: {len(state['stage2'])}", flush=True)
sys.exit(code)
PY
}
export -f sp_run_task
batch_rc=0
xargs -a "$SP_BATCH/configs/tasks.txt" -P "$SP_CONCURRENCY" -n 1 \
    bash -c 'sp_run_task "$1"' _ || batch_rc=$?
"$SP_PYTHON" - "$SP_BATCH" <<'PY'
import json, sys
from pathlib import Path
from collections import Counter
root = Path(sys.argv[1])
states = [json.loads(path.read_text()) for path in (root / 'status').glob('*.json')]
summary = {'tasks_expected': 87, 'pipelines_ended': len(states),
           'pipeline_statuses': dict(Counter(item['status'] for item in states)),
           'stage2_decisions': dict(Counter(entry.get('decision') or 'no_decision'
                for item in states for entry in item['stage2'])), 'tasks': states}
(root / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print('Batch summary:', root / 'summary.json')
PY
exit "$batch_rc"
