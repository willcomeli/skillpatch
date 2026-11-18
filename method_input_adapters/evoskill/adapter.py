"""EvoSkill Agent/scorer contracts backed by official executor archives."""

import asyncio
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4

from excutor.executor_adapter.storage import tree_hashes
from set_up.native_config import read_benchmark
from ..archive import load_archive, write_json
from .upstream import load_module

GROUND_TRUTH = 'official_executor_feedback'


def archive_trace(source, *, receipt=None, model='shared-executor', duration_ms=0):
    """Preserve all archived events; the answer is a score receipt, not a prediction."""
    archive = load_archive(source)
    harness = load_module('harness')
    response = load_module('schemas').AgentResponse
    receipt = receipt or 'executor-result-' + uuid4().hex
    # No completeness flag is inferred or added. Retain other archive caveats.
    limitations = [line for line in archive.limitations
                   if not line.startswith('Trajectory completeness')]
    payload = dict(benchmark=archive.benchmark, task_id=archive.task_id,
                   instruction=archive.instruction, success=archive.success,
                   score=archive.score, conversation=archive.conversation,
                   limitations=limitations, source=str(archive.source))
    trace = harness.AgentTrace[response](
        uuid=receipt, model=model, duration_ms=duration_ms, total_cost_usd=0,
        num_turns=archive.n_turns, usage={'available': False},
        result=json.dumps(payload, ensure_ascii=False), is_error=False,
        output=response(final_answer=receipt, reasoning='Official executor feedback; see archived events.'),
        messages=archive.conversation)
    return trace, archive


def snapshot_skills(workspace, output):
    """Snapshot the exact active program, including multiple skills and resources."""
    workspace = Path(workspace).resolve(strict=True)
    source = workspace / '.claude/skills'
    if source.is_symlink():
        raise ValueError('Active Skill pack cannot be a symlink')
    if source.exists():
        for path in source.rglob('*'):
            if path.is_symlink():
                raise ValueError('Skill resources cannot be symlinks')
        for path in source.iterdir():
            if not path.is_dir() or not (path / 'SKILL.md').is_file():
                raise ValueError('Each active Skill must be a directory containing SKILL.md')
        shutil.copytree(source, output)
    else:
        Path(output).mkdir(parents=True)
    return tree_hashes(Path(output))


def native_skill_text(pack):
    """Native runners accept full text injection, not a filesystem/tool bundle."""
    parts = []
    for path in sorted(Path(pack).rglob('*')):
        if path.is_file():
            if path.suffix.lower() not in {'.md', '.txt'}:
                raise ValueError('Native executors cannot deliver executable/binary EvoSkill resources')
            parts.append(f'--- Skill file: {path.relative_to(pack).as_posix()} ---\n' +
                         path.read_text(encoding='utf-8'))
    return '\n\n'.join(parts)


class SharedExecutionAdapter:
    """Acts as EvoSkill's base Agent; proposers/generators remain upstream agents.

    Entries must come from execution.load_tasks (validated train/val/test splits).
    Use a separate program Git repository; never give ProgramManager this repo.
    """

    def __init__(self, tasks, bridge, workspace, output, *, model, score_mode, concurrency):
        tasks = list(tasks)
        if score_mode not in {'hard', 'soft'}:
            raise ValueError('Choose score_mode=hard (official success) or soft (official score)')
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError('concurrency must be a positive integer')
        self.tasks = {row['id']: dict(row) for row in tasks}
        if len(self.tasks) != len(tasks) or not tasks:
            raise ValueError('Nonempty tasks with unique manifest IDs are required')
        if any(row['split'] not in {'train', 'val', 'test'} for row in tasks):
            raise ValueError('Unknown task split')
        self.bridge, self.model, self.score_mode = bridge, model, score_mode
        self.workspace = Path(workspace).resolve(strict=True)
        self.output = Path(output).resolve()
        if self.output.is_relative_to(self.workspace):
            raise ValueError('Execution archives must be outside the program Git workspace')
        self.output.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._receipts = {}
        self.response_model = load_module('schemas').AgentResponse

    def _get_options(self):
        # Metadata only; shared execution config controls the actual model/tools.
        return dict(sdk='shared_executor', model=self.model, working_directory=str(self.workspace),
                    system='', tools=[], output_schema=self.response_model.model_json_schema())

    def data(self):
        """Use existing split membership, never invoke upstream random re-splitting."""
        train, val, test = {}, [], []
        for key, entry in self.tasks.items():
            ref = entry.get('task_ref')
            family = ref.get('task_family') if isinstance(ref, dict) else None
            category = str(entry.get('task_type') or family or 'tasks')
            if entry['split'] == 'train':
                train.setdefault(category, []).append((key, GROUND_TRUTH))
            else:
                (val if entry['split'] == 'val' else test).append((key, GROUND_TRUTH, category))
        return train, val, test

    async def run(self, question):
        if question not in self.tasks:
            raise ValueError('EvoSkill requested an unknown manifest task')
        async with self._semaphore:
            return await asyncio.to_thread(self._run, question)

    def _run(self, question):
        entry = self.tasks[question]
        folder = self.output / ('execution-' + uuid4().hex)
        folder.mkdir()
        pack = folder / 'skills'
        hashes = snapshot_skills(self.workspace, pack)
        benchmark = read_benchmark(Path(entry['config']))
        start = time.monotonic()
        if benchmark == 'skillsbench':
            source = self.bridge.run(entry, '', folder / 'run', skill_pack=pack)
        else:
            source = self.bridge.run(entry, native_skill_text(pack), folder / 'run')
        trace, archive = archive_trace(source, model=self.model,
                                      duration_ms=int((time.monotonic() - start) * 1000))
        expected = entry.get('task_ref', {}).get('task_id') if isinstance(entry.get('task_ref'), dict) else None
        if expected is not None and archive.task_id != expected:
            raise ValueError('Executor returned a different task identity')
        if tree_hashes(pack) != hashes:
            raise ValueError('Executor modified the candidate Skill snapshot')
        value = float(archive.success) if self.score_mode == 'hard' else archive.score
        self._receipts[trace.output.final_answer] = (question, value)
        write_json(folder / 'trace.json', trace.model_dump(mode='json'))
        write_json(folder / 'receipt.json', dict(task=entry, score_mode=self.score_mode,
                   official_success=archive.success, official_score=archive.score,
                   receipt=trace.output.final_answer, skill_hashes=hashes, source=str(source)))
        return trace

    def scorer(self, question, predicted, ground_truth):
        if ground_truth != GROUND_TRUTH or predicted not in self._receipts:
            raise ValueError('Unknown official-score receipt; model text cannot supply a reward')
        task, value = self._receipts[predicted]
        if question != task:
            raise ValueError('Score receipt belongs to a different task')
        return value

    def build_loop(self, config, method_agents, manager, **kwargs):
        """Keep native evolution; replace only base execution and evaluation transport."""
        from dataclasses import replace
        upstream = load_module('loop')
        if config.evolution_mode != 'skill_only':
            raise ValueError('This adapter delivers Skills; prompt_only needs a separate prompt execution contract')
        if Path(manager.cwd).resolve() != self.workspace:
            raise ValueError('ProgramManager must use the same isolated program workspace')
        if not (self.workspace / '.git').exists():
            raise ValueError('Initialize a separate Git repository for the program workspace')
        root = Path(__file__).resolve().parents[2]
        if self.workspace == root or root.is_relative_to(self.workspace) or self.workspace.is_relative_to(root / 'method'):
            raise ValueError('ProgramManager must not operate on the source checkout')
        (self.workspace / '.evoskill').mkdir(exist_ok=True)
        train, val, _ = self.data()
        if not train or not val:
            raise ValueError('Evolution requires separate train and val tasks')
        adapter = self

        class SharedExecutorLoop(upstream.SelfImprovingLoop):
            async def _ensure_base_program(self):
                # Upstream's options serializer knows only its SDK backends.
                # Store truthful execution metadata instead of labelling this
                # environment executor as a Codex/Claude execution Agent.
                if 'base' not in self.manager.list_programs():
                    program = load_module('registry.models').ProgramConfig(
                        name='base', system_prompt={}, allowed_tools=[],
                        metadata=dict(execution_backend='shared_executor', model=adapter.model,
                                      score_mode=adapter.score_mode))
                    self.manager.create_program('base', program)
                await super()._ensure_base_program()

            async def _evaluate(self, data):
                if not data:
                    raise ValueError('Cannot score an empty evaluation split')
                traces = await asyncio.gather(*(adapter.run(q) for q, _, _ in data))
                return sum(adapter.scorer(q, trace.output.final_answer, answer)
                           for trace, (q, answer, _) in zip(traces, data)) / len(data)

        # Upstream trace cache uses Git/model alone; it omits executor config and
        # environments. Bypass it so stale scores cannot select a candidate.
        return SharedExecutorLoop(replace(config, cache_enabled=False),
                                  replace(method_agents, base=self), manager,
                                  train, val, scorer=self.scorer, **kwargs)


def create_adapter(manifest, workspace, output, environment, *, model, score_mode, concurrency):
    """Public entry point: validate task identities/splits, then use existing executors."""
    from ..execution import SharedExecutorBridge, load_tasks
    return SharedExecutionAdapter(load_tasks(manifest), SharedExecutorBridge(environment),
                                  workspace, output, model=model, score_mode=score_mode,
                                  concurrency=concurrency)
