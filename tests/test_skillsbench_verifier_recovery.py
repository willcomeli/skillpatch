import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.skillsbench.verifier_recovery import validate_report_source


class VerifierRecoveryTests(unittest.TestCase):
    def test_saved_report_must_match_finished_agent_and_backup_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            trial = source / 'jobs/job/energy-ac-optimal-power-flow__one'
            (trial / 'trajectory').mkdir(parents=True)
            summary = {'task_name': 'energy-ac-optimal-power-flow', 'error': None}
            (trial / 'result.json').write_text(json.dumps(summary))
            trace = trial / 'trajectory/acp_trajectory.jsonl'
            trace.write_text('{"type":"agent_message","text":"done"}\n')
            report = root / 'backup/report.json'
            report.parent.mkdir()
            report.write_text('{}')
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            proof = report.parent / 'provenance.jsonl'
            proof.write_text(json.dumps({'source_archive': str(source), 'sha256': digest, 'phase': 'after_agent'}) + '\n')
            validate_report_source(source, report, digest)
            with self.assertRaisesRegex(ValueError, 'checksum changed'):
                validate_report_source(source, report, 'wrong')
            proof.write_text(json.dumps({'source_archive': str(source), 'sha256': digest, 'phase': 'agent_running_intermediate'}) + '\n')
            with self.assertRaisesRegex(ValueError, 'provenance binds'):
                validate_report_source(source, report, digest)
            trace.write_text('{"type":"tool_call"}\n')
            with self.assertRaisesRegex(ValueError, 'finish normally'):
                validate_report_source(source, report, digest)
