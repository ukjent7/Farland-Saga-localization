"""Compatibility shim for tools.iso_layout -> fls.disc.iso_layout."""
import sys
import fls.disc.iso_layout as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
