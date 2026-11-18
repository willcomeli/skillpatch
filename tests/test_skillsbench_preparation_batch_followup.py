import json
from pathlib import Path
import tempfile
import unittest

from scripts.skillsbench import experiment as exp
from scripts.skillsbench.preparation_batch_followup import validate_source


class PreparationBatchTests(unittest.TestCase):
    def test_only_final_pre_agent_unscored_archives_can_be_restarted(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary).resolve()
            source = run / 'previous/one'
            source.mkdir(parents=True)
            path = source / 'result.json'
            for status, events, feedback, finished, valid in (
                    ('agent_error', 0, 'unavailable', True, True),
                    ('running', 0, 'unavailable', False, False),
                    ('agent_error', 1, 'unavailable', True, False),
                    ('agent_error', 0, 'available', True, False)):
                raw = {'status': status, 'execution': {'trajectory_events': events},
                       'feedback': {'status': feedback}}
                if finished:
                    raw['finished_at'] = 'now'
                path.write_text(json.dumps(raw))
                target = {'failure_source': str(source), 'failure_result_sha256': exp.sha256_file(path)}
                if valid:
                    validate_source(run, 'one', target)
                else:
                    with self.assertRaisesRegex(ValueError, 'replay Agent'):
                        validate_source(run, 'one', target)
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                validate_source(run, 'two', target)
            target['failure_result_sha256'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                validate_source(run, 'one', target)
