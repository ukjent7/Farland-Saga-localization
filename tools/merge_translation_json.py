"""Compatibility shim for tools.merge_translation_json -> fls.text.merge_translation_json."""
import sys
import fls.text.merge_translation_json as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
