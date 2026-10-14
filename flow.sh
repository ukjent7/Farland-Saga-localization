#!/usr/bin/env bash
# Entry point for the public repository. Reverse-engineering products live in the
# private workspace: FARLAND_WORK, or the work/ directory beside this repository.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$repo"
if (( $# == 0 )); then
    set -- --help
fi
exec bash "$repo/uv.sh" run --locked python "$repo/tools/localize.py" "$@"
