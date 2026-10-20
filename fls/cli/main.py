"""Command-line interface application and dispatch logic for localization tools."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from fls.core.path_compat import REPO, WORK
from fls.core.test_shards import SHARD_ENV, balance, clear_shards, merge_shards


def compact_if_installed() -> None:
    """Archive historical bulky files; retention policy needs an install report."""
    if (WORK / 'install_report.json').is_file():
        from fls.storage.version_storage import compact
        compact(apply=True)


def invoke(module_name: str, arguments: list[str]) -> None:
    """Dispatch execution to a sub-tool module by its dotted package path."""
    previous = sys.argv
    try:
        sys.argv = [module_name + '.py', *arguments]
        importlib.import_module(module_name).main()
    finally:
        sys.argv = previous


DEFAULT_JOBS = 8

QUICK_TESTS = [
    'Workflows.test_chinese_builder_preserves_item_values',
    'Workflows.test_condition_tables_ko_kr',
    'Workflows.test_condition_tables_en_us',
    'Workflows.test_dash_glyph',
    'Workflows.test_equipment_builder_can_reuse_localized_base',
    'Workflows.test_field_overflow_rejected',
    'Workflows.test_frontend_container_fit_contract',
    'Workflows.test_frontend_menu_rejects_wrong_source_and_overflow',
    'Workflows.test_full_builder_preserves_later_fixes',
    'Workflows.test_idempotent_condition_table',
    'Workflows.test_item_price_guard_rejects_legacy_zeroing',
    'Workflows.test_reject_output_outside_workspace',
    'Workflows.test_save_folder_rejects_changed_instructions_and_overflow',
    'Workflows.test_ui_builders_can_reuse_localized_base',
    'Workflows.test_unsupported_language_rejected',
]


def _pump(process: subprocess.Popen, sink, number: int, total: int) -> None:
    """Mirror one shard's output live, tagged, while teeing it to its log."""
    assert process.stdout is not None
    for line in process.stdout:
        sink.write(line)
        sink.flush()
        print(f'[{number}/{total}] {line}', end='')
    process.stdout.close()


def _spawn_shards(launches: list[list[str]], *, label: str) -> None:
    """Run each argv list as a parallel subprocess, tagging and teeing its output."""
    total = len(launches)
    if total == 1:
        result = subprocess.run(launches[0], cwd=WORK.parent)
        if result.returncode:
            raise SystemExit(result.returncode)
        return

    clear_shards(total)
    environment = dict(os.environ)
    running = []
    threads = []
    for number, argv in enumerate(launches, 1):
        environment[SHARD_ENV] = f'{number}/{total}'
        log = WORK / 'analysis' / f'{label}-{number}.log'
        sink = log.open('w', encoding='utf-8')
        process = subprocess.Popen(
            argv,
            cwd=WORK.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=environment,
        )
        running.append((number, process, sink, log))
        thread = threading.Thread(
            target=_pump, args=(process, sink, number, total), daemon=True
        )
        thread.start()
        threads.append(thread)

    failures = [number for number, process, _, _ in running if process.wait()]
    for thread in threads:
        thread.join()
    for _, _, sink, _ in running:
        sink.close()

    merge_shards(total)

    if failures:
        for number, _, _, log in running:
            if number in failures:
                print(f'--- shard {number}/{total} log: {log} ---', file=sys.stderr)
        raise SystemExit(f'{label} shards failed: {failures}')


def select_tests(patterns: list[str]) -> dict[Path, list[str]]:
    """Map each test script to the test ids matching any pattern.

    A pattern matches when it is a case-insensitive substring of the id, so
    ``menu_spacing``, ``Workflows.test_menu`` and ``test_menu_spacing`` all
    reach the same test. Scripts without a match are left out entirely, which
    is what keeps a targeted run fast.
    """
    wanted = [pattern.lower() for pattern in patterns]
    selected: dict[Path, list[str]] = {}
    for name, ids in discover_tests().items():
        hits = [test for test in ids if any(item in test.lower() for item in wanted)]
        if hits:
            selected[REPO / 'tests' / f'{name}.py'] = hits
    return selected


def discover_tests(refresh: bool = False) -> dict[str, list[str]]:
    """Load every test script and list its test ids, in stable order.

    Discovery imports all thirteen scripts, which costs about five seconds, so
    the result is cached under ``work/maintenance`` and reused until one of the
    scripts changes size or timestamp. ``--list`` and ``--only`` both hit it.

    Runs in a subprocess so heavy module imports and any test that executes at
    import time cannot pollute the dispatcher.
    """
    cache = WORK / 'maintenance/test_inventory.json'
    sources = sorted((REPO / 'tests').glob('test_*.py'))
    stamp = {path.name: [path.stat().st_size, path.stat().st_mtime_ns]
             for path in sources}
    if not refresh and cache.is_file():
        cached = json.loads(cache.read_text(encoding='utf-8'))
        if cached.get('sources') == stamp:
            return cached['tests']

    probe = (
        'import importlib.util, json, sys, unittest\n'
        'from pathlib import Path\n'
        'def walk(suite):\n'
        '    for item in suite:\n'
        '        if isinstance(item, unittest.TestSuite):\n'
        '            yield from walk(item)\n'
        '        else:\n'
        '            yield item\n'
        'result = {}\n'
        'for path in sorted(Path(sys.argv[1]).glob("test_*.py")):\n'
        '    spec = importlib.util.spec_from_file_location("probe_" + path.stem, path)\n'
        '    module = importlib.util.module_from_spec(spec)\n'
        '    sys.argv = [str(path)]\n'
        '    try:\n'
        '        spec.loader.exec_module(module)\n'
        '    except SystemExit:\n'
        '        pass\n'
        '    # Test ids carry the probe module prefix; the scripts run as __main__,\n'
        '    # so strip it and keep the Class.method form unittest accepts there.\n'
        '    result[path.stem] = [".".join(test.id().split(".")[1:]) for test in walk(\n'
        '        unittest.TestLoader().loadTestsFromModule(module))]\n'
        'print(json.dumps(result))\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', probe, str(REPO / 'tests')],
        cwd=REPO, capture_output=True, text=True,
    )
    if result.returncode:
        raise SystemExit(f'无法枚举测试：{result.stderr.strip()}')
    tests = json.loads(result.stdout)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({'sources': stamp, 'tests': tests}, ensure_ascii=False, indent=2),
        encoding='utf-8')
    return tests


def _run_workflows(script: Path, *, pack: bool, quick: bool, jobs: int,
                   tests: list[str] | None = None) -> None:
    """Run workflow regressions as balanced parallel shards when requested."""
    options = ['--pack'] if pack else []
    if tests:
        # 选中的测试同样按预计耗时切分并行；一个进程跑十几个重测就退回串行
        # 时长。只有一两个时并行会白付启动开销，交给 balance 的 jobs 上限。
        groups = [group for group in balance(tests, min(jobs, len(tests))) if group]
        launches = [[sys.executable, str(script), *options, *group] for group in groups]
        _spawn_shards(launches, label='test-workflows')
        return
    if quick:
        groups = [group for group in balance(QUICK_TESTS, jobs) if group]
        launches = [[sys.executable, str(script), *options, *group] for group in groups]
    elif jobs > 1:
        launches = [[sys.executable, str(script), *options] for _ in range(jobs)]
    else:
        result = subprocess.run([sys.executable, str(script), *options], cwd=WORK.parent)
        if result.returncode:
            raise SystemExit(result.returncode)
        return
    _spawn_shards(launches, label='test-workflows')


def suite_scripts(cli_only: bool) -> list[Path]:
    """The scripts a full run covers, in the order they always ran."""
    scripts = [REPO / 'tests' / name for name in (
        'test_cli.py',
        'test_storage_bundles.py',
        'test_translation_merge.py',
        'test_resources.py',
        'test_chinese_rebuild.py',
        'test_cd_parity.py',
        'test_iso_extents.py',
        'test_unity_extract.py',
        'test_fs2_cd_mode1.py',
        'test_fs2_multilingual_layout.py',
        'test_fs2_translation_integrity.py',
        'test_selector_e2e.py',
    )]
    if not cli_only:
        scripts.append(REPO / 'tests/test_bootstrap.py')
        scripts.append(REPO / 'tests/test_workflows.py')
    return scripts


def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line arguments parser."""
    root = argparse.ArgumentParser(
        description='Project entry point: reusable builders, declarative patches, assets, verification.'
    )
    commands = root.add_subparsers(dest='command', required=True)

    commands.add_parser(
        'status',
        help='Read installation and active candidate metadata without hashing or restoring',
    )
    commands.add_parser(
        'doctor',
        help='Check the Python environment, candidate availability, installed and original hashes',
    )

    tests = commands.add_parser(
        'test',
        help='Run CLI checks and frozen-resource regressions; no installation',
    )
    tests.add_argument(
        '--pack', action='store_true', help='Also replay a full encrypted disc'
    )
    tests.add_argument(
        '--cli-only', action='store_true', help='Only run fast entry-point and safety checks'
    )
    tests.add_argument(
        '--quick',
        action='store_true',
        help='Run a representative workflow subset for a fast inner loop',
    )
    tests.add_argument(
        '--jobs',
        type=int,
        help=f'Parallel workflow shards (default {DEFAULT_JOBS}; 1 runs serially)',
    )
    tests.add_argument(
        '--serial', action='store_true', help='Run the workflow regressions in a single process'
    )
    tests.add_argument(
        '--only',
        action='append',
        metavar='PATTERN',
        dest='only',
        help='Run only tests whose name contains this substring; repeatable',
    )
    tests.add_argument(
        '--list',
        action='store_true',
        dest='list_tests',
        help='List every test name and exit without running',
    )
    tests.add_argument(
        '--changed',
        action='store_true',
        help='Run only the tests covering the current git changes; prints Go steps',
    )
    tests.add_argument(
        '--paths',
        action='append',
        metavar='PATH',
        dest='paths',
        help='With --changed: plan for these paths instead of reading git; repeatable',
    )
    tests.add_argument(
        '--dry-run',
        action='store_true',
        dest='dry_run',
        help='Report what --only or --changed selects, then exit without running',
    )
    tests.add_argument(
        '--json',
        action='store_true',
        dest='as_json',
        help='Print the selection as JSON (with --dry-run or --list)',
    )

    install = commands.add_parser(
        'install', help='Install a requested candidate, or preflight with --check-only'
    )
    source = install.add_mutually_exclusive_group(required=True)
    source.add_argument('--stage', help='Explicit release directory under staging')
    source.add_argument(
        '--language',
        choices=['zh-CN', 'ko-KR', 'en-US'],
        help='Select the active candidate from storage policy',
    )
    install.add_argument(
        '--check-only',
        action='store_true',
        help='Check hashes without installing or creating backups',
    )
    install.add_argument(
        '--game-is-closed',
        action='store_true',
        help='Acknowledge the Windows game is closed before writing files',
    )

    restore = commands.add_parser(
        'restore-original', help='Restore original game files from the protected backup'
    )
    restore.add_argument(
        '--check-only',
        action='store_true',
        help='Check restore readiness without writing game files',
    )
    restore.add_argument(
        '--game-is-closed',
        action='store_true',
        help='Acknowledge the Windows game is closed before writing files',
    )

    patch = commands.add_parser('patch', help='Apply a recipe to a frozen base candidate')
    patch.add_argument('--base', required=True)
    patch.add_argument('--output', required=True)
    patch.add_argument('--language', required=True, choices=['zh-CN', 'ko-KR', 'en-US'])
    patch.add_argument(
        '--recipe', required=True, help='Recipe name or JSON path under work'
    )
    patch.add_argument('--version')
    patch.add_argument('--resources-only', action='store_true')
    patch.add_argument('--no-preview', action='store_true')

    release = commands.add_parser(
        'package-release', help='Package a candidate into a distributable patch payload'
    )
    release.add_argument('--stage', required=True, help='候选目录名（work/staging 下）')
    release.add_argument('--output', help='输出目录（相对于 work/），默认 work/release')
    release.add_argument(
        '--patcher', help='补丁器可执行文件；提供时同时生成终端用户整合包'
    )

    patcher = commands.add_parser(
        'build-patcher',
        help='Build the self-contained Windows patcher with payloads embedded',
    )
    patcher.add_argument(
        '--output',
        default='release/FarlandSagaPatcher-windows-amd64.exe',
        help='输出路径（相对于 work/），或绝对路径',
    )
    patcher.add_argument(
        '--stage',
        action='append',
        dest='stages',
        help='要内嵌的候选目录名，可重复；默认中、韩、英三个',
    )

    build = commands.add_parser('build', help='Rebuild full text or selected native UI resources')
    build.add_argument('kind', choices=['full', 'ui', 'equipment', 'titles'])
    build.add_argument('--base', required=True)
    build.add_argument('--output', required=True)
    build.add_argument('--language', required=True, choices=['zh-CN', 'ko-KR', 'en-US'])
    build.add_argument('--resources-only', action='store_true')

    first = commands.add_parser(
        'build-first', help='Build the first candidate straight from extracted originals'
    )
    first.add_argument('--output', required=True, help='New candidate directory under work/staging')
    first.add_argument('--language', choices=['zh-CN'], default='zh-CN')
    first.add_argument(
        '--game', help='Game installation root; stages its FarlandSaga.exe into work/analysis'
    )
    first.add_argument(
        '--force', action='store_true', help='Replace a differing work/analysis/FarlandSaga.exe'
    )
    first.add_argument(
        '--resources-only',
        action='store_true',
        help='Prepare resources and a capacity report without packing the disc',
    )

    unpack = commands.add_parser(
        'unpack', help='Decode the purchased game install into decoded/ and extracted/steam'
    )
    unpack.add_argument(
        '--game', help='Game installation root; defaults to the workspace install root'
    )
    unpack.add_argument(
        '--force',
        action='store_true',
        help='Continue even when the install carries a patched candidate',
    )

    verify = commands.add_parser('verify', help='Verify packaged files and native resource hashes')
    verify.add_argument('--stage', required=True)

    assets = commands.add_parser(
        'assets', help='Manage generated image previews and freeze original sources'
    )
    assets.add_argument(
        'operation', choices=['audit', 'cleanup', 'gallery', 'verify', 'freeze']
    )
    assets.add_argument(
        '--apply', action='store_true', help='Apply configured cleanup; default is an inventory'
    )
    assets.add_argument(
        '--game',
        help='Game installation root for freeze; defaults to the workspace install root',
    )
    assets.add_argument(
        '--force',
        action='store_true',
        help='Freeze even when the install carries a patched candidate',
    )

    unity = commands.add_parser(
        'extract-unity',
        help='Rebuild config/unity.json from the frozen Unity originals',
    )
    unity.add_argument(
        '--source', help='Frozen Unity original directory; defaults to work/assets/sources/unity'
    )
    unity.add_argument(
        '--output', help='Row table to write; defaults to config/unity.json'
    )
    unity.add_argument('--review', help='Where to write hits the corpus does not cover')
    unity.add_argument(
        '--corpus', help='Shared corpus to align against; defaults to translation/corpus.json'
    )
    unity.add_argument(
        '--check', action='store_true', help='Only verify the existing row table; write nothing'
    )

    merge = commands.add_parser(
        'merge', help='Validate the shared corpus or merge an ID-keyed language draft'
    )
    merge.add_argument('--language', choices=['zh-CN', 'ko-KR', 'en-US'], default='zh-CN')
    merge.add_argument(
        '--table',
        action='append',
        choices=['event', 'events', 'catalog', 'description', 'host', 'unity', 'condition'],
        help='Limit a draft merge to one or more corpus kinds',
    )
    merge.add_argument(
        '--draft', help='AI JSON output under work with translations keyed by stable ID'
    )
    merge.add_argument(
        '--overwrite',
        action='store_true',
        help='Allow intentional replacement of existing translations',
    )

    clean = commands.add_parser(
        'clean', help='Verify releases and remove rebuildable packing intermediates'
    )
    clean.add_argument('--apply', action='store_true')

    storage = commands.add_parser(
        'storage', help='Compact, bundle, restore, or verify historical versions'
    )
    storage.add_argument('operation', choices=['compact', 'bundle', 'restore', 'verify'])
    storage.add_argument(
        '--apply', action='store_true', help='Apply compact or create audited bundles'
    )
    storage.add_argument(
        '--snapshot', help='Registered snapshot path or ID for restore/verify'
    )

    titles = commands.add_parser('extract-titles', help='Export one source title contact sheet')
    titles.add_argument('--individual', action='store_true')

    return root


# Backward-compatible parser function
parser = build_parser


def main() -> None:
    cli = build_parser()
    args = cli.parse_args()

    if args.command in {'status', 'doctor'}:
        from fls.storage.workspace_status import doctor, status
        result = status() if args.command == 'status' else doctor()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == 'doctor' and not result['ok']:
            raise SystemExit(1)
        return

    if args.command == 'test':
        if args.cli_only and args.pack:
            cli.error('--pack cannot be combined with --cli-only')
        if args.cli_only and args.quick:
            cli.error('--quick cannot be combined with --cli-only')
        if args.cli_only and (args.serial or args.jobs is not None):
            cli.error('shard options cannot be combined with --cli-only')
        if args.cli_only and args.only:
            cli.error('--only runs selected tests; drop --cli-only')
        if args.quick and args.pack:
            cli.error('--quick cannot be combined with --pack')

        if args.only and args.changed:
            cli.error('--only selects tests by name; --changed infers them; pick one')
        if args.paths and not args.changed:
            cli.error('--paths only applies with --changed')

        jobs = 1 if args.serial else (DEFAULT_JOBS if args.jobs is None else args.jobs)
        if jobs < 1:
            cli.error('--jobs must be at least 1')

        if args.list_tests:
            inventory = discover_tests()
            if args.as_json:
                print(json.dumps(inventory, ensure_ascii=False, indent=2))
            else:
                for name, ids in inventory.items():
                    print(f'{name} ({len(ids)}):')
                    for test in ids:
                        print(f'  {test}')
            return

        def selection() -> dict:
            """Describe what this invocation would run, without running it."""
            if args.only:
                chosen = select_tests(args.only)
                return {
                    'mode': 'only',
                    'patterns': args.only,
                    'scripts': {path.name: names for path, names in chosen.items()},
                    'pack': False,
                    'go': False,
                }
            if args.changed:
                from fls.core.test_plan import changed_paths, plan
                paths = args.paths if args.paths else changed_paths()
                outcome = plan(paths)
                return {
                    'mode': 'changed',
                    'paths': paths,
                    'scripts': {Path(name).name: [] for name in outcome['scripts']},
                    'pack': bool(outcome['pack']),
                    'go': bool(outcome['go']),
                }
            names = [script.name for script in suite_scripts(args.cli_only)]
            return {'mode': 'full', 'scripts': {name: [] for name in names},
                    'pack': bool(args.pack), 'go': False}

        if args.dry_run:
            report = selection()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        if args.changed:
            report = selection()
            if not report['paths']:
                print('No changes in the working tree; nothing to run.', file=sys.stderr)
                return
            print(f'{len(report["paths"])} changed files select '
                  f'{len(report["scripts"])} test scripts:', file=sys.stderr)
            for name in report['scripts']:
                print(f'  {name}', file=sys.stderr)
            if report['pack']:
                print('Image encoding changed; adding --pack for a full replay.', file=sys.stderr)
            if report['go']:
                print('Patcher changed; also run: cd patcher && go vet ./... && go test ./...',
                      file=sys.stderr)
            if not report['scripts']:
                print('No Python test covers these changes.', file=sys.stderr)
                return
            args.cli_only = False
            args.pack = report['pack']
            scripts = [REPO / 'tests' / name for name in report['scripts']]
        else:
            scripts = suite_scripts(args.cli_only)

        if args.only:
            selected = select_tests(args.only)
            if not selected:
                cli.error(f'No test matches {args.only}; see test --list')
            for path, names in selected.items():
                print(f'{path.name}: {len(names)} selected', file=sys.stderr)
                started = time.monotonic()
                if path.name == 'test_workflows.py':
                    _run_workflows(path, pack=args.pack, quick=False, jobs=jobs, tests=names)
                else:
                    result = subprocess.run(
                        [sys.executable, str(path), *names], cwd=WORK.parent)
                    if result.returncode:
                        raise SystemExit(result.returncode)
                print(f'{path.name} finished in {time.monotonic() - started:.1f}s',
                      file=sys.stderr)
            return

        for script in scripts:
            if not script.is_file():
                continue
            started = time.monotonic()
            if script.name == 'test_workflows.py' and not args.cli_only:
                _run_workflows(script, pack=args.pack, quick=args.quick, jobs=jobs)
            else:
                result = subprocess.run([sys.executable, str(script)], cwd=WORK.parent)
                if result.returncode:
                    raise SystemExit(result.returncode)
            print(f'{script.name} finished in {time.monotonic() - started:.1f}s', file=sys.stderr)
        return

    if args.command == 'install':
        from fls.storage.workspace_status import active_stage
        stage = active_stage(args.language).name if args.language else args.stage
        options = ['--stage', stage]
        if args.check_only:
            options.append('--check-only')
        if args.game_is_closed:
            options.append('--game-is-closed')
        invoke('fls.storage.install_alpha', options)
        return

    if args.command == 'restore-original':
        options = []
        if args.check_only:
            options.append('--check-only')
        if args.game_is_closed:
            options.append('--game-is-closed')
        invoke('fls.storage.restore_original', options)
        return

    if args.command == 'unpack':
        options = []
        if args.game:
            options.extend(['--game', args.game])
        if args.force:
            options.append('--force')
        invoke('fls.storage.bootstrap', ['unpack', *options])
        return

    if args.command == 'extract-unity':
        options = []
        for name in ('source', 'output', 'review', 'corpus'):
            value = getattr(args, name)
            if value:
                options.extend([f'--{name}', value])
        if args.check:
            options.append('--check')
        invoke('fls.text.extract_unity_text', options)
        return

    # Heavy imports deferred for commands that require them
    from fls.storage.candidate import prune_intermediates, stage_path, verify_candidate
    from fls.text.native_patches import apply_recipe
    from fls.storage.version_storage import activate_candidate, compact
    from fls.storage.workspace_assets import (
        cleanup_images,
        export_gallery,
        rewrite_image_links,
        verify_previews,
    )

    if args.command == 'patch':
        result = apply_recipe(
            args.base,
            args.output,
            args.language,
            args.recipe,
            args.version,
            args.resources_only,
            not args.no_preview,
        )
        rewrite_image_links()
        export_gallery()
        if not args.resources_only:
            activate_candidate(result.stage, args.language)
            compact_if_installed()
        print(
            json.dumps(
                {
                    'output': str(result.stage),
                    'status': result.report['build_status'],
                    'translated_texts': result.report['translated_texts'],
                },
                ensure_ascii=False,
            )
        )

    elif args.command == 'package-release':
        from fls.storage.package_release import package
        result = package(args.stage, args.output, args.patcher)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == 'build-patcher':
        from fls.builders.build_patcher import build
        result = build(args.output, args.stages)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == 'build':
        output = stage_path(args.output)
        if output.exists():
            cli.error('Use a new output directory; existing releases are not overwritten')
        language = args.language
        if args.kind == 'equipment' and language == 'zh-CN':
            cli.error(
                'Equipment atlas translation is Korean/English; Chinese labels retain their source tiles'
            )
        modules = {
            'full': 'fls.builders.build_full_languages',
            'ui': 'fls.builders.build_ui_labels',
            'equipment': 'fls.builders.build_equipment_labels',
            'titles': 'fls.builders.build_title_images',
        }
        options = [
            '--base',
            str(stage_path(args.base)),
            '--output',
            str(output),
            '--language',
            language,
        ]
        if args.resources_only:
            options.append('--resources-only')
        invoke(modules[args.kind], options)
        rewrite_image_links()
        export_gallery()
        if not args.resources_only:
            activate_candidate(output, args.language)
            compact_if_installed()

    elif args.command == 'verify':
        report = verify_candidate(args.stage)
        print(
            json.dumps(
                {
                    'version': report.get('version', args.stage),
                    'verified_files': len(report['files']),
                    'verified_resources': len(report.get('changed_resources', [])),
                },
                ensure_ascii=False,
            )
        )

    elif args.command == 'merge':
        if args.overwrite and not args.draft:
            cli.error('--overwrite requires --draft')
        options = []
        if args.draft:
            options.extend(['--language', args.language, '--draft', args.draft])
            aliases = {'events': 'event'}
            for kind in args.table or []:
                options.extend(['--table', aliases.get(kind, kind)])
            if args.overwrite:
                options.append('--overwrite')
        elif args.table:
            cli.error('--table requires --draft')
        invoke('fls.text.merge_full_translations', options)

    elif args.command == 'clean':
        result = prune_intermediates(args.apply)
        (WORK / 'maintenance').mkdir(exist_ok=True)
        (WORK / 'maintenance/intermediate_cleanup.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        print(
            json.dumps(
                {
                    'applied': result['applied'],
                    'intermediates': len(result['files']),
                    'bytes_reclaimed': result['bytes_reclaimed'],
                },
                ensure_ascii=False,
            )
        )

    elif args.command == 'storage':
        if args.operation not in {'compact', 'bundle'} and args.apply:
            cli.error('--apply only applies to storage compact or bundle')
        if args.operation == 'restore' and not args.snapshot:
            cli.error('storage restore requires --snapshot')

        if args.operation == 'compact':
            if args.snapshot:
                cli.error('compact uses the retention policy, not --snapshot')
            result = compact(args.apply)
        elif args.operation == 'bundle':
            if args.snapshot:
                cli.error(
                    'bundle operates on the configured cold snapshot set, not --snapshot'
                )
            from fls.storage.version_storage import bundle
            result = bundle(args.apply)
        elif args.operation == 'restore':
            from fls.storage.version_storage import materialize
            result = materialize(args.snapshot)
        else:
            from fls.storage.version_storage import verify_all
            result = verify_all(args.snapshot)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == 'extract-titles':
        invoke('fls.containers.title_sprites', ['--individual'] if args.individual else [])
        rewrite_image_links()
        export_gallery()

    elif args.command == 'build-first':
        output = stage_path(args.output)
        if output.exists():
            cli.error('Use a new output directory; existing releases are not overwritten')
        options = ['--stage', output.name, '--version', output.name]
        if args.game:
            options.extend(['--game', args.game])
        if args.force:
            options.append('--force')
        if args.resources_only:
            options.append('--resources-only')
        invoke('fls.builders.build_alpha', options)
        rewrite_image_links()
        export_gallery()

    elif args.command == 'assets':
        # Properly scoped under command == 'assets'
        if args.operation in {'audit', 'cleanup'}:
            result = cleanup_images(args.operation == 'cleanup' and args.apply)
            (WORK / 'maintenance').mkdir(exist_ok=True)
            report_name = (
                'image_cleanup.json' if result['applied'] else 'image_inventory.json'
            )
            report_path = WORK / 'maintenance' / report_name
            report_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            filtered = {
                k: v
                for k, v in result.items()
                if k not in {'files', 'updated_documents'}
            }
            print(json.dumps(filtered, ensure_ascii=False))

        elif args.operation == 'gallery':
            rewrite_image_links()
            export_gallery()
            print(WORK / 'assets/index.html')

        elif args.operation == 'verify':
            print(json.dumps(verify_previews(), ensure_ascii=False))

        elif args.operation == 'freeze':
            options = []
            if args.game:
                options.extend(['--game', args.game])
            if args.force:
                options.append('--force')
            invoke('fls.storage.bootstrap', ['freeze', *options])


if __name__ == '__main__':
    main()
