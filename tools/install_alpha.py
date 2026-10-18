"""Compatibility shim for tools.install_alpha -> fls.storage.install_alpha."""
import sys
import fls.storage.install_alpha as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
