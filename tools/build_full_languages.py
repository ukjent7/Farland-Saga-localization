"""Compatibility shim for tools.build_full_languages -> fls.builders.build_full_languages."""
import sys
import fls.builders.build_full_languages as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
