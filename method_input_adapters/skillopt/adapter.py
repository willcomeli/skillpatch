"""The upstream EnvAdapter contract backed by the shared executors."""

from pathlib import Path
import json
from skillopt.envs.base import EnvAdapter
from excutor.executor_adapter.storage import sha256_file, write_json
from ..execution import select_tasks
from .results import export_results


class SharedExecutionAdapter(EnvAdapter):
    def __init__(self, tasks, bridge, *, frozen_trajectory_manifest=None, initial_skill=None):
        self.tasks, self.bridge = tasks, bridge
        self._first_training_rollout_seen = False
        self._frozen_manifest_path = None
        if frozen_trajectory_manifest is not None:
            if initial_skill is None:
                raise ValueError('Frozen trajectories require the exact initial Skill file')
            self._initial_skill_path = Path(initial_skill).expanduser().resolve(strict=True)
            self._frozen_manifest_path = Path(frozen_trajectory_manifest).expanduser().resolve(strict=True)
            self._frozen_manifest_sha256 = sha256_file(self._frozen_manifest_path)
            self._check_frozen_inputs()

    def _check_frozen_inputs(self):
        from set_up.appworld_trajectories import load_manifest
        from set_up.native_config import load_native_config
        from excutor.eviskill_runners.appworld_manifests import load_official_appworld_split

        if sha256_file(self._frozen_manifest_path) != self._frozen_manifest_sha256:
            raise ValueError('Frozen trajectory manifest changed after binding')
        manifest = load_manifest(self._frozen_manifest_path)
        if sha256_file(self._frozen_manifest_path) != self._frozen_manifest_sha256:
            raise ValueError('Frozen trajectory manifest changed during verification')
        frozen_skill = Path(manifest['initial_skill']['path']).read_text(encoding='utf-8')
        if self._initial_skill_path.read_text(encoding='utf-8') != frozen_skill:
            raise ValueError('Initial Skill differs from the frozen trajectories')
        source_config = json.loads(Path(manifest['execution_config']['path']).read_text(encoding='utf-8'))
        source_refs = json.loads(Path(manifest['tasks_snapshot']['path']).read_text(encoding='utf-8'))
        official = {row['task_id']: row for row in load_official_appworld_split(
            source_config['runner_options']['data_dir'], 'train')}
        sources = {row['task_id']: row for row in manifest['rows']}
        refs = {row['task_id']: row for row in source_refs}

        def normalized_ref(ref):
            if not isinstance(ref, dict) or ref.get('task_id') not in official:
                raise ValueError('Frozen training task must identify an official AppWorld train task')
            return {**official[ref['task_id']], **ref}

        train_tasks = [entry for entry in self.tasks if entry['split'] == 'train']
        if not train_tasks:
            raise ValueError('Frozen trajectories require selected training tasks')
        identities = []
        matched = {}
        settings = ('benchmark', 'model', 'api_key_env', 'base_url_env', 'temperature',
                    'max_tokens', 'timeout_sec', 'max_retries', 'runner_options')
        for entry in train_tasks:
            ref = normalized_ref(entry.get('task_ref'))
            task_id = ref['task_id']
            if task_id not in sources or ref != normalized_ref(refs[task_id]):
                raise ValueError('Selected training task differs from its frozen trajectory')
            config = load_native_config(Path(entry['config']))
            if (config.skill_path is None
                    or config.skill_path.read_text(encoding='utf-8') != frozen_skill):
                raise ValueError('Training execution config does not use the frozen initial Skill')
            if any(getattr(config, key) != source_config.get(key, 0 if key == 'max_retries' else None)
                   for key in settings):
                raise ValueError('Training execution settings differ from the frozen trajectories')
            endpoint = config.base_url or self.bridge.environment.get(config.base_url_env)
            if endpoint != source_config.get('base_url'):
                raise ValueError('Training model endpoint differs from the frozen trajectories')
            identities.append(task_id)
            if entry['id'] in matched:
                raise ValueError('Duplicate selected training ID')
            matched[entry['id']] = dict(entry=entry.copy(), source=sources[task_id])
        if len(set(identities)) != len(identities):
            raise ValueError('Duplicate selected training task identity')
        for entry in self.tasks:
            ref = entry.get('task_ref')
            if (entry['split'] != 'train' and isinstance(ref, dict)
                    and ref.get('task_id') in sources):
                raise ValueError('Validation/test tasks must be disjoint from frozen training tasks')
        self._frozen_skill = frozen_skill
        self._frozen_sources = matched
        self._frozen_archive_root = self._frozen_manifest_path.parent
        self._frozen_execution_root = Path(manifest['source_run']['path']).parent

    def check_output(self, output):
        """Keep method outputs outside the shared read-only archive."""
        if self._frozen_manifest_path is not None:
            output = Path(output).resolve()
            if (output.is_relative_to(self._frozen_archive_root)
                    or output.is_relative_to(self._frozen_execution_root)):
                raise ValueError('SkillOpt output must be outside the frozen trajectory archive')

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
        self.check_output(output)
        entries = list(env_manager)
        # Only the trainer's initial training phase may consume the archive.
        # Baseline/candidate selection and slow/meta evaluation use live runs.
        first_train = (self._frozen_manifest_path is not None
                       and not self._first_training_rollout_seen
                       and kwargs.get('use_eval_feedback') is True
                       and entries and all(entry['split'] == 'train' for entry in entries))
        if first_train:
            self._first_training_rollout_seen = True
            self._check_frozen_inputs()
            if skill_content != self._frozen_skill:
                raise ValueError('First training rollout must use the exact frozen initial Skill')
            if (len({entry['id'] for entry in entries}) != len(entries)
                    or any(entry['id'] not in self._frozen_sources
                           or entry != self._frozen_sources[entry['id']]['entry'] for entry in entries)):
                raise ValueError('Training rollout tasks differ from the bound frozen inputs')
            reused = [self._frozen_sources[entry['id']]['source'] for entry in entries]
            rows = export_results([row['source'] for row in reused], output,
                                  ids=[entry['id'] for entry in entries])
            self._check_frozen_inputs()
            write_json(output / 'frozen_trajectory_reuse.json', {
                'manifest_path': str(self._frozen_manifest_path),
                'manifest_sha256': self._frozen_manifest_sha256,
                'scope': 'first_initial_skill_training_rollout',
                'rows': [{'id': entry['id'], 'task_id': row['task_id'],
                          'source': row['source'], 'sha256': row['sha256']}
                         for entry, row in zip(entries, reused)],
            })
            return rows
        sources = [self.bridge.run(entry, skill_content, output.with_name(output.name + '-executions') / entry['id'])
                   for entry in entries]
        return export_results(sources, output, ids=[entry['id'] for entry in entries])

    def get_task_types(self):
        return sorted({entry.get('task_type', 'task') for entry in self.tasks})
