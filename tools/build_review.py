"""Compatibility shim for tools.build_review -> fls.builders.build_review."""
import sys
import fls.builders.build_review as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
