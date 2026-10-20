"""Translate drive-qualified paths found in legacy metadata to POSIX paths.

The public repository holds the code, translation tables, and patch recipes.
The private workspace holds the copyrighted game data: extracted resources,
decoded images, candidates, backups, and the storage archive.
``REPO`` and ``WORK`` name those two roots.
Set ``FARLAND_WORK`` to point ``WORK`` elsewhere when the repository is checked
out on its own; by default the workspace is the ``work`` directory beside the
repository.
"""
from __future__ import annotations

import os
from pathlib import Path


def _find_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / 'pyproject.toml').is_file() and (parent / 'fls').is_dir():
            return parent
    return current.parents[2]


REPO: Path = _find_repo_root()


def _private_work() -> Path:
    override = os.environ.get('FARLAND_WORK')
    if override:
        return Path(override).resolve()
    return (REPO.parent / 'work').resolve()


WORK: Path = _private_work()
WORKSPACE: Path = WORK.parent


def _workspace_alias(path: Path) -> Path:
    """Canonicalize case aliases of this workspace on a Windows mount."""
    parts = path.parts
    root_parts = WORKSPACE.parts
    if (path.is_absolute() and len(parts) >= len(root_parts)
            and tuple(part.casefold() for part in parts[:len(root_parts)])
            == tuple(part.casefold() for part in root_parts)):
        prefix = Path(*parts[:len(root_parts)])
        try:
            if prefix.samefile(WORKSPACE):
                return WORKSPACE.joinpath(*parts[len(root_parts):])
        except OSError:
            pass
    return path


def local_path(value: str | Path) -> Path:
    """Resolve old Windows paths in saved records while running on Linux."""
    val_str = str(value)
    if os.name != 'nt' and len(val_str) > 2 and val_str[1:3] in (':\\', ':/'):
        drive = val_str[0].lower()
        root = Path('/mnt') / drive
        if not root.is_dir():
            root = Path('/') / drive
        return _workspace_alias(root / val_str[3:].replace('\\', '/'))
    return _workspace_alias(Path(value))


def inside_workspace(path: str | Path) -> bool:
    """True when a path sits inside the repository or the private workspace."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    return any(
        resolved.is_relative_to(root.resolve()) and resolved != root.resolve()
        for root in (REPO, WORK)
    )
