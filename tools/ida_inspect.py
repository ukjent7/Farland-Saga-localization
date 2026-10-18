"""Compatibility shim for tools.ida_inspect -> fls.text.ida_inspect."""
import sys
import fls.text.ida_inspect as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
