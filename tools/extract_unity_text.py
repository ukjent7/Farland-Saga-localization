"""Compatibility shim for tools.extract_unity_text -> fls.text.extract_unity_text."""
import sys
import fls.text.extract_unity_text as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
