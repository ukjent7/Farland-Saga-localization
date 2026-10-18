"""Compatibility shim for tools.extract_catalog -> fls.text.extract_catalog."""
import sys
import fls.text.extract_catalog as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
