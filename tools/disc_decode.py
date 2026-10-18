"""Compatibility shim for tools.disc_decode -> fls.disc.disc_decode."""
import sys
import fls.disc.disc_decode as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
