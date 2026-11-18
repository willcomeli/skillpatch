from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

from scripts.skillsbench import experiment as exp
from scripts.skillsbench.resume_queue import drain_queue


class ResumeQueueTests(unittest.TestCase):
    def test_backfills_without_restarting_legacy_and_total_never_exceeds_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = {name: root / (name + '.json') for name in ('old-one', 'old-two')}
            lock = threading.Lock()
            counts = {'active': 2, 'peak': 2}
            starts, results, snapshots, retired = [], [], [], []

            def finish_legacy():
                for name, path in legacy.items():
                    time.sleep(0.15)
                    with lock:
                        counts['active'] -= 1
                    exp.write_json(path, {'status': 'completed', 'id': name})

            def worker(name):
                with lock:
                    counts['active'] += 1
                    counts['peak'] = max(counts['peak'], counts['active'])
                    starts.append((name, not all(p.exists() for p in legacy.values())))
                time.sleep(0.02)
                with lock:
                    counts['active'] -= 1
                return {'status': 'completed', 'id': name}

            thread = threading.Thread(target=finish_legacy)
            thread.start()
            drain_queue([(str(i), (str(i),)) for i in range(8)], legacy, worker, 4,
                lambda key, value: results.append(key),
                lambda inherited, active, queued: snapshots.append((inherited, active, queued)),
                lambda: retired.append(all(p.exists() for p in legacy.values())),
                executor_factory=ThreadPoolExecutor)
            thread.join()
            self.assertTrue(starts[0][1], 'New work must start before the old batch ends')
            self.assertEqual(len(starts), 8, 'Never rerun inherited tasks')
            self.assertEqual(len(set(results)), 10)
            self.assertLessEqual(counts['peak'], 4)
            self.assertTrue(all(len(old) + len(new) <= 4 for old, new, _ in snapshots))
            self.assertEqual(retired, [True])

    def test_one_new_job_failure_does_not_stop_the_rest(self):
        results = {}

        def worker(name):
            if name == 'bad':
                raise RuntimeError('isolated failure')
            return {'status': 'completed'}

        drain_queue([(name, (name,)) for name in ('one', 'bad', 'two')], {}, worker, 2,
            lambda key, value: results.update({key: value}), lambda *args: None, lambda: None,
            executor_factory=ThreadPoolExecutor)
        self.assertEqual(results['bad']['status'], 'failed')
        self.assertEqual(results['one']['status'], 'completed')
        self.assertEqual(results['two']['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
