"""Compatibility shim for tools.workspace_assets -> fls.storage.workspace_assets."""
import sys
import fls.storage.workspace_assets as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
