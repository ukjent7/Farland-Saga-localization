"""Project entry point: reusable builders, declarative patches, assets, verification."""
from __future__ import annotations

import sys
import fls.cli.app as _app
from fls.cli.app import (
    DEFAULT_JOBS,
    QUICK_TESTS,
    build_parser,
    compact_if_installed,
    parser,
)

invoke = _app.invoke


def main() -> None:
    original = _app.invoke
    try:
        current_mod = sys.modules[__name__]
        _app.invoke = getattr(current_mod, 'invoke', _app.invoke)
        _app.main()
    finally:
        _app.invoke = original


if __name__ == '__main__':
    main()
