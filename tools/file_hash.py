"""Shared streaming file-hash helpers."""
import hashlib
from pathlib import Path


READ_SIZE = 1024 * 1024
CACHE_LIMIT = 65536
_DIGESTS = {}


def sha256(path):
    """Return a file's SHA-256 without loading it all into memory.

    Results are cached per process, keyed by resolved path, inode, size and
    nanosecond mtime, so repeated verification passes over an unchanged
    candidate hash it once instead of once per pass. A rewrite changes the key
    even when the size stays the same, so a stale hit cannot survive an edit.
    """
    path = Path(path).resolve()
    status = path.stat()
    key = (str(path), status.st_ino, status.st_size, status.st_mtime_ns)
    cached = _DIGESTS.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(READ_SIZE):
            digest.update(block)
    result = digest.hexdigest()
    if len(_DIGESTS) >= CACHE_LIMIT:
        _DIGESTS.clear()
    _DIGESTS[key] = result
    return result
