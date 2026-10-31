import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import write_json
from set_up.appworld_patch_resume import resume


class PatchResumeTests(unittest.TestCase):
    def test_completed_stages_reused_and_failed_replay_retries_without_regeneration(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'old', root / 'resume'
            (source / 'inputs').mkdir(parents=True)
            manifest_path = root / 'manifest.json'
            write_json(manifest_path, {})
            old = {'status': 'failed', 'trajectory_manifest': str(manifest_path), 'tasks': [
                {'task_id': 'done_1', 'status': 'completed', 'issues': []},
                {'task_id': 'pending_1', 'source': 'source.json', 'issues': [],
                 'diagnosis': {'status': 'completed', 'path': str(root / 'diagnosis')}}]}
            write_json(source / 'result.json', old)
            rows = [{'task_id': 'done_1', 'score': 0, 'source': 'done.json'}, {'task_id': 'pending_1', 'score': 0, 'source': 'source.json'},
                    {'task_id': 'success_1', 'score': 1}]
            with patch('set_up.appworld_patch_resume.load_manifest', return_value={'rows': rows}), \
                 patch('set_up.appworld_patch_resume.load_discovery_config'), \
                 patch('set_up.appworld_patch_resume.issue_ids', return_value=['I000001']), \
                 patch('set_up.appworld_patch_resume.run_discovery') as diagnosis, \
                 patch('set_up.appworld_patch_resume.run_stage2', return_value={
                     'status': 'generated', 'patch_path': str(root / 'patch.json')}) as generation, \
                 patch('set_up.appworld_patch_resume.run_validation', side_effect=[
                     {'status': 'failed', 'error': 'connection failed'},
                     {'status': 'completed', 'comparison': {'outcome': 'repaired'}}]) as replay:
                first = resume(source, output, {})
                self.assertEqual(first['status'], 'failed')
                second = resume(source, output, {})
                completed_bytes = (output / 'result.json').read_bytes()
                resume(source, output, {})
                self.assertEqual((output / 'result.json').read_bytes(), completed_bytes)
            self.assertEqual(second['status'], 'completed')
            self.assertEqual(second['total_tasks'], 2)
            self.assertEqual(second['repaired_count'], 1)
            diagnosis.assert_not_called()
            generation.assert_called_once()
            self.assertEqual(replay.call_count, 2)
            self.assertEqual(json.loads((source / 'result.json').read_text()), old)


if __name__ == '__main__':
    unittest.main()
