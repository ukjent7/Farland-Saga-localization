"""Compatibility shim for tools.ida_sh2 -> fls.text.ida_sh2."""
import sys
import fls.text.ida_sh2 as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
