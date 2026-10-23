"""Compile the FS2 recipe into the binary plan embedded in the patcher.

The recipe is the human-readable source of truth: resource layout, label boxes,
styles, text, font roles and expected resource hashes.  This compiler turns the
text into positioned masks and, for 1bpp resources, packed slot bitmaps.  It
never reads the game, so a clean checkout can build the plan; the patcher
composites the result onto the user's own original resources.

Plan layout (little-endian):

    b"FS2RECIPE1\\n"
    u32 header length
    UTF-8 JSON header (sorted keys, compact separators)
    gzip member holding the concatenated mask/bitmap blob

Tile-atlas masks are one byte per pixel: 0 keeps the erased background, 1 is
the outline/shadow colour, 2 is the body colour.  Mono writes are already
packed 1bpp rows, one byte per eight pixels, MSB first.
"""
import argparse
import gzip
import hashlib
import json
import struct
import sys
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

MAGIC = b'FS2RECIPE1\n'
PLAN_FORMAT = 1
REPO = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = REPO / 'recipe'
DEFAULT_OUTPUT = REPO / 'patcher' / 'plan' / 'plan.bin'
TILE_FORMAT = 'tile_atlas_8bpp'
MONO_FORMAT = 'mono_bitmap_1bpp'


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
    """Resolve the public font roles to committed files."""
    path = recipe_dir / 'fonts.json'
    mapping = {}
    if not path.is_file():
        raise SystemExit(f'missing font map: {path}')
    data = read_json(path).get('fonts')
    if not isinstance(data, dict) or not data:
        raise SystemExit(f'{path}: fonts must be a non-empty object')
    for role, value in data.items():
        if isinstance(value, str):
            candidate = Path(value)
            mapping[role] = candidate if candidate.is_absolute() else (recipe_dir / candidate).resolve()
        elif isinstance(value, dict) and value.get('file'):
            mapping[role] = (recipe_dir / value['file']).resolve()
        else:
            raise SystemExit(f'{path}: font {role!r} needs a path or a file entry')
        if not mapping[role].is_file():
            raise SystemExit(f'{path}: font file for {role!r} is missing: {mapping[role]}')
    return mapping


def load_unifont(path):
    table = {}
    try:
        with gzip.open(path, 'rt', encoding='ascii') as handle:
            for line in handle:
                code, encoded = line.split(':', 1)
                table[int(code, 16)] = bytes.fromhex(encoded.strip())
    except OSError as error:
        raise SystemExit(f'cannot read Unifont hex {path}: {error}') from None
    if not table:
        raise SystemExit(f'Unifont hex {path} is empty')
    return table


def size_limit(label, key, language, default=None):
    value = label.get(key, default)
    if isinstance(value, dict):
        value = value.get(language, default)
    return value


def text_for(label, language):
    value = label.get('text', {})
    if not isinstance(value, dict):
        raise SystemExit(f'{label.get("name", "?")}: text must be an object')
    if language not in value or not isinstance(value[language], str):
        raise SystemExit(f'{label.get("name", "?")}: missing {language} text')
    return value[language]


def style_for(recipe, label):
    styles = recipe.get('styles', {})
    name = label.get('style')
    if name not in styles:
        raise SystemExit(f'{label.get("name", "?")}: unknown style {name!r}')
    style = styles[name]
    for key in ('body', 'outline'):
        if not isinstance(style.get(key), int) or not 0 <= style[key] <= 255:
            raise SystemExit(f'{label.get("name", "?")}: style {key} must be a byte')
    return style


def load_font(path):
    if not path.is_file():
        raise SystemExit(f'font file not found: {path}')
    try:
        return ImageFont.truetype(str(path), 16)
    except OSError as error:
        raise SystemExit(f'cannot load font {path}: {error}') from None


def font_path_for(recipe, language, fonts, render):
    role = recipe.get('fonts', {}).get(language)
    if render == 'unifont':
        if 'Unifont' not in fonts:
            raise SystemExit('unifont label needs the Unifont font role')
        return fonts['Unifont']
    if not role:
        raise SystemExit(f'no font role for {language}')
    if role not in fonts:
        raise SystemExit(f'unknown font role {role!r}')
    return fonts[role]


def fit_pillow(path, text, width, height, largest, smallest, reserve):
    if not path or not path.is_file():
        raise SystemExit(f'font file not found: {path}')
    for size in range(largest, smallest - 1, -1):
        try:
            font = ImageFont.truetype(str(path), size)
        except OSError as error:
            raise SystemExit(f'cannot load font {path}: {error}') from None
        bbox = font.getbbox(text)
        ink_width, ink_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if ink_width + reserve <= width and ink_height + reserve <= height:
            return size, font, bbox, ink_width, ink_height
    raise SystemExit(f'{text!r} does not fit {width}x{height} (font {path.name}, '
                     f'sizes {smallest}..{largest})')


def build_pillow_mask(label, text, path, threshold, style, size):
    """Return a tile-atlas mask for one label at a chosen point size."""
    x0, y0, width, height = label['box']
    effect = label.get('effect', 'shadow')
    reserve = label.get('reserve', 2 if effect == 'outline' else 1)
    _, font, bbox, ink_width, ink_height = fit_pillow(
        path, text, width, height, size, size, reserve)
    if effect == 'outline':
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
    else:
        body = Image.new('L', (ink_width, ink_height), 0)
        ImageDraw.Draw(body).text((-bbox[0], -bbox[1]), text, font=font, fill=255)
        if threshold > 1:
            body = body.point(lambda value: 255 if value >= threshold else 0)
        left = x0 if label['align'] == 'left' else x0 + (width - ink_width) // 2
        top = y0 + (height - ink_height) // 2
        mask = bytearray(width * height)
        for yy in range(ink_height):
            gy = top + yy
            if not y0 <= gy < y0 + height:
                continue
            for xx in range(ink_width):
                gx = left + xx
                if not x0 <= gx < x0 + width:
                    continue
                if body.getpixel((xx, yy)):
                    mask[(gy - y0) * width + (gx - x0)] = 2
                    if style['outline'] != style['body']:
                        sx, sy = gx + 1, gy + 1
                        if sx < x0 + width and sy < y0 + height:
                            index = (sy - y0) * width + (sx - x0)
                            if mask[index] == 0:
                                mask[index] = 1
    if not any(mask):
        raise SystemExit(f'{label["name"]}: mask is empty for {text!r}')
    return bytes(mask)


def glyph_pattern(font, character, metrics, proportional):
    raw = font.get(ord(character))
    if raw is None:
        raise SystemExit(f'Unifont has no glyph for U+{ord(character):04X} ({character!r})')
    if len(raw) == 32:
        rows = [int.from_bytes(raw[i * 2:i * 2 + 2], 'big') for i in range(16)]
        source = 16
    else:
        rows = list(raw)
        source = 8
    mask = [[bool(rows[y] & (1 << (source - 1 - x))) for x in range(source)]
            for y in range(16)]
    points = [(x, y) for y in range(16) for x in range(source) if mask[y][x]]
    if not points:
        return [[False] * 2 for _ in range(metrics['height'])], metrics['height']
    if proportional:
        x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
        top, height = metrics['top'], metrics['height']
        box_w = x1 - x0 + 1
        width = max(1, round(box_w * height / metrics['source_height']))
        image = Image.new('L', (box_w, metrics['source_height']))
        image.putdata([255 if mask[top + y][x0 + x] else 0
                       for y in range(metrics['source_height']) for x in range(box_w)])
        image = image.resize((width, height), Image.Resampling.BOX)
    else:
        height = width = metrics['height']
        image = Image.new('L', (source, 16))
        image.putdata([255 if mask[y][x] else 0 for y in range(16) for x in range(source)])
        image = image.resize((width, height), Image.Resampling.BOX)
    pattern = [[image.getpixel((x, y)) >= 128 for x in range(width)] for y in range(height)]
    advance = width + (1 if proportional else 0)
    return pattern, advance


def unifont_metrics(font, text, height, proportional):
    if not proportional:
        return {'height': height}
    top, bottom = 16, 0
    for character in text:
        raw = font.get(ord(character))
        if raw is None:
            raise SystemExit(f'Unifont has no glyph for U+{ord(character):04X}')
        rows = [int.from_bytes(raw[i * 2:i * 2 + 2], 'big') for i in range(16)] \
            if len(raw) == 32 else list(raw)
        source = 16 if len(raw) == 32 else 8
        points = [(x, y) for y in range(16) for x in range(source)
                  if rows[y] & (1 << (source - 1 - x))]
        if not points:
            continue
        top = min(top, min(p[1] for p in points))
        bottom = max(bottom, max(p[1] for p in points))
    source_height = max(1, bottom - top + 1)
    return {'height': height, 'top': top, 'source_height': source_height}


def unifont_layout(font, text, height, proportional):
    metrics = unifont_metrics(font, text, height, proportional)
    cells = []
    total = 0
    for character in text:
        pattern, advance = glyph_pattern(font, character, metrics, proportional)
        cells.append((pattern, advance))
        total += advance
    if proportional:
        total -= 1
    return cells, total, metrics


def proportional_for(label, language):
    value = label.get('proportional', False)
    if isinstance(value, dict):
        return bool(value.get(language, False))
    return bool(value)


def render_for(label, language):
    value = label.get('render', 'pillow')
    if isinstance(value, dict):
        return value.get(language, 'pillow')
    return value


def build_unifont_mask(label, text, font, style, language, size):
    """Return a tile-atlas mask from Unifont's bitmap glyphs."""
    x0, y0, width, height = label['box']
    proportional = proportional_for(label, language)
    cells, total, _ = unifont_layout(font, text, size, proportional)
    if total > width or size > height:
        raise SystemExit(f'{label["name"]}: {text!r} needs {total}x{size}, '
                         f'box is {width}x{height}')
    left = x0 + (width - total) // 2
    top = y0 + (height - size) // 2
    mask = bytearray(width * height)
    for pattern, advance in cells:
        for y in range(size):
            for x in range(len(pattern[0])):
                if not pattern[y][x]:
                    continue
                gx, gy = left + x, top + y
                if not (x0 <= gx < x0 + width and y0 <= gy < y0 + height):
                    continue
                mask[(gy - y0) * width + (gx - x0)] = 2
                if style['outline'] != style['body']:
                    sx, sy = gx + 1, gy + 1
                    if sx < x0 + width and sy < y0 + height:
                        index = (sy - y0) * width + (sx - x0)
                        if mask[index] == 0:
                            mask[index] = 1
        left += advance
    if not any(mask):
        raise SystemExit(f'{label["name"]}: mask is empty for {text!r}')
    return bytes(mask)


def label_fits(label, text, language, recipe, fonts, unifont, size):
    render = render_for(label, language)
    x0, y0, width, height = label['box']
    effect = label.get('effect', 'shadow')
    reserve = label.get('reserve', 2 if effect == 'outline' else 1)
    path = font_path_for(recipe, language, fonts, render)
    if render == 'unifont':
        proportional = proportional_for(label, language)
        _, total, _ = unifont_layout(unifont, text, size, proportional)
        return total + reserve <= width and size <= height
    try:
        _, _, _, ink_width, ink_height = fit_pillow(
            path, text, width, height, size, size, reserve)
    except SystemExit:
        return False
    return ink_width + reserve <= width and ink_height + reserve <= height


def choose_sizes(recipe, language, fonts, unifont):
    labels = recipe['labels']
    groups = {}
    for label in labels:
        group = label.get('size_group')
        if not group:
            continue
        groups.setdefault(group, []).append(label)
    selected = {}
    for group, members in groups.items():
        largest = min(size_limit(member, 'max_size', language) for member in members)
        smallest = max(size_limit(member, 'min_size', language, 7) for member in members)
        chosen = None
        for size in range(largest, smallest - 1, -1):
            if all(label_fits(label, text_for(label, language), language, recipe,
                              fonts, unifont, size) for label in members):
                chosen = size
                break
        if chosen is None:
            raise SystemExit(f'{recipe["resource"]}/{group}: no common size fits '
                             f'{smallest}..{largest} for {language}')
        selected[group] = chosen
    result = {}
    for label in labels:
        if 'size' in label:
            result[label['name']] = label['size']
        elif label.get('size_group'):
            result[label['name']] = selected[label['size_group']]
        else:
            result[label['name']] = None
    return result


def build_label_mask(label, text, language, recipe, fonts, unifont, size):
    render = render_for(label, language)
    style = style_for(recipe, label)
    path = font_path_for(recipe, language, fonts, render)
    if render == 'unifont':
        if size is None:
            largest = size_limit(label, 'max_size', language)
            smallest = size_limit(label, 'min_size', language, 7)
            for candidate in range(largest, smallest - 1, -1):
                if label_fits(label, text, language, recipe, fonts, unifont, candidate):
                    size = candidate
                    break
            if size is None:
                raise SystemExit(f'{label["name"]}: no unifont size fits {text!r}')
        return build_unifont_mask(label, text, unifont, style, language, size)
    threshold = recipe['thresholds'][language]
    if size is None:
        width, height = label['box'][2], label['box'][3]
        effect = label.get('effect', 'shadow')
        reserve = label.get('reserve', 2 if effect == 'outline' else 1)
        size = fit_pillow(path, text, width, height,
                          size_limit(label, 'max_size', language),
                          size_limit(label, 'min_size', language, 7), reserve)[0]
    return build_pillow_mask(label, text, path, threshold, style, size)


def validate_box(rid, name, box, width, height):
    if not isinstance(box, list) or len(box) != 4 or any(not isinstance(v, int) for v in box):
        raise SystemExit(f'{rid}/{name}: box must be four integers')
    x0, y0, w, h = box
    if x0 < 0 or y0 < 0 or w <= 0 or h <= 0 or x0 + w > width or y0 + h > height:
        raise SystemExit(f'{rid}/{name}: box {box} leaves the {width}x{height} atlas')


def validate_tile_recipe(rid, recipe, languages, fonts):
    if recipe.get('format') != TILE_FORMAT:
        raise SystemExit(f'{rid}: unsupported recipe format {recipe.get("format")!r}')
    atlas_width, atlas_height = recipe.get('atlas_size', (0, 0))
    if atlas_width <= 0 or atlas_height <= 0 or atlas_width > 8192 or atlas_height > 8192:
        raise SystemExit(f'{rid}: atlas_size is missing or too large')
    if atlas_width % 8 or atlas_height % 8:
        raise SystemExit(f'{rid}: atlas_size must be a multiple of 8')
    styles = recipe.get('styles', {})
    if not isinstance(styles, dict) or not styles:
        raise SystemExit(f'{rid}: styles are missing')
    if not recipe.get('labels'):
        raise SystemExit(f'{rid}: no labels')
    for region in recipe.get('erase', []):
        if not isinstance(region, dict):
            raise SystemExit(f'{rid}: erase entry must be an object')
        validate_box(rid, 'erase', region.get('box'), atlas_width, atlas_height)
        if region.get('mode') not in ('row_background', 'color'):
            raise SystemExit(f'{rid}: unsupported erase mode {region.get("mode")!r}')
        if region['mode'] == 'color':
            if not isinstance(region.get('color'), int) or not 0 <= region['color'] <= 255:
                raise SystemExit(f'{rid}: erase colour must be a byte')
        if 'sample' in region:
            validate_box(rid, 'erase/sample', region['sample'], atlas_width, atlas_height)
    seen = set()
    for label in recipe['labels']:
        name = label.get('name')
        if not name or name in seen:
            raise SystemExit(f'{rid}: label names must be unique')
        seen.add(name)
        validate_box(rid, name, label.get('box'), atlas_width, atlas_height)
        style_for(recipe, label)
        if label.get('align') not in ('left', 'center'):
            raise SystemExit(f'{rid}/{name}: align must be left or center')
        for language in languages:
            largest = size_limit(label, 'max_size', language)
            smallest = size_limit(label, 'min_size', language, 7)
            if not isinstance(largest, int) or largest < 7:
                raise SystemExit(f'{rid}/{name}: max_size must be at least 7 for {language}')
            if not isinstance(smallest, int) or smallest < 7:
                raise SystemExit(f'{rid}/{name}: min_size must be at least 7 for {language}')
            if smallest > largest:
                raise SystemExit(f'{rid}/{name}: min_size exceeds max_size for {language}')
        if 'size' in label and (not isinstance(label['size'], int) or label['size'] < 7):
            raise SystemExit(f'{rid}/{name}: size must be at least 7')
        if label.get('effect', 'shadow') not in ('shadow', 'outline'):
            raise SystemExit(f'{rid}/{name}: unsupported effect {label.get("effect")!r}')
        renders = label.get('render', 'pillow')
        if isinstance(renders, str):
            renders = {language: renders for language in languages}
        if not isinstance(renders, dict) or set(renders) - set(languages) or \
                any(value not in ('pillow', 'unifont') for value in renders.values()):
            raise SystemExit(f'{rid}/{name}: render must be "pillow", "unifont"'
                             ' or a per-language map of those')
        for language in languages:
            text_for(label, language)
            font_path_for(recipe, language, fonts, render_for(label, language))
        if 'size_group' in label and not isinstance(label['size_group'], str):
            raise SystemExit(f'{rid}/{name}: size_group must be a string')


def validate_mono_recipe(rid, recipe, languages, fonts, size):
    if recipe.get('format') != MONO_FORMAT:
        raise SystemExit(f'{rid}: unsupported recipe format {recipe.get("format")!r}')
    if not recipe.get('writes'):
        raise SystemExit(f'{rid}: no writes')
    for write in recipe['writes']:
        name = write.get('name', '?')
        if write.get('generator') not in ('slots', 'canvas'):
            raise SystemExit(f'{rid}/{name}: unsupported generator {write.get("generator")!r}')
        offset = write.get('offset')
        if not isinstance(offset, int) or offset < 0:
            raise SystemExit(f'{rid}/{name}: offset must be a non-negative integer')
        width, height = write.get('width'), write.get('height')
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise SystemExit(f'{rid}/{name}: width/height must be positive')
        if width % 8:
            raise SystemExit(f'{rid}/{name}: width must be a multiple of 8')
        if write.get('background') not in (0, 1) or write.get('ink') not in (0, 1):
            raise SystemExit(f'{rid}/{name}: background and ink must be 0 or 1')
        if write.get('background') == write.get('ink'):
            raise SystemExit(f'{rid}/{name}: background and ink must differ')
        for language in languages:
            font_path_for(recipe, language, fonts, 'pillow')
        if write['generator'] == 'slots':
            slots = write.get('slots')
            texts = write.get('texts')
            if not isinstance(slots, list) or not isinstance(texts, list) or len(slots) != len(texts):
                raise SystemExit(f'{rid}/{name}: slots and texts must be equal-length lists')
            if len(set(slots)) != len(slots) or any(not isinstance(slot, int) or slot < 0
                                                    for slot in slots):
                raise SystemExit(f'{rid}/{name}: slot indices must be unique non-negative integers')
            stride = width // 8 * height
            if slots and offset + (max(slots) + 1) * stride > size:
                raise SystemExit(f'{rid}/{name}: slots leave the resource')
            for index, text in enumerate(texts):
                if not isinstance(text, dict):
                    raise SystemExit(f'{rid}/{name}[{index}]: text must be an object')
                for language in languages:
                    if language not in text or not isinstance(text[language], str):
                        raise SystemExit(f'{rid}/{name}[{index}]: missing {language} text')
            for key in ('max_size', 'min_size'):
                if not isinstance(write.get(key), int) or write[key] < 7:
                    raise SystemExit(f'{rid}/{name}: {key} must be at least 7')
            if write['min_size'] > write['max_size']:
                raise SystemExit(f'{rid}/{name}: min_size exceeds max_size')
        else:
            lines = write.get('lines')
            if not isinstance(lines, list) or not lines:
                raise SystemExit(f'{rid}/{name}: canvas needs lines')
            for index, line in enumerate(lines):
                if not isinstance(line, dict):
                    raise SystemExit(f'{rid}/{name}[{index}]: line must be an object')
                for key in ('y', 'height'):
                    if not isinstance(line.get(key), int) or line[key] < 0:
                        raise SystemExit(f'{rid}/{name}[{index}]: {key} must be non-negative')
                if line['y'] + line['height'] > height:
                    raise SystemExit(f'{rid}/{name}[{index}]: line leaves the canvas')
                for language in languages:
                    text_for(line, language)
                for key in ('max_size', 'min_size'):
                    if not isinstance(line.get(key), int) or line[key] < 7:
                        raise SystemExit(f'{rid}/{name}[{index}]: {key} must be at least 7')
                if line['min_size'] > line['max_size']:
                    raise SystemExit(f'{rid}/{name}[{index}]: min_size exceeds max_size')


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


def compile_tile(rid, meta, recipe, languages, fonts, unifont, oracle, blob, preview_dir):
    operations = {}
    report = {'format': recipe['format'], 'labels': {}}
    atlas_width, atlas_height = recipe['atlas_size']
    for language in languages:
        sizes = choose_sizes(recipe, language, fonts, unifont)
        labels = []
        preview = None
        if preview_dir:
            preview = Image.new('L', (atlas_width, atlas_height), 128)
        for label in recipe['labels']:
            text = text_for(label, language)
            style = style_for(recipe, label)
            size = sizes[label['name']]
            mask = build_label_mask(label, text, language, recipe, fonts, unifont, size)
            labels.append({
                'name': label['name'],
                'box': label['box'],
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
            report['labels'].setdefault(language, {})[label['name']] = {
                'text': text,
                'font': 'Unifont' if render_for(label, language) == 'unifont'
                        else recipe['fonts'][language],
                'font_size': size,
                'box': label['box'],
                'ink_pixels': sum(1 for value in mask if value),
            }
        operation = {'type': 'tile_atlas', 'labels': labels}
        if recipe.get('erase'):
            operation['erase'] = recipe['erase']
        operations[language] = [operation]
        if preview is not None:
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview.resize((preview.width * 2, preview.height * 2),
                           Image.Resampling.NEAREST).save(
                preview_dir / f'{language}-{rid}-mask.png')
    header = {
        'format': recipe['format'],
        'atlas_size': recipe['atlas_size'],
        'archive_offset': meta['archive_offset'],
        'size': meta['size'],
        'original_sha256': meta['original_sha256'],
        'expected': {language: oracle.get(rid, {}).get(language, '') for language in languages},
        'operations': operations,
    }
    return header, report


def paint_slot(text, width, height, path, largest, smallest, background, ink,
               box_width, align='center'):
    image = Image.new('1', (width, height), background)
    draw = ImageDraw.Draw(image)
    chosen = None
    for size in range(largest, smallest - 1, -1):
        font = ImageFont.truetype(str(path), size)
        bbox = draw.textbbox((0, 0), text, font=font)
        ink_width, ink_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if ink_width <= box_width and ink_height <= height:
            chosen = (size, font, bbox, ink_width, ink_height)
            break
    if chosen is None:
        raise SystemExit(f'text does not fit {box_width}x{height}: {text!r} '
                         f'(font {path.name}, sizes {smallest}..{largest})')
    size, font, bbox, ink_width, ink_height = chosen
    if align == 'left':
        x = -bbox[0]
    else:
        x = (box_width - ink_width) // 2 - bbox[0]
    y = (height - ink_height) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=ink)
    return image.tobytes(), size


def compile_mono(rid, meta, recipe, languages, fonts, oracle, blob, preview_dir):
    operations = {}
    report = {'format': recipe['format'], 'writes': {}}
    for language in languages:
        writes = []
        sheets = []
        for spec in recipe['writes']:
            name = spec.get('name', '?')
            path = font_path_for(recipe, language, fonts, 'pillow')
            offset = spec['offset']
            width, height = spec['width'], spec['height']
            stride = width // 8 * height
            if spec['generator'] == 'slots':
                box_width = spec.get('box_width', width)
                if isinstance(box_width, dict):
                    box_width = box_width.get(language, width)
                sheet = Image.new('1', (width, height * len(spec['slots'])), spec['background'])
                for index, (slot, text) in enumerate(zip(spec['slots'], spec['texts'])):
                    data, size = paint_slot(
                        text[language], width, height, path,
                        spec['max_size'], spec['min_size'],
                        spec['background'], spec['ink'], box_width,
                        spec.get('align', 'center'))
                    writes.append({'name': f'{name}[{slot}]',
                                   'offset': offset + slot * stride,
                                   'data_offset': len(blob),
                                   'data_length': len(data)})
                    blob.extend(data)
                    sheet.paste(Image.frombytes('1', (width, height), data), (0, index * height))
                    report['writes'].setdefault(language, {})[f'{name}[{slot}]'] = {
                        'text': text[language], 'font': recipe['fonts'][language],
                        'font_size': size, 'offset': offset + slot * stride,
                    }
                sheets.append(sheet)
            else:
                image = Image.new('1', (width, height), spec['background'])
                draw = ImageDraw.Draw(image)
                for line in spec['lines']:
                    text = text_for(line, language)
                    chosen = None
                    for size in range(line['max_size'], line['min_size'] - 1, -1):
                        font = ImageFont.truetype(str(path), size)
                        bbox = draw.textbbox((0, 0), text, font=font)
                        ink_width, ink_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
                        if ink_width <= width - spec.get('reserve', 0) and ink_height <= line['height']:
                            chosen = (size, font, bbox, ink_width, ink_height)
                            break
                    if chosen is None:
                        raise SystemExit(f'{rid}/{name}: {text!r} does not fit the card')
                    size, font, bbox, ink_width, ink_height = chosen
                    x = (width - ink_width) // 2 - bbox[0]
                    y = line['y'] + (line['height'] - ink_height) // 2 - bbox[1]
                    draw.text((x, y), text, font=font, fill=spec['ink'])
                    report['writes'].setdefault(language, {})[f'{name}:{line["y"]}'] = {
                        'text': text, 'font': recipe['fonts'][language],
                        'font_size': size, 'y': line['y'],
                    }
                data = image.tobytes()
                writes.append({'name': name, 'offset': offset,
                               'data_offset': len(blob), 'data_length': len(data)})
                blob.extend(data)
                sheets.append(image)
        operations[language] = [{'type': 'mono_bitmap', 'writes': writes}]
        if preview_dir:
            preview_dir.mkdir(parents=True, exist_ok=True)
            total_height = sum(sheet.height for sheet in sheets)
            preview = Image.new('1', (max(sheet.width for sheet in sheets), total_height), 1)
            y = 0
            for sheet in sheets:
                preview.paste(sheet, (0, y))
                y += sheet.height
            preview.convert('L').resize((preview.width * 2, preview.height * 2),
                                        Image.Resampling.NEAREST).save(
                preview_dir / f'{language}-{rid}-mask.png')
    header = {
        'format': recipe['format'],
        'archive_offset': meta['archive_offset'],
        'size': meta['size'],
        'original_sha256': meta['original_sha256'],
        'expected': {language: oracle.get(rid, {}).get(language, '') for language in languages},
        'operations': operations,
    }
    return header, report


def load_oracle(recipe_dir, languages, allow_missing):
    oracle = {}
    for language in languages:
        path = recipe_dir / 'expected' / f'{language}.json'
        if not path.is_file():
            if allow_missing:
                continue
            raise SystemExit(f'missing expected file: {path}')
        value = read_json(path).get('resources', {})
        for rid, digest_value in value.items():
            if not isinstance(digest_value, str) or len(digest_value) != 64:
                raise SystemExit(f'{path}: invalid hash for {rid}')
            oracle.setdefault(rid, {})[language] = digest_value
    return oracle


def compile_plan(recipe_dir, output, report_path, preview_dir, allow_missing=False):
    manifest = read_json(recipe_dir / 'manifest.json')
    validate_manifest(manifest, recipe_dir)
    languages = manifest['languages']
    fonts = font_map(recipe_dir)
    unifont = load_unifont(fonts['Unifont'])
    oracle = load_oracle(recipe_dir, languages, allow_missing)
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
    report = {'version': manifest['version'], 'languages': languages, 'resources': {}}
    for rid, meta in manifest['resources'].items():
        recipe = read_json(recipe_dir / meta['recipe'])
        if recipe.get('resource') != rid:
            raise SystemExit(f'{rid}: recipe declares resource {recipe.get("resource")!r}')
        fmt = recipe.get('format')
        if fmt == TILE_FORMAT:
            validate_tile_recipe(rid, recipe, languages, fonts)
            if allow_missing:
                oracle.setdefault(rid, {})
            resource_header, resource_report = compile_tile(
                rid, meta, recipe, languages, fonts, unifont, oracle, blob, preview_dir)
        elif fmt == MONO_FORMAT:
            validate_mono_recipe(rid, recipe, languages, fonts, meta['size'])
            if allow_missing:
                oracle.setdefault(rid, {})
            resource_header, resource_report = compile_mono(
                rid, meta, recipe, languages, fonts, oracle, blob, preview_dir)
        else:
            raise SystemExit(f'{rid}: unsupported recipe format {fmt!r}')
        header['resources'][rid] = resource_header
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
    parser.add_argument('--allow-missing-expected', action='store_true',
                        help='development only: compile without expected hashes')
    args = parser.parse_args()
    report = None if str(args.report) in ('', '.') else args.report
    compile_plan(args.recipe_dir, args.output, report, args.preview_dir,
                 args.allow_missing_expected)
    return 0


if __name__ == '__main__':
    sys.exit(main())
