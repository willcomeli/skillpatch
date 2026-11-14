"""Plan upstream SkillOpt batches from an already resolved task manifest."""

from skillopt.datasets.base import SplitDataLoader


class ManifestDataLoader(SplitDataLoader):
    def __init__(self, tasks):
        super().__init__(split_mode="split_dir")
        # The manifest already owns split identities; do not split or reload it.
        self._splits = {name: [row for row in tasks if row['split'] == name]
                        for name in ('train', 'val', 'test')}

    def setup(self, cfg):
        """Tasks have already been loaded and their splits resolved."""

    def build_eval_batch(self, env_num, split, seed, **kwargs):
        aliases = {'selection': 'val', 'valid_seen': 'val', 'valid_unseen': 'test'}
        split = aliases.get(split, split)
        if split not in self._splits:
            raise ValueError(f'Unsupported evaluation split: {split}')
        return super().build_eval_batch(env_num, split, seed, **kwargs)
