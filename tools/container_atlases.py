"""Compatibility shim for tools.container_atlases -> fls.containers.container_atlases."""
import sys
import fls.containers.container_atlases as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
