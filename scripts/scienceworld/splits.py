"""Pin SkillNet task identities and select EviSkill's released cohorts."""

import hashlib
import http.client
import json
from collections import Counter
from pathlib import Path
import re
import runpy
import time
from urllib.error import URLError, HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
SKILLNET_COMMIT = 'edfbda23ac2108b59579300d050218558d20f6a6'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def normalize_task(name):
    # Same legacy-name normalization as ScienceWorld.utils.infer_task.
    return re.sub(r'task-(\d|a|b)+-|[()]', '', name.replace('mendellian', 'mendelian'))


def select_cohorts(raw, simplification=''):
    recipe = runpy.run_path(str(ROOT / 'method/eviskill/scripts/build_scienceworld_split.py'))
    selections = {'train': recipe['MAIN_TRAIN_INDICES'], 'val': recipe['MAIN_VAL_INDICES'],
                  'test': list(range(len(raw['test'])))}
    result, identities = {}, set()
    for split, indices in selections.items():
        source = raw['valid' if split == 'val' else split]
        rows = []
        for index in indices:
            name, variation = source[index]
            task = normalize_task(name)
            identity = (task, variation)
            if identity in identities:
                raise ValueError(f'Duplicate task identity across cohorts: {identity}')
            identities.add(identity)
            rows.append({'task_id': f'{task}--{variation}', 'task': task,
                         'task_family': task, 'variation_idx': variation,
                         'skillnet_index': index, 'skillnet_task': name,
                         'simplification': simplification})
        if split != 'test':
            counts = Counter(row['task_family'] for row in rows)
            if len(counts) != 24 or set(counts.values()) != {4 if split == 'train' else 2}:
                raise ValueError(f'SkillNet cohort differs from EviSkill: {split}, {counts}')
        result[split] = rows
    return result


def prepare(output, simplification=''):
    output = Path(output)
    receipt = output / 'split_manifest.json'
    if receipt.exists():
        manifest = read(receipt)
        raw = {split: read(output / 'source' / f'{split}_indices.json')
               for split in ('train', 'valid', 'test')}
        cohorts = select_cohorts(raw, simplification)
        if (manifest['skillnet_commit'] != SKILLNET_COMMIT
                or manifest['source_digests'] != {k: digest(v) for k, v in raw.items()}
                or manifest['split_digests'] != {k: digest(v) for k, v in cohorts.items()}):
            raise ValueError('Split sources or EviSkill cohort recipe changed')
        for split, rows in cohorts.items():
            if read(output / split / 'items.json') != rows:
                raise ValueError(f'Prepared split was modified: {split}')
        return cohorts
    raw = {}
    for split in ('train', 'valid', 'test'):
        name = f'{split}_indices.json'
        path = output / 'source' / name
        if not path.exists():
            url = (f'https://raw.githubusercontent.com/zjunlp/SkillNet/{SKILLNET_COMMIT}/'
                   f'experiments/src/scienceworld/data/{name}')
            while True:
                try:
                    with urlopen(url, timeout=60) as response:
                        payload = response.read()
                    json.loads(payload)
                    break
                except HTTPError as error:
                    if error.code not in {408, 429, 500, 502, 503, 504}:
                        raise
                except (URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
                    pass
                print(f'Retrying interrupted SkillNet download: {name}', flush=True)
                time.sleep(5)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        raw[split] = read(path)
    cohorts = select_cohorts(raw, simplification)
    for split, rows in cohorts.items():
        save(output / split / 'items.json', rows)
    save(receipt, {'skillnet_commit': SKILLNET_COMMIT,
                   'eviskill_recipe_sha256': hashlib.sha256((ROOT / 'method/eviskill/scripts/build_scienceworld_split.py').read_bytes()).hexdigest(),
                   'counts': {k: len(v) for k, v in cohorts.items()},
                   'source_digests': {k: digest(v) for k, v in raw.items()},
                   'split_digests': {k: digest(v) for k, v in cohorts.items()},
                   'provenance': 'Reconstructed from pinned SkillNet rows using EviSkill zero-based indices; '
                                 'not the unavailable byte-identical EviSkill source manifests.',
                   'simplification': simplification,
                   'task_limits': 'No SkillNet recommended step limits added; ScienceWorld defaults'})
    return cohorts
