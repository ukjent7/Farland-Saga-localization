"""Compatibility shim for tools.test_shards -> fls.core.test_shards."""
import sys
import fls.core.test_shards as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
