import json
from pathlib import Path
from scripts.skillsbench import baseline_review as review
from scripts.skillsbench import experiment as exp


def test_only_agent_wall_clock_timeouts_are_retryable():
    raw = {'status': 'timeout', 'execution': {'summary': {'agent_timeout_info': {'reason': 'wall_clock_timeout'}}}}
    assert review.classify(raw) == {'category': 'task_timeout', 'score': 0.0}
    for error in ['Command timed out after 1800 seconds', 'Verifier exceeded wall-clock budget']:
        assert review.classify({'status': 'agent_error', 'errors': [{'code': 'timeout', 'message': error}]})['category'] == 'exception'
    assert review.classify({'status': 'completed', 'feedback': {'metrics': {'reward': 0.6}}})['score'] == 0.6


def test_retry_selection_is_latest_non_exception_not_maximum_score():
    first = {'category': 'scored', 'score': 1.0}
    zero = {'category': 'scored', 'score': 0.0}
    assert review.select_attempt(first, zero) == ('timeout-rerun', zero)
    assert review.select_attempt(zero, {'category': 'exception', 'score': None}) == ('first-pass', zero)


def test_summary_counts_timeout_as_zero_and_exceptions_separately():
    summary = review.score_summary([{'category': 'scored', 'score': 1.0},
        {'category': 'scored', 'score': 0.6}, {'category': 'task_timeout', 'score': 0.0},
        {'category': 'exception', 'score': None}])
    assert summary['total'] == 4 and summary['normal_tasks'] == 3
    assert summary['full_success'] == 1 and summary['partial_score'] == 1
    assert summary['task_failure_zero'] == 1 and summary['exceptions'] == 1


def test_collect_final_missing_archive_is_preserved_as_exception(tmp_path):
    review.atomic_json(tmp_path / 'result.json', {'baselines': {'one': {'status': 'failed'}}})
    rows = review.collect_first({'source_output': str(tmp_path)}, {'tasks': ['one', 'two']}, final=True)
    assert set(rows) == {'one', 'two'}
    assert all(review.describe(row)['category'] == 'exception' for row in rows.values())


def test_merge_preserves_both_attempts_and_copies_independently(tmp_path):
    def archive(name, raw):
        path = tmp_path / name
        path.mkdir()
        review.atomic_json(path / 'result.json', raw)
        return {'status': 'completed' if raw['status'] == 'completed' else 'failed',
                'source': str(path), 'hashes': exp.tree_hashes(path)}
    first = {'one': archive('first-one', {'status': 'timeout', 'execution': {'summary': {
        'agent_timeout_info': {'reason': 'wall_clock_timeout'}}}}),
        'two': archive('first-two', {'status': 'agent_error', 'errors': [{'code': 'install_failure'}]})}
    retry = {'one': archive('retry-one', {'status': 'completed', 'feedback': {'metrics': {'reward': 1.0}}})}
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    summary = review.merge_bundle(bundle, {'tasks': ['one', 'two']}, first, retry)
    assert summary['full_success'] == 1 and summary['exceptions'] == 1
    merged = bundle / 'merged'
    assert (merged / 'tasks/one/first-pass/result.json').exists()
    selected = merged / 'selected/one'
    assert selected.resolve() == (merged / 'tasks/one/timeout-rerun').resolve()
    original = Path(retry['one']['source']) / 'result.json'
    before = original.read_text()
    (selected / 'result.json').write_text('changed copy')
    assert original.read_text() == before
