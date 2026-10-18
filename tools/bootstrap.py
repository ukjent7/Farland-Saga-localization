"""Compatibility shim for tools.bootstrap -> fls.storage.bootstrap."""
import sys
import fls.storage.bootstrap as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
