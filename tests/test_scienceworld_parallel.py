"""Exercise the real scheduler/worker/checkpoints with a simulated native backend."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.scienceworld import experiment as exp
from scripts.scienceworld.parallel import reserve_port, run_jobs, worker_count
from scripts.scienceworld.splits import read, save


WORKER = '''
import os, socket, sys, time, subprocess
from pathlib import Path
from scripts.scienceworld import experiment as exp
from scripts.scienceworld.splits import read, save
from excutor.runner_common import launcher

def native(config, output, environment, **kwargs):
    output.mkdir(parents=True)
    ref = read(config.tasks_path)[0]
    if ref.get('mode') == 'fatal':
        ready = Path(ref['wait_for'])
        while not ready.exists(): time.sleep(0.01)
        raise ValueError('fixture fatal error')
    port = config.runner_options['server_port_start']
    assert config.runner_options['parallel_workers'] == 1
    assert config.max_tokens is None and config.max_retries == 0
    with socket.socket() as server:
        server.bind(('127.0.0.1', port))
        server.listen()
        start = time.time()
        if ref.get('mode') == 'hang':
            child = subprocess.Popen([sys.executable, '-c',
                'import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);time.sleep(600)'])
            save(Path(ref['ready']), {'pid': os.getpid(), 'child': child.pid})
            while True: time.sleep(0.1)
        time.sleep(ref.get('delay', 0))
        attempt = int(output.parent.name.split('-')[-1])
        if attempt <= ref.get('failures', 0):
            state = {'status': 'backend_error', 'errors': ['ConnectionError: connection reset'],
                     'task_results': []}
        else:
            source = output / 'tasks/task_000001.json'
            save(source, {'dataset': 'scienceworld', 'task_id': ref['task_id'],
                'instruction': 'Fixture task', 'task_family': 'boil', 'success': True,
                'score': 1.0, 'steps': 1,
                'trajectory': [{'step': 0, 'action': 'look around', 'observation': 'done'}],
                'fixture': {'pid': os.getpid(), 'port': port, 'start': start, 'end': time.time()}})
            state = {'status': 'completed', 'task_results': [str(source)]}
        save(output / 'result.json', state)
        return state

launcher.run_native = native
exp.backoff = lambda: None
def discovery(source, config, output, environment, **kwargs):
    output.mkdir()
    result = {'status': 'completed'}
    save(output / 'result.json', result)
    return result

def generation(diagnosis, identifier, config, output, environment, **kwargs):
    output.mkdir()
    path = output / 'patch.json'
    save(path, {'issue': identifier})
    result = {'status': 'generated', 'patch_path': str(path)}
    save(output / 'result.json', result)
    return result

import method.skillpatch.patch_diagnosis.config as dc
import method.skillpatch.patch_diagnosis.discovery as dd
import method.skillpatch.patch_generation.config as gc
import method.skillpatch.patch_generation.pipeline as gp
import set_up.appworld_experiments as ae
dc.load_discovery_config = lambda path: None
gc.Stage2Config = lambda path: None
dd.run_discovery = discovery
gp.run_stage2 = generation
ae.issue_ids = lambda path: ['I000001', 'I000002']
from scripts.scienceworld.jobs import main
sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
'''


class ParallelExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worker = self.root / 'worker.py'
        self.worker.write_text(WORKER)
        self.environment = dict(os.environ)
        # Fixture script lives outside the repository; module entry points in
        # production use python -m and do not need this adjustment.
        self.environment['PYTHONPATH'] = str(exp.ROOT)
        self.environment['EXECUTION_API_KEY'] = 'fixture-private-key'
        self.config = self.root / 'execution.toml'
        for stage in ('diagnosis', 'generation'):
            save(self.root / 'inputs' / f'{stage}.prompt.md', 'Fixture prompt')
        exp.write_native(self.config, self.root / 'tasks.json', self.root / 'SKILL.md',
                         {}, self.root / 'scienceworld')
        self.command = [sys.executable, str(self.worker)]

    def job(self, index, **options):
        return {'kind': 'execute', 'entry': {'id': f'train-{index}', 'split': 'train',
                'config': str(self.config), 'task_ref': {'task_id': f'boil--{index}',
                    'task': 'boil', 'variation_idx': index, **options}},
                'skill': 'Use the current skill.', 'folder': str(self.root / f'task-{index}')}

    def test_process_isolation_bounded_overlap_order_and_checkpoint_reuse(self):
        jobs = [self.job(i, delay=0.5 if i == 0 else 0.15) for i in range(4)]
        sources = run_jobs(jobs, self.environment, 2, command=self.command)
        rows = [read(source) for source in sources]
        self.assertEqual([row['task_id'] for row in rows], [f'boil--{i}' for i in range(4)])
        facts = [row['fixture'] for row in rows]
        self.assertEqual(len({row['pid'] for row in facts}), 4)
        events = [(row['start'], 1) for row in facts] + [(row['end'], -1) for row in facts]
        active = peak = 0
        for _, delta in sorted(events):
            active += delta
            peak = max(peak, active)
        self.assertEqual(peak, 2)
        for left in facts:
            for right in facts:
                if left is not right and max(left['start'], right['start']) < min(left['end'], right['end']):
                    self.assertNotEqual(left['port'], right['port'])
        for job in jobs:
            self.assertNotIn('fixture-private-key', (Path(job['folder']) / '.dispatch/request.json').read_text())
        # New processes recover archives without invoking the fake backend again.
        replay = run_jobs(jobs, self.environment, 2, command=self.command)
        self.assertEqual(sources, replay)
        self.assertTrue(all(len(list(Path(j['folder']).glob('attempt-*'))) == 1 for j in jobs))

    def test_task_retries_are_independent_and_skip_excluded(self):
        jobs = [self.job(1, failures=1), self.job(2, failures=6), self.job(3)]
        sources = run_jobs(jobs, self.environment, 2, command=self.command)
        self.assertIsNotNone(sources[0])
        self.assertIsNone(sources[1])
        self.assertIsNotNone(sources[2])
        self.assertEqual([len(list(Path(j['folder']).glob('attempt-*'))) for j in jobs], [2, 6, 1])
        self.assertEqual(exp.summarize(sources)['count'], 2)
        self.assertEqual(exp.summarize(sources)['skipped_count'], 1)
        run_jobs(jobs, self.environment, 2, command=self.command)
        self.assertEqual(len(list(Path(jobs[1]['folder']).glob('attempt-*'))), 6)

    def test_unlimited_retries_reach_subprocess_workers(self):
        environment = {**self.environment, 'SCIENCEWORLD_TASK_MAX_RETRIES': 'unlimited'}
        jobs = [self.job(1, failures=8), self.job(2)]
        sources = run_jobs(jobs, environment, 10, command=self.command)
        self.assertTrue(all(sources))
        self.assertEqual(len(list(Path(jobs[0]['folder']).glob('attempt-*'))), 9)
        self.assertFalse((Path(jobs[0]['folder']) / 'skipped.json').exists())

    def test_skillpatch_jobs_preserve_task_issue_order_and_stage_checkpoints(self):
        jobs = [{**self.job(i, delay=0.1), 'kind': 'skillpatch', 'inputs': str(self.root / 'inputs')}
                for i in range(3)]
        patches = run_jobs(jobs, self.environment, 2, command=self.command)
        self.assertEqual(len(patches), 3)
        for job, group in zip(jobs, patches):
            self.assertEqual([read(path)['issue'] for path in group], ['I000001', 'I000002'])
            self.assertTrue(all(Path(path).resolve().is_relative_to(Path(job['folder']).resolve())
                                for path in group))
            self.assertEqual(len(list(Path(job['folder']).glob('method-inputs/*/diagnosis/completed.json'))), 1)
        self.assertEqual(run_jobs(jobs, self.environment, 2, command=self.command), patches)
        for job in jobs:
            self.assertEqual(len(list(Path(job['folder']).glob('baseline/attempt-*'))), 1)
            self.assertEqual(len(list(Path(job['folder']).glob('method-inputs/*/diagnosis/attempt-*'))), 1)
        save(self.root / 'inputs/generation.prompt.md', 'Updated generation policy')
        updated = run_jobs(jobs, self.environment, 2, command=self.command)
        self.assertNotEqual(updated, patches)
        self.assertTrue(all(Path(path).exists() for group in patches for path in group))
        for job in jobs:
            self.assertEqual(len(list(Path(job['folder']).glob('baseline/attempt-*'))), 1)
            self.assertEqual(len(list(Path(job['folder']).glob('method-inputs/*/diagnosis/completed.json'))), 2)

    def test_fatal_error_cancels_other_process_groups(self):
        ready = self.root / 'ready.json'
        jobs = [self.job(1, mode='hang', ready=str(ready)),
                self.job(2, mode='fatal', wait_for=str(ready))]
        with self.assertRaisesRegex(RuntimeError, 'fixture fatal error'):
            run_jobs(jobs, self.environment, 2, command=self.command)
        process = read(ready)
        # The scheduler waits for its children and kills each whole group,
        # including a grandchild that ignores SIGINT.
        with self.assertRaises(ProcessLookupError):
            os.kill(process['pid'], 0)
        import subprocess
        state = subprocess.run(['ps', '-p', str(process['child']), '-o', 'stat='],
                               capture_output=True, text=True)
        self.assertTrue(state.returncode != 0 or state.stdout.strip().startswith('Z'))

    def test_config_validation_port_leases_and_execute_routing(self):
        self.assertEqual(worker_count({}, 'SCIENCEWORLD_EXECUTION_WORKERS'), 1)
        for invalid in ('0', '-2', 'four', '1.5'):
            with self.assertRaises(ValueError):
                worker_count({'WORKERS': invalid}, 'WORKERS')
        with reserve_port() as first, reserve_port() as second:
            self.assertNotEqual(first, second)
        job = self.job(1)
        with self.assertRaisesRegex(ValueError, 'distinct output'):
            run_jobs([job, job], self.environment, 2, command=self.command)
        items = [(job['entry'], Path(job['folder']))]
        with patch.object(exp, 'run_jobs', return_value=['/fixture/archive.json']) as dispatch:
            sources = exp.execute_many(items, 'skill', {'SCIENCEWORLD_EXECUTION_WORKERS': '4'},
                                       self.root / 'patches')
        self.assertEqual(sources, [Path('/fixture/archive.json')])
        self.assertEqual(dispatch.call_args.args[2], 4)
        self.assertEqual(dispatch.call_args.args[0][0]['patch_dir'], str(self.root / 'patches'))


if __name__ == '__main__':
    unittest.main()
