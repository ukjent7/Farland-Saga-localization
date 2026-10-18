"""Compatibility shim for tools.build_equipment_labels -> fls.builders.build_equipment_labels."""
import sys
import fls.builders.build_equipment_labels as _target

sys.modules[__name__] = _target

if __name__ == '__main__':
    if hasattr(_target, 'main'):
        _target.main()
