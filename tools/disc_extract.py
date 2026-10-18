"""Compatibility shim for tools.disc_extract -> fls.disc.disc_extract."""
import sys
import fls.disc.disc_extract as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
