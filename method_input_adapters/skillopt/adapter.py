"""The upstream EnvAdapter contract backed by the shared executors."""

from pathlib import Path
from skillopt.envs.base import EnvAdapter
from ..execution import select_tasks
from .results import export_results


class SharedExecutionAdapter(EnvAdapter):
    def __init__(self, tasks, bridge):
        self.tasks, self.bridge = tasks, bridge

    def setup(self, cfg):
        super().setup(cfg)
        for name, default in [('analyst_workers', 1), ('failure_only', False), ('minibatch_size', 8), ('edit_budget', 4)]:
            setattr(self, name, cfg.get(name, default))

    def build_train_env(self, batch_size, seed, **kwargs):
        return select_tasks(self.tasks, 'train', batch_size, seed)

    def build_eval_env(self, env_num, split, seed, **kwargs):
        aliases = {'val': 'val', 'valid_seen': 'val', 'valid_unseen': 'test', 'selection': 'val', 'test': 'test'}
        if split not in aliases:
            raise ValueError(f'Unsupported evaluation split: {split}')
        return select_tasks(self.tasks, aliases[split], env_num, seed)

    def rollout(self, env_manager, skill_content, out_dir, **kwargs):
        output = Path(out_dir)
        if output.exists():
            raise ValueError('Rollout output already exists; candidate runs must be fresh')
        sources = [self.bridge.run(entry, skill_content, output.with_name(output.name + '-executions') / entry['id'])
                   for entry in env_manager]
        return export_results(sources, output, ids=[entry['id'] for entry in env_manager])

    def get_task_types(self):
        return sorted({entry.get('task_type', 'task') for entry in self.tasks})
