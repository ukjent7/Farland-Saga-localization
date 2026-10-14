"""Project entry point: reusable builders, declarative patches, assets, verification."""
import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from path_compat import REPO, WORK
from test_shards import SHARD_ENV, balance, clear_shards, merge_shards


def invoke(module_name, arguments):
    previous = sys.argv
    try:
        sys.argv = [module_name + '.py', *arguments]
        importlib.import_module(module_name).main()
    finally:
        sys.argv = previous


DEFAULT_JOBS = 4

# Representative workflow subset for the fast inner loop: one case per
# builder family plus every rejection guard, leaving the trilingual atlas
# replays, the runtime-hook replay and the two encrypted-disc replays to the
# full tiers.
QUICK_TESTS = [
    'Workflows.test_chinese_builder_preserves_item_values',
    'Workflows.test_condition_tables',
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


def _pump(process, sink, number, total):
    """Mirror one shard's output live, tagged, while teeing it to its log."""
    for line in process.stdout:
        sink.write(line)
        sink.flush()
        print(f'[{number}/{total}] {line}', end='')
    process.stdout.close()


def _run_workflows(script, *, pack, quick, jobs):
    """Run the workflow regressions, as balanced parallel shards when asked.

    Each shard writes its own proof and timing records; the parent folds them
    into the shared files after every shard finishes, so the audit trail stays
    identical to a serial run.
    """
    options = ['--pack'] if pack else []
    if quick:
        groups = [group for group in balance(QUICK_TESTS, jobs) if group]
        launches = [[sys.executable, str(script), *options, *group] for group in groups]
    elif jobs > 1:
        launches = [[sys.executable, str(script), *options] for index in range(1, jobs + 1)]
    else:
        result = subprocess.run([sys.executable, str(script), *options], cwd=WORK.parent)
        if result.returncode:
            raise SystemExit(result.returncode)
        return
    total = len(launches)
    if total > 1:
        clear_shards(total)
    environment = dict(os.environ)
    running, threads = [], []
    for number, argv in enumerate(launches, 1):
        if total > 1:
            environment[SHARD_ENV] = f'{number}/{total}'
        log = WORK / 'analysis' / f'test-workflows-{number}.log'
        sink = log.open('w')
        process = subprocess.Popen(argv, cwd=WORK.parent, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1,
                                   env=environment)
        running.append((number, process, sink, log))
        thread = threading.Thread(target=_pump, args=(process, sink, number, total), daemon=True)
        thread.start()
        threads.append(thread)
    failures = [number for number, process, _, _ in running if process.wait()]
    for thread in threads:
        thread.join()
    for _, _, sink, _ in running:
        sink.close()
    if total > 1:
        merge_shards(total)
    if failures:
        for number, _, _, log in running:
            if number in failures:
                print(f'--- shard {number}/{total} log: {log} ---', file=sys.stderr)
        raise SystemExit(f'Workflow shards failed: {failures}')


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Read installation and active candidate metadata without hashing or restoring')
    commands.add_parser('doctor', help='Check the Python environment, candidate availability, installed and original hashes')
    tests = commands.add_parser('test', help='Run CLI checks and frozen-resource regressions; no installation')
    tests.add_argument('--pack', action='store_true', help='Also replay a full encrypted disc')
    tests.add_argument('--cli-only', action='store_true', help='Only run fast entry-point and safety checks')
    tests.add_argument('--quick', action='store_true', help='Run a representative workflow subset for a fast inner loop')
    tests.add_argument('--jobs', type=int, help=f'Parallel workflow shards (default {DEFAULT_JOBS}; 1 runs serially)')
    tests.add_argument('--serial', action='store_true', help='Run the workflow regressions in a single process')
    commands.add_parser('review', help='Rebuild the shared Chinese/Korean/English review page')
    install = commands.add_parser('install', help='Install a requested candidate, or preflight with --check-only')
    source = install.add_mutually_exclusive_group(required=True)
    source.add_argument('--stage', help='Explicit release directory under staging')
    source.add_argument('--language', choices=['zh-CN', 'ko-KR', 'en-US'], help='Select the active candidate from storage policy')
    install.add_argument('--check-only', action='store_true', help='Check hashes without installing or creating backups')
    install.add_argument('--game-is-closed', action='store_true', help='Acknowledge the Windows game is closed before writing files')
    restore = commands.add_parser('restore-original', help='Restore original game files from the protected backup')
    restore.add_argument('--check-only', action='store_true', help='Check restore readiness without writing game files')
    restore.add_argument('--game-is-closed', action='store_true', help='Acknowledge the Windows game is closed before writing files')
    patch = commands.add_parser('patch', help='Apply a recipe to a frozen base candidate')
    patch.add_argument('--base', required=True)
    patch.add_argument('--output', required=True)
    patch.add_argument('--language', required=True, choices=['zh-CN','ko-KR','en-US'])
    patch.add_argument('--recipe', required=True, help='Recipe name or JSON path under work')
    patch.add_argument('--version')
    patch.add_argument('--resources-only', action='store_true')
    patch.add_argument('--no-preview', action='store_true')
    build = commands.add_parser('build', help='Rebuild full text or selected native UI resources')
    build.add_argument('kind', choices=['full','ui','equipment','titles'])
    build.add_argument('--base', required=True)
    build.add_argument('--output', required=True)
    build.add_argument('--language', required=True, choices=['zh-CN','ko-KR','en-US'])
    build.add_argument('--resources-only', action='store_true')
    verify = commands.add_parser('verify', help='Verify packaged files and native resource hashes')
    verify.add_argument('--stage', required=True)
    assets = commands.add_parser('assets', help='Manage generated image previews')
    assets.add_argument('operation', choices=['audit','cleanup','gallery','verify'])
    assets.add_argument('--apply', action='store_true', help='Apply configured cleanup; default is an inventory')
    merge = commands.add_parser('merge', help='Validate the shared corpus or merge an ID-keyed language draft')
    merge.add_argument('--language', choices=['zh-CN','ko-KR','en-US'], default='zh-CN')
    merge.add_argument('--table', action='append', choices=['event','events','catalog','description','host','unity','condition'],
                       help='Limit a draft merge to one or more corpus kinds')
    merge.add_argument('--draft', help='AI JSON output under work with translations keyed by stable ID')
    merge.add_argument('--overwrite', action='store_true', help='Allow intentional replacement of existing translations')
    clean=commands.add_parser('clean',help='Verify releases and remove rebuildable packing intermediates')
    clean.add_argument('--apply',action='store_true')
    storage=commands.add_parser('storage',help='Compact, bundle, restore, or verify historical versions')
    storage.add_argument('operation',choices=['compact','bundle','restore','verify'])
    storage.add_argument('--apply',action='store_true',help='Apply compact or create audited bundles')
    storage.add_argument('--snapshot',help='Registered snapshot path or ID for restore/verify')
    titles = commands.add_parser('extract-titles', help='Export one source title contact sheet')
    titles.add_argument('--individual', action='store_true')
    return root


def main():
    cli = parser(); args = cli.parse_args()
    if args.command in {'status', 'doctor'}:
        from workspace_status import status, doctor
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
        if args.quick and args.pack:
            cli.error('--quick cannot be combined with --pack')
        jobs = 1 if args.serial else (DEFAULT_JOBS if args.jobs is None else args.jobs)
        if jobs < 1:
            cli.error('--jobs must be at least 1')
        scripts = [REPO/'tests/test_cli.py', REPO/'tests/test_storage_bundles.py',
                   REPO/'tests/test_translation_merge.py', REPO/'tests/test_resources.py',
                   REPO/'tests/test_chinese_rebuild.py']
        if not args.cli_only:
            scripts.append(REPO/'tests/test_workflows.py')
        for script in scripts:
            started = time.monotonic()
            if script.name == 'test_workflows.py' and not args.cli_only:
                _run_workflows(script, pack=args.pack, quick=args.quick, jobs=jobs)
            else:
                result = subprocess.run([sys.executable, str(script)], cwd=WORK.parent)
                if result.returncode:
                    raise SystemExit(result.returncode)
            print(f'{script.name} finished in {time.monotonic()-started:.1f}s', file=sys.stderr)
        return
    if args.command == 'review':
        import runpy
        runpy.run_path(str(REPO/'tools/build_review.py'), run_name='__main__')
        return
    if args.command == 'install':
        from workspace_status import active_stage
        stage = active_stage(args.language).name if args.language else args.stage
        options = ['--stage', stage]
        if args.check_only:
            options.append('--check-only')
        if args.game_is_closed:
            options.append('--game-is-closed')
        invoke('install_alpha', options)
        return
    if args.command == 'restore-original':
        options = []
        if args.check_only:
            options.append('--check-only')
        if args.game_is_closed:
            options.append('--game-is-closed')
        invoke('restore_original', options)
        return
    # Load resource/font modules only for commands that use them, keeping help
    # and diagnostics available even if a project dependency needs repair.
    from candidate import stage_path, verify_candidate, prune_intermediates
    from native_patches import apply_recipe
    from workspace_assets import cleanup_images, export_gallery, rewrite_image_links, verify_previews
    from version_storage import activate_candidate, compact
    if args.command == 'patch':
        result = apply_recipe(args.base,args.output,args.language,args.recipe,args.version,
                              args.resources_only,not args.no_preview)
        rewrite_image_links(); export_gallery()
        if not args.resources_only:
            activate_candidate(result.stage,args.language)
            compact(apply=True)
        print(json.dumps({'output':str(result.stage),'status':result.report['build_status'],
                          'translated_texts':result.report['translated_texts']},ensure_ascii=False))
    elif args.command == 'build':
        output = stage_path(args.output)
        if output.exists():
            cli.error('Use a new output directory; existing releases are not overwritten')
        language = args.language
        if args.kind == 'full':
            # build_full_languages accepts the canonical locale names and
            # reconstructs the Chinese release from the shared corpus too.
            pass
        elif args.kind == 'equipment' and language == 'zh-CN':
            cli.error('Equipment atlas translation is Korean/English; Chinese labels retain their source tiles')
        modules={'full':'build_full_languages','ui':'build_ui_labels','equipment':'build_equipment_labels',
                 'titles':'build_title_images'}
        options=['--base',str(stage_path(args.base)),'--output',str(output),'--language',language]
        if args.resources_only: options.append('--resources-only')
        invoke(modules[args.kind],options)
        rewrite_image_links(); export_gallery()
        if not args.resources_only:
            activate_candidate(output,args.language)
            compact(apply=True)
    elif args.command == 'verify':
        report=verify_candidate(args.stage)
        print(json.dumps({'version':report.get('version',args.stage),'verified_files':len(report['files']),
                          'verified_resources':len(report.get('changed_resources',[]))},ensure_ascii=False))
    elif args.command == 'merge':
        if args.overwrite and not args.draft:
            cli.error('--overwrite requires --draft')
        options=[]
        if args.draft:
            options.extend(['--language',args.language,'--draft',args.draft])
            aliases={'events':'event'}
            for kind in args.table or []:
                options.extend(['--table',aliases.get(kind,kind)])
            if args.overwrite:options.append('--overwrite')
        elif args.table:
            cli.error('--table requires --draft')
        invoke('merge_full_translations',options)
    elif args.command == 'clean':
        result=prune_intermediates(args.apply)
        (WORK/'maintenance').mkdir(exist_ok=True)
        (WORK/'maintenance/intermediate_cleanup.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({'applied':result['applied'],'intermediates':len(result['files']),
                          'bytes_reclaimed':result['bytes_reclaimed']},ensure_ascii=False))
    elif args.command == 'storage':
        if args.operation not in {'compact','bundle'} and args.apply:
            cli.error('--apply only applies to storage compact or bundle')
        if args.operation=='restore' and not args.snapshot:
            cli.error('storage restore requires --snapshot')
        if args.operation=='compact':
            if args.snapshot:cli.error('compact uses the retention policy, not --snapshot')
            result=compact(args.apply)
        elif args.operation=='bundle':
            if args.snapshot:cli.error('bundle operates on the configured cold snapshot set, not --snapshot')
            # version_storage.bundle compacts first when applying.
            from version_storage import bundle
            result=bundle(args.apply)
        elif args.operation=='restore':
            from version_storage import materialize
            result=materialize(args.snapshot)
        else:
            from version_storage import verify_all
            result=verify_all(args.snapshot)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command == 'extract-titles':
        invoke('title_sprites',['--individual'] if args.individual else [])
        rewrite_image_links(); export_gallery()
    elif args.operation in {'audit','cleanup'}:
        result=cleanup_images(args.operation=='cleanup' and args.apply)
        (WORK/'maintenance').mkdir(exist_ok=True)
        report_path=WORK/'maintenance'/('image_cleanup.json' if result['applied'] else 'image_inventory.json')
        report_path.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k not in {'files','updated_documents'}},ensure_ascii=False))
    elif args.operation == 'gallery':
        rewrite_image_links(); export_gallery(); print(WORK/'assets/index.html')
    elif args.operation == 'verify':
        print(json.dumps(verify_previews(),ensure_ascii=False))


if __name__ == '__main__': main()
