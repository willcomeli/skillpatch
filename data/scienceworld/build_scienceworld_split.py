#!/usr/bin/env python3
"""Build shared ScienceWorld splits from EviSkill manifests or SkillNet indices.

Adapted from method/eviskill/scripts/build_scienceworld_split.py.
The fixed cohorts and legacy manifest hashes are preserved. Public SkillNet
indices can also be converted into explicitly reconstructed Runner records.
This script does not download data or invoke a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


MAIN_TRAIN_INDICES = [
    1386, 1271, 1400, 105, 334, 1408, 368, 1422, 897, 1286, 1188, 621,
    25, 1035, 142, 1343, 792, 1109, 1283, 1468, 1111, 180, 889, 361,
    1124, 224, 914, 226, 640, 1282, 747, 1099, 729, 1122, 619, 1209,
    276, 450, 328, 292, 110, 1148, 196, 493, 514, 1341, 766, 238,
    1030, 1203, 574, 645, 618, 1351, 1472, 1313, 1365, 326, 331, 899,
    1186, 147, 1375, 1327, 1340, 1121, 79, 847, 958, 1092, 1239, 904,
    1170, 471, 1242, 174, 392, 1290, 760, 1369, 202, 320, 699, 171,
    81, 1345, 923, 1211, 1221, 956, 1015, 436, 353, 622, 1219, 953,
]

MAIN_VAL_INDICES = [
    154, 78, 82, 54, 65, 185, 137, 89, 100, 62, 136, 109, 86, 46, 49,
    193, 150, 119, 13, 67, 157, 190, 171, 91, 64, 47, 156, 167, 51, 33,
    36, 143, 177, 126, 165, 73, 61, 176, 116, 153, 149, 188, 160, 41,
    20, 68, 123, 135,
]

EXPECTED_SOURCE_SHA256 = {
    "train": "79f6065d410bb54643b2499003c4419dbbd85be0e66171503b9466e230c229af",
    "dev": "f6c6c72f09d27cdbac5027a871e04984838c8cc996e7c7cfab060a57511d4c52",
    "test": "7afbb7b5559f4f7e8714197406f297cefb9bfd188017e4b166da259dd6accfa5",
}

SKILLNET_COMMIT = "edfbda23ac2108b59579300d050218558d20f6a6"
EXPECTED_SKILLNET_SHA256 = {
    "train": "c79eea7858383b829269986ee83d0d9a504533e7b4b3a5bc76e5bdc1e6999cd0",
    "dev": "33de480fc2ddb8c7848ce088d1bba7e43ba2ce1fba941ada1fa36f24ee74d133",
    "test": "d6fe951ee08bd5dbc08c72355bb68e672fd51a91e15dea1da57d0f794f1dedbe",
    "max_steps": "035592f47fbe1309b6ed1a260b392a50d94d3163de951508a74e985d93048f73",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_by_skillnet_index(
    rows: list[dict[str, Any]], indices: list[int], split: str
) -> list[dict[str, Any]]:
    by_index = {int(row["skillnet_index"]): dict(row) for row in rows}
    missing = [index for index in indices if index not in by_index]
    if missing:
        raise ValueError(f"{split} source is missing SkillNet indices: {missing[:8]}")
    return [by_index[index] for index in indices]


def family_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row.get("task_family") or "") for row in rows)
    return dict(sorted(counts.items()))


def convert_indices(rows, split, simplification, max_steps):
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{split} indices must be a nonempty JSON list")
    result = []
    for index, row in enumerate(rows):
        if (not isinstance(row, list) or len(row) != 2
                or not isinstance(row[0], str) or not row[0].strip()
                or type(row[1]) is not int or row[1] < 0):
            raise ValueError(f"Invalid {split} index row {index}: expected [task_name, variation_idx]")
        task, variation = row
        item = {
            "task_id": f"scienceworld_{split}_{index:06d}",
            "task": task,
            "task_family": task,
            "variation_idx": variation,
            "simplification": simplification,
            "skillnet_index": index,
            "split": split,
        }
        if max_steps is not None:
            steps = max_steps.get(task)
            if type(steps) is not int or steps <= 0:
                raise ValueError(f"Missing or invalid SkillNet max_steps for {task}")
            item["recommended_max_steps"] = steps
        result.append(item)
    return result


def check_task_identities(splits):
    identities = {}
    task_ids = set()
    for name, rows in splits.items():
        if not rows:
            raise ValueError(f"Empty {name} split")
        seen = set()
        for row in rows:
            identity = (row["task"], row["variation_idx"])
            if identity in seen:
                raise ValueError(f"Duplicate task/variation in {name}: {identity}")
            if row["task_id"] in task_ids:
                raise ValueError(f"Duplicate task_id: {row['task_id']}")
            seen.add(identity)
            task_ids.add(row["task_id"])
        identities[name] = seen
    overlaps = {}
    for left, right in (("train", "val"), ("train", "test"), ("val", "test")):
        shared = identities[left] & identities[right]
        if shared:
            raise ValueError(f"Task/variation overlap between {left} and {right}: {sorted(shared)[:3]}")
        overlaps[f"{left}/{right}"] = len(shared)
    return overlaps


def build(args: argparse.Namespace) -> dict[str, Any]:
    source_dir = Path(args.source_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    source_format = getattr(args, "source_format", "eviskill-manifests")
    reconstructed = source_format == "skillnet-indices"
    if source_format not in {"eviskill-manifests", "skillnet-indices"}:
        raise ValueError(f"Unsupported source format: {source_format}")
    if reconstructed:
        if args.simplification is None or args.step_budget is None:
            raise ValueError("skillnet-indices requires explicit --simplification and --step-budget")
        source_paths = {
            "train": source_dir / "train_indices.json",
            "dev": source_dir / "valid_indices.json",
            "test": source_dir / "test_indices.json",
        }
        if args.step_budget == "skillnet":
            source_paths["max_steps"] = source_dir / "max_steps.json"
        elif args.step_budget != "runner":
            raise ValueError("step-budget must be skillnet or runner")
        expected_hashes = EXPECTED_SKILLNET_SHA256
    else:
        if getattr(args, "simplification", None) is not None or getattr(args, "step_budget", None) is not None:
            raise ValueError("Existing EviSkill records preserve their settings; reconstruction options require skillnet-indices")
        source_paths = {
            "train": source_dir / "train.json",
            "dev": source_dir / "dev.json",
            "test": source_dir / "test.json",
        }
        expected_hashes = EXPECTED_SOURCE_SHA256
    source_hashes = {}
    for name, path in source_paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"ScienceWorld source manifest does not exist: {path}")
        actual_hash = sha256(path)
        source_hashes[name] = actual_hash
        expected_hash = expected_hashes[name]
        if not args.allow_source_mismatch and actual_hash != expected_hash:
            raise ValueError(
                f"Unexpected {path.name} fingerprint: {actual_hash}. "
                "Use the pinned source files or pass "
                "--allow-source-mismatch after verifying row identities."
            )

    train_source = load_json(source_paths["train"])
    dev_source = load_json(source_paths["dev"])
    test = load_json(source_paths["test"])
    if not all(isinstance(rows, list) for rows in (train_source, dev_source, test)):
        raise ValueError("ScienceWorld source manifests must be JSON lists")
    if reconstructed:
        max_steps = load_json(source_paths["max_steps"]) if "max_steps" in source_paths else None
        if max_steps is not None and not isinstance(max_steps, dict):
            raise ValueError("SkillNet max_steps.json must be a JSON object")
        train_source = convert_indices(train_source, "train", args.simplification, max_steps)
        dev_source = convert_indices(dev_source, "val", args.simplification, max_steps)
        test = convert_indices(test, "test", args.simplification, max_steps)

    train = select_by_skillnet_index(train_source, MAIN_TRAIN_INDICES, "train")
    val = select_by_skillnet_index(dev_source, MAIN_VAL_INDICES, "val")
    splits = {"train": train, "val": val, "test": test}
    expected_caps = {"train": 4, "val": 2}
    for name, cap in expected_caps.items():
        counts = family_counts(splits[name])
        if len(counts) != 24 or set(counts.values()) != {cap}:
            raise ValueError(f"Unexpected {name} family coverage: {counts}")

    overlaps = check_task_identities(splits) if reconstructed else None

    for name, rows in splits.items():
        save_json(output_dir / name / "items.json", rows)

    manifest = {
        "benchmark": "ScienceWorld",
        "manifest_type": "stratified_train96_val48_testfull",
        "source": "SkillNet public indices" if reconstructed else "SkillNet-derived full manifests",
        "source_format": source_format,
        "counts": {name: len(rows) for name, rows in splits.items()},
        "strategy": {
            "train": "released four-example cohort for each of 24 task families",
            "val": "released two-example cohort for each of 24 task families",
            "test": "all SkillNet test instances in source order",
        },
        "family_counts": {name: family_counts(rows) for name, rows in splits.items()},
        "source_sha256": source_hashes,
        "source_hashes_verified": all(source_hashes[key] == expected_hashes[key] for key in source_paths),
        "manifest_sha256": {
            name: sha256(output_dir / name / "items.json") for name in splits
        },
    }
    if reconstructed:
        manifest["identity_checks"] = {"duplicate_task_ids": 0, "cross_split_overlap": overlaps}
        manifest["reconstruction"] = {
            "status": "reconstructed",
            "upstream_repository": "https://github.com/zjunlp/SkillNet",
            "expected_upstream_commit": SKILLNET_COMMIT,
            "eviskill_wrapper_hashes_verified": False,
            "index_interpretation": "zero-based row position within each source split",
            "simplification": args.simplification,
            "step_budget": args.step_budget,
            "limitations": [
                "Task records are reconstructed, not the original EviSkill wrapper JSON files.",
                "Simplification and step-budget choices are explicit; equivalence to EviSkill's missing wrappers is unverified.",
                "SDK/JVM version and runtime environment step limit are configured separately.",
            ],
        }
    save_json(output_dir / "split_manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        required=True,
        help="Directory containing EviSkill manifests or public SkillNet index files, according to --source-format.",
    )
    parser.add_argument(
        "--source-format", choices=("eviskill-manifests", "skillnet-indices"),
        default="eviskill-manifests",
        help="Input schema; skillnet-indices reconstructs executable task records.",
    )
    parser.add_argument(
        "--simplification", default=None,
        help='Required for skillnet-indices: e.g. "easy" (SkillNet setting) or "" (no simplification).',
    )
    parser.add_argument(
        "--step-budget", choices=("skillnet", "runner"), default=None,
        help="Required for skillnet-indices: skillnet copies max_steps.json; runner omits recommended_max_steps.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent),
        help="Destination directory for train/val/test manifests (default: this script's directory).",
    )
    parser.add_argument(
        "--allow-source-mismatch",
        action="store_true",
        help="Allow a source fingerprint mismatch while selecting by skillnet_index.",
    )
    args = parser.parse_args()
    print(json.dumps(build(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
