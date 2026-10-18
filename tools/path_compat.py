"""Compatibility shim for tools.path_compat -> fls.core.path_compat."""
import sys
import fls.core.path_compat as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
