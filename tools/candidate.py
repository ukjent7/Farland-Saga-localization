"""Compatibility shim for tools.candidate -> fls.storage.candidate."""
import sys
import fls.storage.candidate as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
