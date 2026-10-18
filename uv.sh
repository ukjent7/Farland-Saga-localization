#!/usr/bin/env bash
# Keep project environments, caches, temporary files, and bytecode inside the
# private workspace: FARLAND_WORK, or the work/ directory beside this repository.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
work="${FARLAND_WORK:-$repo/../work}"
if [[ -d "$work" ]]; then
    work="$(cd -- "$work" && pwd -P)"
fi
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$work/.venv}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$work/.uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$work/.uv-python}"
export UV_PYTHON_DOWNLOADS="${UV_PYTHON_DOWNLOADS:-never}"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$work/.pycache}"
export TMPDIR="${TMPDIR:-$work/.tmp}"
mkdir -p -- "$TMPDIR"
# The private workspace carries its own project file with the proprietary IDALib
# wheel; a standalone checkout uses this repository's public dependency list.
project="$repo"
if [[ -f "$work/pyproject.toml" ]]; then
    project="$work"
fi
if command -v uv >/dev/null 2>&1; then
    taskUv=uv
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    # Non-interactive shells may not load the user's interactive profile.
    taskUv="$HOME/.local/bin/uv"
else
    echo 'uv was not found in PATH or ~/.local/bin/uv.' >&2
    exit 127
fi
exec "$taskUv" --project "$project" "$@"
