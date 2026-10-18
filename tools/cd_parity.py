"""Compatibility shim for tools.cd_parity -> fls.disc.cd_parity."""
import sys
import fls.disc.cd_parity as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
