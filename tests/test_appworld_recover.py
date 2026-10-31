"""Recovery reuses only matching, unchanged official execution archives."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import write_json
from set_up.appworld_recover import CachedBridge


class CachedBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('frozen executor')
        self.entry = {'id': 'task_1', 'split': 'test', 'config': str(self.config),
                      'task_ref': {'task_id': 'task_1', 'split': 'test_normal'}}
        self.bridge = CachedBridge({}, self.root / 'cache')
        self.calls = []

    def execute(self, entry, text, output, **kwargs):
        self.calls.append((entry, text))
        output.mkdir(parents=True)
        archive = output / 'task.json'
        write_json(archive, {'text': text})
        return archive

    def checked(self, archive, entry, expected):
        self.assertEqual(json.loads(Path(archive).read_text())['text'], expected)

    def test_resume_skips_paid_execution_and_detects_archive_tampering(self):
        with patch('method_input_adapters.execution.SharedExecutorBridge.run', side_effect=self.execute), \
             patch('set_up.appworld_recover.check_execution', side_effect=self.checked):
            first = self.bridge.run(self.entry, 'selected', self.root / 'unused')
            second = self.bridge.run(self.entry, 'selected', self.root / 'unused')
            self.assertEqual(first, second)
            self.assertEqual(len(self.calls), 1)
            first.write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'archive changed'):
                self.bridge.run(self.entry, 'selected', self.root / 'unused')

    def test_different_skills_and_test_splits_never_share_results(self):
        with patch('method_input_adapters.execution.SharedExecutorBridge.run', side_effect=self.execute), \
             patch('set_up.appworld_recover.check_execution', side_effect=self.checked):
            for text in ['initial', 'selected']:
                self.bridge.run(self.entry, text, self.root / 'unused')
            entry = {**self.entry, 'task_ref': {'task_id': 'task_1', 'split': 'dev'}}
            self.bridge.run(entry, 'initial', self.root / 'unused')
        self.assertEqual(len(self.calls), 3)

    def test_failed_attempt_not_cached_and_original_evidence_preserved(self):
        attempts = []
        def flaky(entry, text, output, **kwargs):
            attempts.append(output)
            if len(attempts) == 1:
                output.mkdir(parents=True)
                (output / 'error.txt').write_text('connection failed')
                raise ValueError('connection failed')
            return self.execute(entry, text, output, **kwargs)
        with patch('method_input_adapters.execution.SharedExecutorBridge.run', side_effect=flaky), \
             patch('set_up.appworld_recover.check_execution', side_effect=self.checked):
            with self.assertRaisesRegex(ValueError, 'connection failed'):
                self.bridge.run(self.entry, 'initial', self.root / 'unused')
            self.bridge.run(self.entry, 'initial', self.root / 'unused')
        self.assertNotEqual(attempts[0], attempts[1])
        self.assertTrue((attempts[0] / 'error.txt').exists())

    def test_incomplete_legacy_execution_is_never_reused(self):
        previous = self.root / 'old'
        write_json(previous / 'result.json', {'status': 'backend_error', 'errors': ['connection failed']})
        with patch('method_input_adapters.execution.SharedExecutorBridge.run', side_effect=self.execute), \
             patch('set_up.appworld_recover.check_execution', side_effect=self.checked):
            self.bridge.run(self.entry, 'initial', self.root / 'unused', previous=previous)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
