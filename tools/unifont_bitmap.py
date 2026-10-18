"""Compatibility shim for tools.unifont_bitmap -> fls.containers.unifont_bitmap."""
import sys
import fls.containers.unifont_bitmap as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
