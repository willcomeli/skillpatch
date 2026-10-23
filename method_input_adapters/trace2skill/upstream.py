"""Load the pinned source snapshot without rewriting its internal imports."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'method/trace2skill'


def activate():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def domain_prompt(text):
    # Keep workflow, output schema, constraints and few-shot formats. Only
    # replace the original SpreadsheetBench-specific domain assumptions.
    return (text.replace('spreadsheet manipulation tasks', 'benchmark agent tasks')
            .replace('spreadsheet', 'benchmark task')
            .replace('`input.xlsx`', 'the task-provided inputs')
            .replace('no ground-truth file', 'no hidden ground-truth material'))
