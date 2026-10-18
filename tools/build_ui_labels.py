"""Compatibility shim for tools.build_ui_labels -> fls.builders.build_ui_labels."""
import sys
import fls.builders.build_ui_labels as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
