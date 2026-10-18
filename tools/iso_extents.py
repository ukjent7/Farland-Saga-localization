"""Compatibility shim for tools.iso_extents -> fls.disc.iso_extents."""
import sys
import fls.disc.iso_extents as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
