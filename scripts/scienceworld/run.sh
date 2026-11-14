#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: scripts/scienceworld/run.sh OUTPUT [--method standard|boundary-aware|vanilla|all]
       [--experiment FILE] [--issues FILE | --train-run DIR ...]
       [--train-manifest FILE] [--test-manifest FILE] [--workers N] [--attempts N]

Share collection and diagnosis across patched methods. Repeat the same command
to continue a partial run, or pass a saved issue index to start at Stage 2.
HELP
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  usage
  exit 0
fi
if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
python_bin="${SKILLPATCH_PYTHON:-$project_root/.venvs/methods/bin/python}"
caller_dir="$PWD"
absolute() {
  case "$1" in /*) printf '%s\n' "$1" ;; *) printf '%s/%s\n' "$caller_dir" "$1" ;; esac
}
output="$(absolute "$1")"
shift
method=all
experiment="$project_root/configs/scienceworld/experiment.toml"
issues=""
workers=2
attempts=3
train_runs=()
train_manifest=()
test_manifest=()
while [[ $# -gt 0 ]]; do
  if [[ $# -lt 2 ]]; then
    printf 'Missing value for %s\n' "$1" >&2
    exit 2
  fi
  case "$1" in
    --method) method="$2" ;;
    --experiment) experiment="$(absolute "$2")" ;;
    --issues) issues="$(absolute "$2")" ;;
    --train-run) train_runs+=(--train-run "$(absolute "$2")") ;;
    --train-manifest) train_manifest=(--train-manifest "$(absolute "$2")") ;;
    --test-manifest) test_manifest=(--test-manifest "$(absolute "$2")") ;;
    --workers) workers="$2" ;;
    --attempts) attempts="$2" ;;
    *) printf 'Unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
  shift 2
done
case "$method" in standard|boundary-aware|vanilla|all) ;; *) usage >&2; exit 2 ;; esac
if [[ -n "$issues" && ${#train_runs[@]} -gt 0 ]]; then
  printf 'Use --issues or --train-run.\n' >&2
  exit 2
fi

cd "$project_root"
cli() {
  "$python_bin" -Bu -m set_up.cli scienceworld "$@"
}

if [[ "$method" != vanilla && -z "$issues" ]]; then
  if [[ ${#train_runs[@]} -eq 0 ]]; then
    cli collect --experiment "$experiment" --output "$output/train" --attempts "$attempts" ${train_manifest[@]+"${train_manifest[@]}"}
    train_runs=(--train-run "$output/train")
  fi
  cli diagnose --experiment "$experiment" --output "$output/stage1" --workers "$workers" \
    "${train_runs[@]}" ${test_manifest[@]+"${test_manifest[@]}"}
  issues="$output/stage1/issues.json"
fi

if [[ "$method" == vanilla || "$method" == all ]]; then
  cli evaluate --experiment "$experiment" --output "$output/vanilla" --attempts "$attempts" \
    ${test_manifest[@]+"${test_manifest[@]}"}
fi
for induction in standard boundary-aware; do
  if [[ "$method" == "$induction" || "$method" == all ]]; then
    port=8911
    if [[ "$induction" == boundary-aware ]]; then port=9011; fi
    cli evolve --experiment "$experiment" --output "$output/$induction" --issues "$issues" \
      --induction "$induction" --workers "$workers" ${test_manifest[@]+"${test_manifest[@]}"}
    cli evaluate --experiment "$experiment" --output "$output/$induction/evaluation" \
      --stage3-run "$output/$induction/stage3" --server-port-start "$port" --attempts "$attempts" ${test_manifest[@]+"${test_manifest[@]}"}
  fi
done
if [[ "$method" == all ]]; then
  cli compare --vanilla "$output/vanilla" --standard "$output/standard/evaluation" \
    --boundary-aware "$output/boundary-aware/evaluation" --output "$output/test_comparison.json"
fi
