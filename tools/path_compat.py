"""Translate drive-qualified paths found in legacy metadata to POSIX paths.

The public repository (the parent of this tools/ directory) holds the code,
the translation tables and the patch recipes. The private workspace holds the
copyrighted game data: extracted resources, decoded images, candidates,
backups and the storage archive. ``REPO`` and ``WORK`` name those two roots.
Set ``FARLAND_WORK`` to point ``WORK`` elsewhere when the repository is checked
out on its own; by default the repository is expected inside the workspace.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _private_work():
    override = os.environ.get('FARLAND_WORK')
    if override:
        return Path(override).resolve()
    return REPO.parent


WORK = _private_work()
WORKSPACE = WORK.parent


def _workspace_alias(path):
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


def local_path(value):
    """Resolve old Windows paths in saved records while running on Linux."""
    value = str(value)
    if os.name != 'nt' and len(value) > 2 and value[1:3] in (':\\', ':/'):
        drive = value[0].lower()
        root = Path('/mnt') / drive
        if not root.is_dir():
            root = Path('/') / drive
        return _workspace_alias(root / value[3:].replace('\\', '/'))
    return _workspace_alias(Path(value))


def inside_workspace(path):
    """True when a path sits inside the repository or the private workspace."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(root.resolve()) and resolved != root.resolve()
               for root in (REPO, WORK))
