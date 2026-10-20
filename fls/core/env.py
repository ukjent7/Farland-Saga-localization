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
GLYPH_SLOTS: Path = (
    REPO / 'config/glyph-slots.zh-CN.json'
    if (REPO / 'config/glyph-slots.zh-CN.json').is_file()
    else WORK / 'assets/sources/glyph-slots.zh-CN.json'
)

GAME_ROOT: Path | None = None


def set_game_root(path: str | Path | None) -> None:
    global GAME_ROOT
    GAME_ROOT = Path(path).resolve() if path else None


def resolve_backup(recorded: str | Path, work: Path = WORK) -> Path:
    """Map a recorded backup directory to the copy that lives in ``work``.

    A record written in another checkout (the retail install beside a renamed
    test copy, for example) names a directory outside this workspace. The same
    directory name under our own ``work/backups`` belongs to this workspace and
    takes over, so a copied workspace keeps working without editing the record.

    Raises ``ValueError`` when neither location is usable.
    """
    work = Path(work).resolve()
    recorded = Path(recorded)
    candidate = local_path(recorded).resolve()
    if candidate.is_relative_to(work) and candidate != work:
        return candidate
    local = work / 'backups' / recorded.name
    if local.is_dir():
        return local.resolve()
    raise ValueError(
        f'原版备份不在当前工作区：{recorded}；'
        f'请把该目录复制到 {work / "backups"} 下，或重新执行一次安装')


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
        candidate = resolve_backup(report['backup_directory']) / name
        expected = report['files'][name]['original_sha256']
        if digest(candidate) != expected:
            raise ValueError(f'原版备份与安装记录不一致: {candidate}')
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
