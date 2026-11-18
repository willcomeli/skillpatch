import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.skillsbench import recover_references as recovery
from scripts.skillsbench.recover_references_global import take_available, main as global_main
from scripts.skillsbench.step_retries import retryable_build_store_failure, retry_operation
import asyncio
from unittest.mock import AsyncMock


class ReferenceRecoveryTests(unittest.TestCase):
    def test_duplicate_global_coordinator_is_rejected_before_signals_or_execution(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'global_reference_recovery_progress.json').write_text('{}')
            with patch('scripts.skillsbench.recover_references_global.os.kill') as kill:
                with self.assertRaisesRegex(ValueError, 'duplicate executions'):
                    global_main(['--run', d])
                kill.assert_not_called()

    def test_global_pool_fills_slots_released_by_the_previous_batch(self):
        pending = list('abcde')
        selected, pending = take_available(pending, 7, 0, 10)
        self.assertEqual(selected, list('abc'))
        self.assertEqual(pending, list('de'))
        # Two old tasks finish while all three newly submitted tasks still run.
        selected, pending = take_available(pending, 5, 3, 10)
        self.assertEqual(selected, list('de'))
        self.assertEqual(pending, [])
        selected, pending = take_available(['next'], 5, 5, 10)
        self.assertEqual(selected, [])
        self.assertEqual(pending, ['next'])
        with self.assertRaises(ValueError):
            take_available(['next'], 7, 4, 10)

    def test_ingest_commit_failure_retries_only_native_preparation_without_task_replay(self):
        message = ('failed commit on ref "layer-sha256:abc": commit failed: rename '
                   '/var/lib/desktop-containerd/daemon/io.containerd.content.v1.content/ingest/abc/data '
                   '/var/lib/desktop-containerd/daemon/io.containerd.content.v1.content/blobs/sha256/abc: '
                   'no such file or directory')
        self.assertTrue(retryable_build_store_failure(message))
        for permanent in ('permission denied', 'no space left on device', '401 Unauthorized', 'certificate verify failed'):
            self.assertFalse(retryable_build_store_failure(message + permanent))
        async def run():
            operation = AsyncMock(side_effect=[RuntimeError(message)] * 7 + ['native build completed'])
            with patch('scripts.skillsbench.step_retries.asyncio.sleep', new_callable=AsyncMock):
                self.assertEqual(await retry_operation(operation, 'docker_build'), 'native build completed')
            self.assertEqual(operation.await_count, 8)
            operation = AsyncMock(side_effect=RuntimeError(message))
            with self.assertRaises(RuntimeError):
                await retry_operation(operation, 'model_request')
            self.assertEqual(operation.await_count, 1)
        asyncio.run(run())

    def test_pending_archives_are_not_adopted_and_failed_archives_remain_unscored(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'one'
            folder.mkdir()
            path = folder / 'result.json'
            path.write_text(json.dumps({'status': 'running', 'finished_at': None}))
            self.assertIsNone(recovery.finished_record(folder))
            path.write_text(json.dumps({'status': 'timeout', 'finished_at': 'now'}))
            with patch.object(recovery.exp, 'load_archive', side_effect=ValueError('No formal score')):
                record = recovery.finished_record(folder)
            self.assertEqual(record['status'], 'failed')
            self.assertNotIn('score', record)
            self.assertEqual(record['source'], str(folder))

    def test_rerun_updates_both_reference_and_original_checkpoint_without_reexecution(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'one'
            folder.mkdir()
            (folder / 'result.json').write_text(json.dumps({'status': 'completed', 'finished_at': 'now'}))
            with patch.object(recovery.exp, 'load_archive', return_value=SimpleNamespace(task_id='one')):
                record = recovery.finished_record(folder)
            state = {'baselines': {'one': {'status': 'failed'}}, 'methods': {}}
            checkpoint = {'finished': {'one': {'status': 'failed'}}}
            state, checkpoint = recovery.integrate(state, checkpoint, {'one': record})
            self.assertEqual(state['baselines']['one'], checkpoint['finished']['one'])
            self.assertEqual(state['baselines']['one']['previous_reference_record']['status'], 'failed')
            (folder / 'result.json').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'reference changed'):
                recovery.integrate(state, checkpoint, {'one': record})

    def test_references_already_used_by_methods_cannot_be_replaced(self):
        with self.assertRaisesRegex(ValueError, 'method work'):
            recovery.integrate({'baselines': {}, 'methods': {'skillopt/one': {}}}, {'finished': {}}, {})

    def test_sigint_only_drains_submitted_workers_without_starting_queued_jobs(self):
        # Real processes verify the running executor work survives a signal
        # sent only to its coordinator. These are file-writing fixtures.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = root / 'drain.py'
            script.write_text('''from pathlib import Path
import sys, time
from scripts.skillsbench.experiment import _batch_results
def worker(folder, number):
    folder = Path(folder)
    (folder / (str(number) + '.started')).write_text('started')
    time.sleep(1)
    (folder / (str(number) + '.finished')).write_text('finished')
    return {'status': 'completed'}
if __name__ == '__main__':
    try:
        for _ in _batch_results(worker, [(str(n), (sys.argv[1], n)) for n in range(4)], 2, ()):
            pass
    except KeyboardInterrupt:
        (Path(sys.argv[1]) / 'drained').write_text('done')
''')
            environment = {**os.environ, 'PYTHONPATH': str(recovery.exp.ROOT)}
            process = subprocess.Popen([str(recovery.exp.ROOT / '.venvs/methods/bin/python'),
                                        str(script), str(root)], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while len(list(root.glob('*.started'))) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(len(list(root.glob('*.started'))), 2)
                os.kill(process.pid, signal.SIGINT)
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                self.assertTrue((root / 'drained').exists())
                self.assertEqual(len(list(root.glob('*.finished'))), 2)
                self.assertEqual(len(list(root.glob('*.started'))), 2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    unittest.main()
