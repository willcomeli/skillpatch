"""Classify normalized verifier outcomes before diagnosis or Skill evolution."""
import math
from collections.abc import Mapping


def reward_eligibility(feedback) -> str:
    """Legacy SkillsBench rule: finite numeric reward=1 succeeded, others eligible."""
    if isinstance(feedback, Mapping):
        status, metrics = feedback.get('status'), feedback.get('metrics')
    else:
        status, metrics = getattr(feedback,'status',None), getattr(feedback,'metrics',None)
    if status != 'scored' or not isinstance(metrics,Mapping):
        return 'invalid'
    reward = metrics.get('reward')
    if type(reward) not in (int,float):
        return 'invalid'
    try:
        if not math.isfinite(reward):
            return 'invalid'
    except OverflowError:
        return 'invalid'
    return 'succeeded' if reward == 1 else 'eligible'


def native_eligibility(record, benchmark):
    """Use native official success; score alone cannot invent a success flag."""
    if (record.get('error') or record.get('errors') or
            record.get('status') not in (None,'completed','success','failed')):
        return 'invalid','execution_or_evaluation_error'
    success = record.get('success')
    if type(success) is not bool:
        return 'invalid','official_success_missing_or_invalid'
    summary = record.get('evaluation_summary')
    if isinstance(summary, Mapping):
        if summary.get('error') or summary.get('errors'):
            return 'invalid','official_evaluation_error'
        if benchmark == 'appworld' and 'num_tests' in summary:
            count = summary['num_tests']
            if type(count) is not int or count <= 0:
                return 'invalid','official_evaluation_has_no_tests'
            passed, failed = summary.get('passed_tests'), summary.get('failed_tests')
            if (type(passed) is not int or type(failed) is not int
                    or passed < 0 or failed < 0 or passed + failed != count
                    or success != (passed == count)):
                return 'invalid','official_evaluation_summary_conflict'
    score = record.get('score')
    if 'score' in record:
        if type(score) not in (int,float):
            return 'invalid','official_score_invalid'
        try:
            if not math.isfinite(score):
                return 'invalid','official_score_invalid'
        except OverflowError:
            return 'invalid','official_score_invalid'
        # The migrated native runners normalize success scores to [0,1].
        if not 0 <= score <= 1:
            return 'invalid','official_score_out_of_range'
        if (success and score != 1) or (benchmark != 'scienceworld' and not success and score == 1):
            return 'invalid','success_and_score_conflict'
    if benchmark == 'scienceworld':
        trajectory = record.get('trajectory') or []
        last = trajectory[-1] if trajectory else {}
        if (record.get('score_available') is False or last.get('score_available') is False
                or 'score_after' in last and last['score_after'] is None):
            return 'invalid','final_score_unavailable'
    if benchmark == 'appworld' and success and record.get('task_completed') is False:
        return 'invalid','success_and_completion_conflict'
    return ('succeeded','official_task_succeeded') if success else ('eligible','official_task_not_succeeded')
