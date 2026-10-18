"""Compatibility shim for tools.process_guard -> fls.core.process_guard."""
import sys
import fls.core.process_guard as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
