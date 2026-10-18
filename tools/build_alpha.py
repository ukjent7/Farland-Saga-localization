"""Compatibility shim for tools.build_alpha -> fls.builders.build_alpha."""
import sys
import fls.builders.build_alpha as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
