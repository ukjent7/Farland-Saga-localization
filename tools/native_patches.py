"""Compatibility shim for tools.native_patches -> fls.text.native_patches."""
import sys
import fls.text.native_patches as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
