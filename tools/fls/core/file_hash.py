"""Shared streaming file-hash helpers."""
from __future__ import annotations

import hashlib
from pathlib import Path

READ_SIZE = 1024 * 1024
CACHE_LIMIT = 65536
_DIGESTS: dict[tuple[str, int, int, int], str] = {}


def sha256(path: str | Path) -> str:
    """Return a file's SHA-256 without loading it all into memory.

    Results are cached per process, keyed by resolved path, inode, size and
    nanosecond mtime.
    """
    path_obj = Path(path).resolve()
    status = path_obj.stat()
    key = (str(path_obj), status.st_ino, status.st_size, status.st_mtime_ns)
    cached = _DIGESTS.get(key)
    if cached is not None:
        return cached

    digest = hashlib.sha256()
    with path_obj.open('rb') as source:
        while block := source.read(READ_SIZE):
            digest.update(block)

    result = digest.hexdigest()
    if len(_DIGESTS) >= CACHE_LIMIT:
        _DIGESTS.clear()
    _DIGESTS[key] = result
    return result
