"""Compatibility shim for tools.build_title_images -> fls.builders.build_title_images."""
import sys
import fls.builders.build_title_images as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
