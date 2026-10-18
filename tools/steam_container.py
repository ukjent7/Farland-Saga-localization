"""Compatibility shim for tools.steam_container -> fls.containers.steam_container."""
import sys
import fls.containers.steam_container as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
