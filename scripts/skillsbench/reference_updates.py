"""Adopt approved scored references only before their task enters a method."""

from pathlib import Path


def adopt(state, requests, output):
    from . import experiment as exp
    marker = output / 'APPROVED_REFERENCE_UPDATES.json'
    if not marker.exists():
        return {}
    policy = exp.read(marker)
    budget = exp.read(output / 'VERIFIER_BUDGET_REQUESTED.json')
    if policy.get('status') != 'approved':
        raise ValueError('Reference update has not been approved')
    adopted = {}
    for name, rule in policy['tasks'].items():
        if budget['tasks'].get(name, {}).get('seconds') != rule['verifier_timeout_seconds']:
            raise ValueError('Recovered reference and subsequent scoring budgets differ')
        if name not in requests:
            raise ValueError('Approved reference update names an unknown task')
        source = Path(rule['source']).resolve(strict=True)
        previous = Path(rule['previous_source']).resolve(strict=True)
        if not source.is_relative_to(output) or not previous.is_relative_to(output):
            raise ValueError('Approved reference must remain inside this experiment')
        if exp.tree_hashes(source) != rule['hashes']:
            raise ValueError('Approved recovered reference archive changed')
        if exp.sha256_file(previous / 'result.json') != rule['previous_result_sha256']:
            raise ValueError('Previous reference evidence changed')
        archive = exp.load_archive(source)
        if archive.benchmark != 'skillsbench' or archive.task_id != name or archive.score != rule['score']:
            raise ValueError('Approved reference identity or score differs')
        old = state['baselines'][name]
        if old['status'] == 'completed' and old['source'] == str(source):
            continue
        if old['source'] != str(previous):
            raise ValueError('Current reference differs from the approved predecessor')
        matching = [key for key in state['methods'] if key.rsplit('/', 1)[-1] == name]
        if any(state['methods'][key]['status'] != 'blocked' for key in matching):
            raise ValueError('Cannot replace a reference already consumed by a method')
        record = {**old, 'status': 'completed', 'source': str(source), 'hashes': rule['hashes'],
            'execution_status': 'completed', 'previous_reference_record': old,
            'approved_verifier_timeout_seconds': rule['verifier_timeout_seconds'],
            'reference_recovered_without_agent_replay': True}
        record.pop('error', None)
        state['baselines'][name] = record
        for key in matching:
            del state['methods'][key]
        adopted[name] = {'source': str(source), 'score': archive.score,
                         'verifier_timeout_seconds': rule['verifier_timeout_seconds']}
    if adopted:
        exp.write_json(output / 'approved_reference_updates_applied.json', {
            'at': exp.now(), 'records': adopted, 'original_evidence_preserved': True})
    return adopted
