"""Compatibility shim for tools.restore_original -> fls.storage.restore_original."""
import sys
import fls.storage.restore_original as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
