"""Dispatch method-internal runs to the existing four executors."""

from dataclasses import replace
import json
from pathlib import Path, PurePosixPath
import random
import re
import shutil

from .archive import read_json, write_json
from set_up.config import load_config
from set_up.native_config import load_native_config, read_benchmark


def load_tasks(path):
    path = Path(path).resolve(strict=True)
    rows = read_json(path)
    if not isinstance(rows, list) or not rows:
        raise ValueError('Task manifest must be a nonempty JSON list')
    identities, ids = {}, set()
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', row.get('id', '')):
            raise ValueError('Each task needs a safe nonempty id')
        if row['id'] in ids or row.get('split') not in {'train', 'val', 'test'}:
            raise ValueError('Duplicate task ID or invalid split (train/val/test)')
        ids.add(row['id'])
        config = Path(row['config']).expanduser()
        row['config'] = str((path.parent / config).resolve() if not config.is_absolute() else config.resolve())
        benchmark = read_benchmark(Path(row['config']))
        if benchmark == 'skillsbench':
            request, _ = load_config(Path(row['config']))
            identity = (benchmark, str(request.task_dir))
        else:
            cfg = load_native_config(Path(row['config']))
            refs = read_json(cfg.tasks_path)
            refs = refs if isinstance(refs, list) else [refs]
            if 'task_ref' not in row:
                if len(refs) != 1:
                    raise ValueError('Native manifest entries need task_ref when their config contains multiple tasks')
                row['task_ref'] = refs[0]
            ref = row['task_ref']
            identity = (benchmark, str(ref['task_id']) if isinstance(ref, dict) and 'task_id' in ref
                        else json.dumps(ref, sort_keys=True) if isinstance(ref, dict) else str(ref))
        if identity in identities:
            raise ValueError('The same task occurs more than once; splits must be disjoint')
        identities[identity] = row['split']
    return rows


def select_tasks(rows, split, count, seed):
    pool = [row for row in rows if row['split'] == split]
    if not pool:
        raise ValueError(f'No tasks in split {split}')
    if type(count) is not int or count < 0:
        raise ValueError('Task count must be a nonnegative integer')
    return random.Random(seed).sample(pool, min(count or len(pool), len(pool)))


def _target(pack, relative):
    candidates = sorted(pack.glob('*/SKILL.md'))
    if relative is None:
        if len(candidates) != 1:
            raise ValueError('Specify skill_target (<package>/SKILL.md) for a multi-Skill pack')
        return candidates[0]
    parts = PurePosixPath(relative).parts
    if len(parts) != 2 or parts[0] in {'.', '..'} or parts[1] != 'SKILL.md':
        raise ValueError('skill_target must be <package>/SKILL.md')
    target = pack / relative
    if not target.is_file() or target.is_symlink() or not target.resolve().is_relative_to(pack.resolve()):
        raise ValueError('skill_target is missing or escapes the copied pack')
    return target


class SharedExecutorBridge:
    def __init__(self, environment):
        self.environment = dict(environment)

    def run(self, entry, skill_content, output, *, skill_dir=None):
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=False)
        config_path = Path(entry['config'])
        benchmark = read_benchmark(config_path)
        if benchmark == 'skillsbench':
            from excutor.excutors import Executor
            from excutor.executor_adapter.registry import get_execution_adapter
            from excutor.excutor_runners.benchflow import BenchFlowBackend
            request, _ = load_config(config_path)
            base = request.skills_dir or request.task_dir / 'environment/skills'
            pack = output / 'skills'
            shutil.copytree(base, pack)
            target = _target(pack, entry.get('skill_target'))
            if skill_dir is not None:
                shutil.rmtree(target.parent)
                shutil.copytree(skill_dir, target.parent)
            else:
                target.write_text(skill_content, encoding='utf-8')
            request = replace(request, skills_dir=pack, skill_mode='with-skill')
            result = Executor(get_execution_adapter(benchmark), BenchFlowBackend()).run(
                request, output / 'execution', self.environment)
            if result.status != 'completed':
                raise ValueError(f'Shared execution failed: {result.status}; see {output / "execution/result.json"}')
            return output / 'execution'
        from excutor.eviskill_runners.launcher import run_native
        config = load_native_config(config_path)
        if 'task_ref' not in entry:
            raise ValueError('Native execution requires one explicit task_ref')
        write_json(output / 'tasks.json', [entry['task_ref']])
        text = skill_content
        if skill_dir is not None:
            skill_dir = Path(skill_dir)
            text = (skill_dir / 'SKILL.md').read_text(encoding='utf-8')
            for path in sorted(skill_dir.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Native Skill bundles cannot include symlinks')
                if path.is_file() and path != skill_dir / 'SKILL.md':
                    if path.suffix.lower() not in {'.md', '.txt'}:
                        raise ValueError('Native runners support text reference documents, not executable/binary Skill resources')
                    text += f'\n\n--- Skill reference: {path.relative_to(skill_dir).as_posix()} ---\n' + path.read_text(encoding='utf-8')
        skill_path = output / 'SKILL.md'
        skill_path.write_text(text, encoding='utf-8')
        config = replace(config, tasks_path=output / 'tasks.json', skill_path=skill_path)
        state = run_native(config, output / 'execution', self.environment)
        if state['status'] != 'completed' or len(state['task_results']) != 1:
            raise ValueError(f'Shared execution failed: {state["status"]}; see {output / "execution/result.json"}')
        return Path(state['task_results'][0])
