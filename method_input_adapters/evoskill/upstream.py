"""Load the pinned checkout without installing its unrelated execution SDKs."""

import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2] / 'method' / 'evoskill'


def load_module(name):
    if not (ROOT / 'src/loop/runner.py').is_file():
        raise ImportError('Initialize method/evoskill with git submodule update --init method/evoskill')
    existing = sys.modules.get('src')
    if existing is not None and Path(existing.__file__).resolve() != ROOT / 'src/__init__.py':
        raise ImportError('Another package named src is loaded; use a separate EvoSkill process')
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module('src.' + name)
