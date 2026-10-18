"""Compatibility shim for tools.version_storage -> fls.storage.version_storage."""
import sys
import fls.storage.version_storage as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
