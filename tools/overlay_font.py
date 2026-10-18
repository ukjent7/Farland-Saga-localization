"""Compatibility shim for tools.overlay_font -> fls.containers.overlay_font."""
import sys
import fls.containers.overlay_font as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
