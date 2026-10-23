"""Compile the FS2 recipe into the binary plan embedded in the patcher.

The recipe is the human-readable source of truth: label boxes, styles, text,
font roles and expected resource hashes.  This compiler turns the text into
positioned masks.  It never reads the game, so a clean checkout can build the
plan; the patcher composites the masks onto the user's own original resources.

Plan layout (little-endian):

    b"FS2RECIPE1\\n"
    u32 header length
    UTF-8 JSON header (sorted keys, compact separators)
    gzip member holding the concatenated masks

Each mask is one byte per pixel: 0 keeps the erased row background, 1 is the
outline colour, 2 is the body colour.  Offsets in the header are relative to
the decompressed mask blob.
"""
import argparse
import gzip
import hashlib
import json
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

MAGIC = b'FS2RECIPE1\n'
PLAN_FORMAT = 1
REPO = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = REPO / 'recipe'
DEFAULT_OUTPUT = REPO / 'patcher' / 'plan' / 'plan.bin'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise SystemExit(f'missing recipe file: {path}') from None
    except json.JSONDecodeError as error:
        raise SystemExit(f'invalid JSON in {path}: {error}') from None


def font_map(recipe_dir):
    """Resolve font roles; fonts.local.json wins over the public map."""
    mapping = {}
    for name in ('fonts.json', 'fonts.local.json'):
        path = recipe_dir / name
        if not path.is_file():
            continue
        for role, value in read_json(path).get('fonts', {}).items():
            if isinstance(value, str):
                candidate = Path(value)
                mapping[role] = candidate if candidate.is_absolute() else (recipe_dir / candidate).resolve()
            elif isinstance(value, dict) and value.get('file'):
                mapping[role] = (recipe_dir / value['file']).resolve()
            else:
                raise SystemExit(f'{path}: font {role!r} needs a path or a file entry')
    return mapping


def fit(path, text, width, height, largest, smallest=7):
    """Largest point size whose ink fits the box, matching the Pillow pipeline."""
    if not path.is_file():
        raise SystemExit(f'font file not found: {path}')
    for size in range(largest, smallest - 1, -1):
        try:
            font = ImageFont.truetype(str(path), size)
        except OSError as error:
            raise SystemExit(f'cannot load font {path}: {error}') from None
        bbox = font.getbbox(text)
        ink_width, ink_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if ink_width <= width and ink_height <= height:
            return size, font, bbox, ink_width, ink_height
    raise SystemExit(f'{text!r} does not fit {width}x{height} (font {path.name}, '
                     f'sizes {smallest}..{largest})')


def build_mask(label, text, path, threshold, style):
    """Return the label's mask and chosen font size.

    The drawing order matches the private pipeline: erase the row background,
    dilate the thresholded body by one pixel for the outline, then paint the
    body over the outline.  The mask records which of the two colours wins.
    """
    x0, y0, width, height = label['box']
    size, font, bbox, ink_width, ink_height = fit(
        path, text, width - 2, height - 2, label['max_size'])
    body = Image.new('L', (ink_width + 2, ink_height + 2), 0)
    ImageDraw.Draw(body).text((1 - bbox[0], 1 - bbox[1]), text, font=font, fill=255)
    if threshold > 1:
        body = body.point(lambda value: 255 if value >= threshold else 0)
    if style['outline'] == style['body']:
        outline = Image.new('L', body.size, 0)
    else:
        outline = body.filter(ImageFilter.MaxFilter(3))
    left = x0 if label['align'] == 'left' else x0 + (width - ink_width) // 2 - 1
    top = y0 + (height - ink_height) // 2 - 1
    mask = bytearray(width * height)
    for yy in range(ink_height + 2):
        gy = top + yy
        if not y0 <= gy < y0 + height:
            continue
        for xx in range(ink_width + 2):
            gx = left + xx
            if not x0 <= gx < x0 + width:
                continue
            if body.getpixel((xx, yy)):
                mask[(gy - y0) * width + (gx - x0)] = 2
            elif outline.getpixel((xx, yy)):
                mask[(gy - y0) * width + (gx - x0)] = 1
    if not any(mask):
        raise SystemExit(f'{label["name"]}: mask is empty for {text!r}')
    return bytes(mask), size


def validate_manifest(manifest, recipe_dir):
    if manifest.get('format') != PLAN_FORMAT:
        raise SystemExit(f'unsupported recipe format {manifest.get("format")!r}')
    if manifest.get('kind') != 'farland-saga-recipe':
        raise SystemExit('manifest is not a farland-saga-recipe')
    if not manifest.get('version') or not manifest.get('languages'):
        raise SystemExit('manifest needs a version and a language list')
    if not manifest.get('game'):
        raise SystemExit('manifest needs a game id')
    if len(set(manifest['languages'])) != len(manifest['languages']):
        raise SystemExit('manifest repeats a language')
    disc = manifest.get('disc')
    if not isinstance(disc, dict):
        raise SystemExit('manifest needs a disc block')
    if not isinstance(disc.get('archive_lba'), int) or disc['archive_lba'] < 0:
        raise SystemExit('disc.archive_lba must be a non-negative integer')
    if not isinstance(disc.get('sector_size'), int) or disc['sector_size'] <= 0:
        raise SystemExit('disc.sector_size must be a positive integer')
    resources = manifest.get('resources')
    if not isinstance(resources, dict) or not resources:
        raise SystemExit('manifest needs at least one resource')
    for rid, meta in resources.items():
        if not isinstance(meta, dict):
            raise SystemExit(f'{rid}: resource entry must be an object')
        size = meta.get('size')
        offset = meta.get('archive_offset')
        if not isinstance(size, int) or size <= 0 or size % disc['sector_size']:
            raise SystemExit(f'{rid}: size must be a positive multiple of disc.sector_size')
        if not isinstance(offset, int) or offset < 0 or offset % disc['sector_size']:
            raise SystemExit(f'{rid}: archive_offset must be non-negative and sector aligned')
        if len(meta.get('original_sha256', '')) != 64:
            raise SystemExit(f'{rid}: original_sha256 is missing')
        recipe = meta.get('recipe')
        if not isinstance(recipe, str) or not recipe:
            raise SystemExit(f'{rid}: recipe path is missing')
        if not (recipe_dir / recipe).is_file():
            raise SystemExit(f'{rid}: recipe file {recipe} is missing')


def validate_recipe(rid, recipe, languages):
    if recipe.get('format') != 'tile_atlas_8bpp':
        raise SystemExit(f'{rid}: unsupported recipe format {recipe.get("format")!r}')
    atlas_width, atlas_height = recipe.get('atlas_size', (0, 0))
    if atlas_width <= 0 or atlas_height <= 0:
        raise SystemExit(f'{rid}: atlas_size is missing')
    styles = recipe.get('styles', {})
    strings = recipe.get('strings', {})
    fonts = recipe.get('fonts', {})
    thresholds = recipe.get('thresholds', {})
    seen = set()
    for label in recipe.get('labels', []):
        name = label.get('name')
        if not name or name in seen:
            raise SystemExit(f'{rid}: label names must be unique')
        seen.add(name)
        box = label.get('box', [])
        if len(box) != 4 or any(not isinstance(value, int) for value in box):
            raise SystemExit(f'{rid}/{name}: box must be four integers')
        x0, y0, width, height = box
        if x0 < 0 or y0 < 0 or width <= 2 or height <= 2:
            raise SystemExit(f'{rid}/{name}: invalid box {box}')
        if x0 + width > atlas_width or y0 + height > atlas_height:
            raise SystemExit(f'{rid}/{name}: box {box} leaves the {atlas_width}x{atlas_height} atlas')
        if label.get('style') not in styles:
            raise SystemExit(f'{rid}/{name}: unknown style {label.get("style")!r}')
        style = styles[label['style']]
        for key in ('body', 'outline'):
            if not isinstance(style.get(key), int) or not 0 <= style[key] <= 255:
                raise SystemExit(f'{rid}/{name}: style {key} must be a byte')
        if label.get('align') not in ('left', 'center'):
            raise SystemExit(f'{rid}/{name}: align must be left or center')
        if not isinstance(label.get('max_size'), int) or label['max_size'] < 7:
            raise SystemExit(f'{rid}/{name}: max_size must be at least 7')
        if label.get('text') not in strings:
            raise SystemExit(f'{rid}/{name}: unknown string {label.get("text")!r}')
        for language in languages:
            if not strings[label['text']].get(language):
                raise SystemExit(f'{rid}/{name}: missing {language} text for {label["text"]!r}')
            if language not in fonts:
                raise SystemExit(f'{rid}: no font for {language}')
            threshold = thresholds.get(language)
            if not isinstance(threshold, int) or not 1 <= threshold <= 255:
                raise SystemExit(f'{rid}: threshold for {language} must be 1..255')


def compile_plan(recipe_dir, output, report_path, preview_dir):
    manifest = read_json(recipe_dir / 'manifest.json')
    validate_manifest(manifest, recipe_dir)
    languages = manifest['languages']
    fonts = font_map(recipe_dir)
    blob = bytearray()
    header = {
        'format': PLAN_FORMAT,
        'version': manifest['version'],
        'game': manifest['game'],
        'languages': languages,
        'archive_lba': manifest['disc']['archive_lba'],
        'sector_size': manifest['disc']['sector_size'],
        'resources': {},
    }
    report = {
        'version': manifest['version'],
        'languages': languages,
        'resources': {},
    }
    for rid, meta in manifest['resources'].items():
        recipe = read_json(recipe_dir / meta['recipe'])
        validate_recipe(rid, recipe, languages)
        expected = {}
        for language in languages:
            values = read_json(recipe_dir / 'expected' / f'{language}.json')
            value = values.get('resources', {}).get(rid)
            if not value or len(value) != 64:
                raise SystemExit(f'expected/{language}.json: no hash for {rid}')
            expected[language] = value
        operations = {}
        resource_report = {'format': recipe['format'], 'labels': {}}
        for language in languages:
            labels = []
            preview = None
            if preview_dir:
                preview = Image.new('L', tuple(recipe['atlas_size']), 128)
                draw = ImageDraw.Draw(preview)
            for label in recipe['labels']:
                text = recipe['strings'][label['text']][language]
                style = recipe['styles'][label['style']]
                mask, size = build_mask(
                    label, text, fonts[recipe['fonts'][language]],
                    recipe['thresholds'][language], style)
                labels.append({
                    'name': label['name'],
                    'box': label['box'],
                    'erase': 'row_background',
                    'mask_offset': len(blob),
                    'mask_length': len(mask),
                    'body': style['body'],
                    'outline': style['outline'],
                })
                blob.extend(mask)
                if preview is not None:
                    x0, y0, width, height = label['box']
                    for y in range(height):
                        for x in range(width):
                            value = mask[y * width + x]
                            if value:
                                preview.putpixel((x0 + x, y0 + y), 255 if value == 2 else 0)
                resource_report['labels'].setdefault(language, {})[label['name']] = {
                    'text': text,
                    'font': recipe['fonts'][language],
                    'font_size': size,
                    'box': label['box'],
                    'ink_pixels': sum(1 for value in mask if value),
                }
            operations[language] = [{'type': 'tile_atlas', 'labels': labels}]
            if preview is not None:
                preview_dir.mkdir(parents=True, exist_ok=True)
                preview.resize((preview.width * 2, preview.height * 2),
                               Image.Resampling.NEAREST).save(
                    preview_dir / f'{language}-{rid}-mask.png')
        header['resources'][rid] = {
            'format': recipe['format'],
            'atlas_size': recipe['atlas_size'],
            'archive_offset': meta['archive_offset'],
            'size': meta['size'],
            'original_sha256': meta['original_sha256'],
            'expected': expected,
            'operations': operations,
        }
        report['resources'][rid] = resource_report

    header['binary_sha256'] = digest(blob)
    header_bytes = json.dumps(header, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')).encode('utf-8')
    compressed = gzip.compress(bytes(blob), compresslevel=9, mtime=0)
    plan = MAGIC + struct.pack('<I', len(header_bytes)) + header_bytes + compressed
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.tmp')
    temporary.write_bytes(plan)
    temporary.replace(output)
    report.update({
        'plan': str(output),
        'plan_bytes': len(plan),
        'plan_sha256': digest(plan),
        'binary_bytes': len(blob),
        'binary_sha256': header['binary_sha256'],
    })
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
    print(json.dumps({
        'plan': str(output),
        'plan_sha256': report['plan_sha256'],
        'plan_bytes': len(plan),
        'languages': languages,
        'resources': list(header['resources']),
    }, ensure_ascii=False, sort_keys=True))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe-dir', type=Path, default=DEFAULT_RECIPE,
                        help='directory holding manifest.json (default: recipe/)')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                        help='plan path (default: patcher/plan/plan.bin)')
    parser.add_argument('--report', type=Path,
                        default=REPO / 'patcher' / 'plan' / 'report.json',
                        help='JSON build report; empty string disables it')
    parser.add_argument('--preview-dir', type=Path,
                        help='optional directory for per-language mask previews')
    args = parser.parse_args()
    report = None if str(args.report) in ('', '.') else args.report
    compile_plan(args.recipe_dir, args.output, report, args.preview_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
