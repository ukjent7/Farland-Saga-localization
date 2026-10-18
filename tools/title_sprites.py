"""Compatibility shim for tools.title_sprites -> fls.containers.title_sprites."""
import sys
import fls.containers.title_sprites as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
