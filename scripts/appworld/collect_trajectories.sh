#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
python_bin="${SKILLPATCH_PYTHON:-$project_root/.venvs/methods/bin/python}"
cd "$project_root"
exec "$python_bin" -m set_up.appworld_experiments collect "$@"
