"""Recipe gates that need no game files.

The full end-to-end check installs all three languages into a copy of the real
disc image, which means it can only run where the game files live. These gates
cover the part that a clean checkout can prove on its own, so CI runs them on
every push:

1. The compiled plan carries the right magic, game, version and languages.
2. The plan's file identities match ``recipe/manifest.json``,
   ``recipe/exe.json`` and ``recipe/texture.json``.
3. Every manifest resource, story page and expected hash reaches the plan, and
   the three blank story slots stay out of it.
4. The compiler rejects a broken recipe: missing font, overflowing text, a
   texture language that has no layer, a malformed executable patch, a bad card
   tilemap or glyph, a script word outside its resource, and a nameplate that
   cannot fit its canvas.
5. The public repository carries no FS2 payload ZIP, and ``patcher/plan`` holds
   nothing that would be embedded by accident.

``fs2/work/tools/recipe_e2e.py`` imports this module for the same checks, so a
rule only lives in one place.

Run after compiling the plan:

    bash uv.sh run --locked python tools/recipe_checks.py

Exit code 0 means every gate passed; the JSON summary goes to stdout.
"""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

MAGIC = b'FS2RECIPE1\n'
PLAN_FORMAT = 2
GAME = 'saga2'
BLANK_STORY_PAGES = 3
DEFAULT_REPO = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_plan(path):
    """Split the compiled plan into its JSON header and decompressed blob."""
    raw = Path(path).read_bytes()
    if raw[:len(MAGIC)] != MAGIC:
        raise SystemExit(f'{path}: plan has no FS2RECIPE1 marker')
    length = struct.unpack_from('<I', raw, len(MAGIC))[0]
    start = len(MAGIC) + 4
    if start + length > len(raw):
        raise SystemExit(f'{path}: header length {length} leaves the plan')
    header = json.loads(raw[start:start + length])
    blob = gzip.decompress(raw[start + length:])
    if digest(blob) != header.get('binary_sha256'):
        raise SystemExit(f'{path}: plan blob hash does not match its header')
    return header, blob


def recipe_identity(repo):
    manifest = read_json(repo / 'recipe/manifest.json')
    executable = read_json(repo / 'recipe/exe.json')
    texture = read_json(repo / 'recipe/texture.json')
    pages = read_json(repo / 'recipe/story_pages.json')['pages']
    return manifest, executable, texture, pages


def validate_plan(header, repo):
    """Compare a compiled plan against the committed recipe.

    Raises SystemExit listing every difference: a plan that disagrees with the
    recipe would install something the repository never reviewed.
    """
    manifest, executable, texture, pages = recipe_identity(repo)
    problems = []
    if header.get('format') != PLAN_FORMAT:
        problems.append(f'plan format = {header.get("format")!r}')
    if header.get('game') != GAME:
        problems.append(f'plan game = {header.get("game")!r}')
    if header.get('version') != manifest['version']:
        problems.append(f'plan version = {header.get("version")!r}')
    if header.get('languages') != manifest['languages']:
        problems.append(f'plan languages = {header.get("languages")!r}')
    if header.get('archive_lba') != manifest['disc']['archive_lba']:
        problems.append('plan archive LBA differs from manifest')
    if header.get('sector_size') != manifest['disc']['sector_size']:
        problems.append('plan sector size differs from manifest')

    files = header.get('files') or {}
    expected_files = {
        manifest['disc']['image']: ('disc', manifest['disc']['image_sha256'],
                                    manifest['disc']['image_size']),
        executable['path']: ('binary', executable['original_sha256'],
                             executable['original_size']),
        texture['path']: ('texture', texture['original_sha256'], texture['size']),
    }
    if set(files) != set(expected_files):
        problems.append(f'plan files {sorted(files)} != {sorted(expected_files)}')
    for name, (kind, sha, size) in expected_files.items():
        item = files.get(name)
        if not item:
            continue
        if item.get('kind') != kind or item.get('path') != name:
            problems.append(f'{name}: kind or path differs')
        if item.get('original_sha256') != sha or item.get('size') != size:
            problems.append(f'{name}: original identity differs')

    binary = files.get(executable['path']) or {}
    if binary.get('patched_sha256') != executable['patched_sha256']:
        problems.append('executable patched hash differs')
    if binary.get('patched_size') != executable['patched_size']:
        problems.append('executable patched size differs')
    if binary.get('patches') != [
            {'name': patch['name'], 'offset': patch['offset'],
             'original': patch['original'].lower(), 'patched': patch['patched'].lower()}
            for patch in executable['patches']]:
        problems.append('executable patch list differs')

    texture_plan = files.get(texture['path']) or {}
    for key in ('image_size', 'part_count', 'parts'):
        if texture_plan.get(key) != texture[key]:
            problems.append(f'texture {key} differs from recipe')
    expected_panels = {key: texture['panels'][key]
                       for key in ('parts', 'title_box', 'body_box')}
    if texture_plan.get('panels') != expected_panels:
        problems.append('texture panels differ from recipe')
    if len(texture_plan.get('languages') or {}) != len(manifest['languages']):
        problems.append('texture language layers are incomplete')

    resources = header.get('resources') or {}
    expected_count = len(manifest['resources']) + len(pages) - BLANK_STORY_PAGES
    if len(resources) != expected_count:
        problems.append(f'plan has {len(resources)} resources, expected {expected_count}')
    for rid in manifest['resources']:
        if rid not in resources:
            problems.append(f'{rid}: missing from plan')
    blank = []
    for page in pages:
        rid = f'P{page["id"]}'
        resource = resources.get(rid)
        if not resource:
            blank.append(rid)
            continue
        if resource.get('archive_offset') != page['archive_offset']:
            problems.append(f'{rid}: archive offset differs from story index')
        if resource.get('size') != page['size']:
            problems.append(f'{rid}: size differs from story index')
        if resource.get('original_sha256') != page['original_sha256']:
            problems.append(f'{rid}: original hash differs from story index')
    if len(blank) != BLANK_STORY_PAGES:
        problems.append(
            f'story index has {len(blank)} blank pages in the plan, '
            f'expected {BLANK_STORY_PAGES}')
    for language in manifest['languages']:
        expected = read_json(repo / f'recipe/expected/{language}.json')['resources']
        if set(expected) != set(resources):
            problems.append(f'{language}: expected keys differ from plan')
            continue
        for rid, value in expected.items():
            if resources[rid]['expected'].get(language) != value:
                problems.append(f'{language}/{rid}: expected hash differs from plan')
    if problems:
        raise SystemExit('plan does not match the recipe:\n' + '\n'.join(problems[:30]))
    return {'resources': len(resources), 'pages': len(pages),
            'blank_pages': len(blank), 'files': sorted(files)}


def check_hygiene(repo):
    """The public tree must not carry game data or stray embedded files."""
    problems = []
    payload = repo / 'patcher/payload/saga2'
    if payload.is_dir():
        zips = sorted(path.name for path in payload.glob('*.zip'))
        if zips:
            problems.append(f'patcher/payload/saga2 still has ZIPs: {zips}')
    plan_dir = repo / 'patcher/plan'
    if plan_dir.is_dir():
        extras = sorted(path.name for path in plan_dir.iterdir()
                        if path.name not in ('plan.bin', '.gitkeep'))
        if extras:
            problems.append(f'patcher/plan has files that would be embedded: {extras}')
    if problems:
        raise SystemExit('\n'.join(problems))
    return {'payload_zips': [], 'plan_extras': []}


def _rewrite(path, change):
    data = read_json(path)
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')


def _set_long_text(root):
    def change(data):
        for label in data['labels']:
            if label['name'].startswith('menu-action-'):
                label['text']['zh-CN'] = '这是一段绝对不可能放进菜单按钮里的超长文字' * 2
                break
    _rewrite(root / 'resources/02C9.json', change)


def _texture_missing_language(root):
    _rewrite(root / 'texture.json', lambda data: data['labels'][0]['text'].pop('en-US'))


def _texture_overflow(root):
    def change(data):
        label = data['labels'][0]
        label['min_size'] = 60
        label['max_size'] = {'zh-CN': 60, 'ko-KR': 60, 'en-US': 60}
    _rewrite(root / 'texture.json', change)


def _exe_bad_patch(root):
    _rewrite(root / 'exe.json', lambda data: data['patches'][0].update(patched='eb'))


def _exe_bad_format(root):
    _rewrite(root / 'exe.json', lambda data: data.update(format='unknown'))


def _card_bad_tilemap(root):
    _rewrite(root / 'resources/cards/00E2.json',
             lambda data: data['tilemap'].update(map_offset=data['tilemap']['map_offset'] + 2))


def _card_missing_glyph(root):
    _rewrite(root / 'resources/cards/00E2.json',
             lambda data: data['title'].update({'zh-CN': '\U0001F600'}))


def _missing_font(root):
    roles = ('NotoSansSC', 'Galmuri11', 'LiberationSans', 'Unifont',
             'NotoSansSCAtlas', 'Galmuri11Atlas')
    (root / 'fonts.json').write_text(
        json.dumps({'fonts': {role: '/nonexistent/font.ttf' for role in roles}}),
        encoding='utf-8')


def _word_bad_offset(root):
    _rewrite(root / 'resources/0117.json',
             lambda data: data['patches'][0].update(offset=8189))


def _word_duplicate_offset(root):
    def change(data):
        data['patches'][1]['offset'] = data['patches'][0]['offset']
    _rewrite(root / 'resources/0117.json', change)


def _plate_overflow(root):
    def change(data):
        # Latin letters exist in every language font, so this fails on the
        # canvas, not on glyph coverage.
        for language in data['text']:
            data['text'][language] = 'Overflowing name ' * 4
    _rewrite(root / 'resources/1044.json', change)


def _plate_canvas_overflow(root):
    _rewrite(root / 'resources/1044.json',
             lambda data: data['canvas'].update(height=64))


def _plate_duplicate_ink(root):
    def change(data):
        data['canvas']['shadow'] = data['canvas']['body']
    _rewrite(root / 'resources/1044.json', change)


def _plate_missing_glyph(root):
    _rewrite(root / 'resources/1044.json',
             lambda data: data['text'].update({'ko-KR': '漢字'}))


COMPILER_CASES = (
    ('missing-font', _missing_font),
    ('text-overflow', _set_long_text),
    ('texture-missing-language', _texture_missing_language),
    ('texture-overflow', _texture_overflow),
    ('exe-bad-patch', _exe_bad_patch),
    ('exe-bad-format', _exe_bad_format),
    ('card-bad-tilemap', _card_bad_tilemap),
    ('card-missing-glyph', _card_missing_glyph),
    ('word-bad-offset', _word_bad_offset),
    ('word-duplicate-offset', _word_duplicate_offset),
    ('plate-overflow', _plate_overflow),
    ('plate-canvas-overflow', _plate_canvas_overflow),
    ('plate-duplicate-ink', _plate_duplicate_ink),
    ('plate-missing-glyph', _plate_missing_glyph),
)


def _run_compiler_case(repo, sandbox, name, mutate):
    """Compile one deliberately broken recipe; it must be refused."""
    target = sandbox / f'recipe-{name}'
    shutil.copytree(repo / 'recipe', target)
    mutate(target)
    result = subprocess.run(
        [sys.executable, str(repo / 'tools/compile_recipe.py'),
         '--recipe-dir', str(target),
         '--output', str(sandbox / f'{name}.bin'),
         '--report', str(sandbox / f'{name}-report.json')],
        cwd=str(repo), capture_output=True, text=True)
    message = (result.stderr or result.stdout).strip()
    message = message.splitlines()[-1] if message else '(no message)'
    return {'name': f'compiler-{name}', 'passed': result.returncode != 0,
            'message': message}


def test_compiler(repo, sandbox=None, jobs=None):
    """Every malformed recipe must fail the compile with a message."""
    repo = Path(repo)
    own = sandbox is None
    if own:
        sandbox = Path(tempfile.mkdtemp(prefix='fs2-recipe-checks-'))
    sandbox = Path(sandbox)
    jobs = jobs or min(len(COMPILER_CASES), os.cpu_count() or 4)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(_run_compiler_case, repo, sandbox, name, mutate)
                       for name, mutate in COMPILER_CASES]
            results = [future.result() for future in futures]
    finally:
        if own:
            shutil.rmtree(sandbox, ignore_errors=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=DEFAULT_REPO,
                        help='repository root (default: beside this script)')
    parser.add_argument('--plan', type=Path,
                        help='compiled plan (default: patcher/plan/plan.bin)')
    parser.add_argument('--skip-compiler', action='store_true',
                        help='only check the compiled plan and tree hygiene')
    parser.add_argument('--jobs', type=int,
                        help='parallel compiler cases (default: CPU count)')
    args = parser.parse_args()
    repo = args.repo.resolve()
    plan = args.plan or repo / 'patcher/plan/plan.bin'
    if not plan.is_file():
        raise SystemExit(f'{plan} is missing; compile the recipe first')

    failures = []
    header, blob = load_plan(plan)
    summary = {'plan': {'path': str(plan), 'version': header.get('version'),
                        'bytes': plan.stat().st_size, 'blob_bytes': len(blob),
                        'sha256': digest(plan.read_bytes())}}
    try:
        summary['recipe'] = validate_plan(header, repo)
    except SystemExit as error:
        failures.append(str(error))
    try:
        summary['hygiene'] = check_hygiene(repo)
    except SystemExit as error:
        failures.append(str(error))

    if not args.skip_compiler:
        results = test_compiler(repo, jobs=args.jobs)
        summary['compiler'] = results
        for result in results:
            if not result['passed']:
                failures.append(f"{result['name']}: {result['message']}")

    summary['failures'] = failures
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
