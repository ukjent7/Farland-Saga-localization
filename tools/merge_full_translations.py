"""Compatibility shim for tools.merge_full_translations -> fls.text.merge_full_translations."""
import sys
import fls.text.merge_full_translations as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
