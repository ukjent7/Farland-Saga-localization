"""Compatibility shim for tools.workspace_status -> fls.storage.workspace_status."""
import sys
import fls.storage.workspace_status as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
