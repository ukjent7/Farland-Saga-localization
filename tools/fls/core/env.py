"""Common workspace environment, core paths, and original file resolution."""
from __future__ import annotations

import json
from pathlib import Path

from fls.core.file_hash import sha256 as digest
from fls.core.path_compat import REPO, WORK, WORKSPACE, local_path

ROOT: Path = WORK.parent
SOURCE: Path = WORK / 'extracted/steam'
STAGE: Path = WORK / 'staging/alpha'
EXE: Path = WORK / 'analysis/FarlandSaga.exe'
GLYPH_SLOTS: Path = WORK / 'assets/sources/glyph-slots.zh-CN.json'

GAME_ROOT: Path | None = None


def set_game_root(path: str | Path | None) -> None:
    global GAME_ROOT
    GAME_ROOT = Path(path).resolve() if path else None


def original_file(name: str, game_root: Path | None = None) -> Path:
    """Locate the pristine original of a game file.

    An installation record wins when present. Otherwise prefer the analyzed
    copy under work/analysis, then the game root passed with --game, so a
    standalone checkout with FARLAND_WORK pointing elsewhere still resolves the
    executable and container it was analyzed against.
    """
    record = WORK / 'install_report.json'
    if record.exists():
        report = json.loads(record.read_text(encoding='utf-8-sig'))
        candidate = (local_path(report['backup_directory']) / name).resolve()
        assert candidate.is_relative_to(WORK.resolve())
        assert digest(candidate) == report['files'][name]['original_sha256']
        return candidate
    analyzed = WORK / 'analysis' / name
    if analyzed.is_file():
        return analyzed
    effective_root = game_root if game_root is not None else GAME_ROOT
    if effective_root is not None and (effective_root / name).is_file():
        return effective_root / name
    return ROOT / name


def font_index(pair: tuple[int, int]) -> int:
    """Calculate Shift-JIS / Japanese font glyph table index."""
    hi, lo = pair
    return (hi - (0x81 if hi < 0xA0 else 0xC1)) * 188 + lo - (0x40 if lo < 0x7F else 0x41)
