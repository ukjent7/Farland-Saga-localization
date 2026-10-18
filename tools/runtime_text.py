"""Compatibility shim for tools.runtime_text -> fls.text.runtime_text."""
import sys
import fls.text.runtime_text as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
