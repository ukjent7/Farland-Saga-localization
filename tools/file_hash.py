"""Compatibility shim for tools.file_hash -> fls.core.file_hash."""
import sys
import fls.core.file_hash as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
