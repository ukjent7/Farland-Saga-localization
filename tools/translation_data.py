"""Compatibility shim for tools.translation_data -> fls.text.translation_data."""
import sys
import fls.text.translation_data as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
