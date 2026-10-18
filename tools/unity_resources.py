"""Compatibility shim for tools.unity_resources -> fls.containers.unity_resources."""
import sys
import fls.containers.unity_resources as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
