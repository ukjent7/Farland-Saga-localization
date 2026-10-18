"""Compatibility shim for tools.audit_remaining_text -> fls.text.audit_remaining_text."""
import sys
import fls.text.audit_remaining_text as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
