"""Compatibility shim for tools.package_release -> fls.storage.package_release."""
import sys
import fls.storage.package_release as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
