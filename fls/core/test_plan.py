"""Pick the tests that cover a change, so a focused edit needs a focused run.

`fls test` used to rely on a written rule ("entry points use --cli-only,
resources use test, image encoding adds --pack"). That rule is gone: the tool
works it out from what actually changed, which cannot go stale.

The mapping is explicit rather than import-derived. Import graphs say what a
module touches, not what a test proves, and a wrong guess here is expensive:
it silently skips the regression that mattered.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fls.core.path_compat import REPO

# 每个测试脚本覆盖的仓库路径前缀。扫描全部脚本而不是扫描改动路径，这样
# 一个改动同时命中多个脚本时会自然合并。
SCRIPT_COVERAGE: dict[str, tuple[str, ...]] = {
    'test_cli': ('fls/cli/', 'fls/core/path_compat.py', 'fls/core/test_shards.py'),
    # 选择器本身由端到端套件覆盖：改 CLI 或改动映射都要跑它。
    'test_selector_e2e': ('fls/cli/', 'fls/core/test_plan.py'),
    'test_storage_bundles': (
        'fls/storage/',
        'fls/core/file_hash.py',
    ),
    'test_translation_merge': (
        'fls/text/merge_full_translations.py',
        'fls/text/translation_data.py',
        'fls/text/language_encoding.py',
        'translation/corpus.json',
    ),
    'test_resources': (
        'fls/containers/',
        'fls/builders/build_ui_labels.py',
        'fls/builders/build_equipment_labels.py',
        'fls/builders/build_title_images.py',
        'fls/core/env.py',
    ),
    'test_chinese_rebuild': ('fls/builders/build_alpha.py', 'fls/builders/build_full_languages.py'),
    'test_cd_parity': ('fls/disc/cdrom.py', 'fls/disc/cd_parity.py'),
    'test_iso_extents': ('fls/disc/iso_layout.py', 'fls/disc/iso_extents.py'),
    'test_unity_extract': ('fls/text/extract_unity_text.py', 'config/unity.json'),
    'test_fs2_cd_mode1': ('fls/disc/cdrom.py',),
    'test_fs2_multilingual_layout': ('fls/containers/multilingual_layout.py',),
    'test_fs2_translation_integrity': ('fls/text/', 'translation/', 'config/fs2/'),
    'test_bootstrap': ('fls/storage/bootstrap.py', 'fls/disc/disc_decode.py'),
    'test_workflows': (
        'fls/builders/',
        'fls/text/native_patches.py',
        'fls/text/runtime_text.py',
        'fls/storage/candidate.py',
        'config/patches/',
    ),
}

# 这些前缀一旦变化，workflow 回归要额外重放一次完整加密镜像：扇区填充与
# 容器重打包都在这两处。cdrom 的 EDC/ECC 算法由 test_cd_parity 与
# test_fs2_cd_mode1 直接对拍，不必再付一次整盘重放。
PACK_COVERAGE = (
    'fls/builders/build_alpha.py',
    'fls/storage/candidate.py',
)

# 补丁器是独立模块，改动它只需 go vet / go test，不进入 Python 套件。
GO_COVERAGE = ('patcher/',)


def changed_paths() -> list[str]:
    """Repository-relative paths touched since HEAD, including untracked files."""
    result = subprocess.run(
        ['git', 'status', '--porcelain', '--untracked-files=all'],
        cwd=REPO, capture_output=True, text=True,
    )
    if result.returncode:
        raise SystemExit(f'git status 失败，无法判断改动范围：{result.stderr.strip()}')
    paths = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        entry = line[3:]
        # 重命名显示为 "旧 -> 新"，两侧都算改动。
        for part in entry.split(' -> '):
            path = part.strip().strip('"')
            if path:
                paths.append(path)
    return sorted(set(paths))


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


def plan(paths: list[str]) -> dict[str, object]:
    """Map changed paths to the test scripts, extra flags, and Go module work."""
    scripts = []
    for name, prefixes in SCRIPT_COVERAGE.items():
        if any(_matches(path, prefixes) for path in paths):
            scripts.append(name)
    # 直接改测试文件时跑那个脚本本身；tests/ 下的其他模块（夹具、辅助）
    # 无法判断归属，整套跑。
    known_scripts = set(SCRIPT_COVERAGE)
    for path in paths:
        if not path.startswith('tests/') or not path.endswith('.py'):
            continue
        stem = Path(path).stem
        if stem in known_scripts:
            if stem not in scripts:
                scripts.append(stem)
        else:
            scripts = sorted(set(SCRIPT_COVERAGE) | {'test_workflows'})
            break

    pack = any(_matches(path, PACK_COVERAGE) for path in paths)
    go = any(_matches(path, GO_COVERAGE) for path in paths)
    return {
        'paths': paths,
        'scripts': [f'tests/{name}.py' for name in sorted(scripts)],
        # --pack 只在完整工作流回归上有意义；没选中它就不必提示。
        'pack': pack and 'test_workflows' in scripts,
        'go': go,
        'docs_only': bool(paths) and all(
            path.endswith(('.md', '.webp', '.png')) or path.startswith('docs/')
            for path in paths),
    }
