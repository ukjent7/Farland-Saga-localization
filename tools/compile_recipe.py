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
packed 1bpp rows, one byte per eight pixels, MSB first.  Story pages store a
196x48 2bpp image plus the character count for the timing table.
"""
import argparse
import gzip
import hashlib
import json
import math
import struct
import sys
from collections import Counter
from io import BytesIO
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont

MAGIC = b'FS2RECIPE1\n'
RECIPE_FORMAT = 1
PLAN_FORMAT = 2
REPO = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = REPO / 'recipe'
DEFAULT_OUTPUT = REPO / 'patcher' / 'plan' / 'plan.bin'
TILE_FORMAT = 'tile_atlas_8bpp'
MONO_FORMAT = 'mono_bitmap_1bpp'
STORY_FORMAT = 'story_page_2bpp'
TEXTURE_FORMAT = 'texture_atlas_rgba'
BINARY_FORMAT = 'binary_patches'


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


_FONT_CMAPS = {}


def font_cmap(path):
    key = str(path)
    if key not in _FONT_CMAPS:
        font = TTFont(key)
        cmap = set()
        for table in font['cmap'].tables:
            cmap.update(table.cmap)
        _FONT_CMAPS[key] = cmap
    return _FONT_CMAPS[key]


def check_font_coverage(recipe, language, fonts, text, rid, name):
    path = font_path_for(recipe, language, fonts, 'pillow')
    cmap = font_cmap(path)
    missing = sorted({char for char in text
                      if char not in '\n\r\t' and ord(char) not in cmap})
    if missing:
        codes = ' '.join(f'U+{ord(char):04X}' for char in missing)
        raise SystemExit(f'{rid}/{name}: {language} font lacks {codes}')


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
                    check_font_coverage(recipe, language, fonts, text[language], rid,
                                        f'{name}[{index}]')
            for key in ('max_size', 'min_size'):
                if not isinstance(write.get(key), int) or write[key] < 7:
                    raise SystemExit(f'{rid}/{name}: {key} must be at least 7')
            if write['min_size'] > write['max_size']:
                raise SystemExit(f'{rid}/{name}: min_size exceeds max_size')
        elif write.get('wrap'):
            text = write.get('text')
            if not isinstance(text, dict):
                raise SystemExit(f'{rid}/{name}: wrapped canvas needs a text object')
            for language in languages:
                if language not in text or not isinstance(text[language], str):
                    raise SystemExit(f'{rid}/{name}: missing {language} text')
                words = text[language].replace('\n', ' ')
                check_font_coverage(recipe, language, fonts, words, rid, name)
            for key in ('line_height', 'max_lines'):
                if not isinstance(write.get(key), int) or write[key] < 1:
                    raise SystemExit(f'{rid}/{name}: {key} must be a positive integer')
            if write['line_height'] * write['max_lines'] > height:
                raise SystemExit(f'{rid}/{name}: wrapped lines leave the canvas')
            if not isinstance(write.get('reserve', 0), int) or write['reserve'] < 0:
                raise SystemExit(f'{rid}/{name}: reserve must be non-negative')
            if write.get('align', 'center') not in ('left', 'center'):
                raise SystemExit(f'{rid}/{name}: align must be left or center')
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
    if manifest.get('format') != RECIPE_FORMAT:
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
    story = manifest.get('story')
    if story is not None:
        if not isinstance(story, dict) or not story.get('recipe') or not story.get('pages'):
            raise SystemExit('manifest story block needs recipe and pages')
        for key in ('recipe', 'pages'):
            if not (recipe_dir / story[key]).is_file():
                raise SystemExit(f'manifest story {key} file is missing')
    for key in ('executable', 'texture'):
        section = manifest.get(key)
        if not isinstance(section, dict) or not isinstance(section.get('recipe'), str):
            raise SystemExit(f'manifest {key} block needs a recipe')
        if not (recipe_dir / section['recipe']).is_file():
            raise SystemExit(f'manifest {key} recipe {section["recipe"]} is missing')
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


def wrap_text(draw, text, font, max_width, language):
    """Split one paragraph into lines that fit max_width."""
    if language == 'zh-CN':
        units = list(text)
        separator = ''
    else:
        units = text.split(' ')
        separator = ' '
    lines = []
    line = ''
    for unit in units:
        candidate = f'{line}{separator}{unit}' if line else unit
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ''
        if draw.textlength(unit, font=font) <= max_width:
            line = unit
            continue
        piece = ''
        for char in unit:
            if piece and draw.textlength(piece + char, font=font) > max_width:
                lines.append(piece)
                piece = char
            else:
                piece += char
        line = piece
    if line:
        lines.append(line)
    return lines


def wrap_paragraphs(draw, text, font, max_width, language):
    lines = []
    for paragraph in text.split('\n'):
        paragraph = paragraph.strip()
        if paragraph:
            lines.extend(wrap_text(draw, paragraph, font, max_width, language))
    return lines


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
                if spec.get('wrap'):
                    text = text_for(spec, language)
                    reserve = spec.get('reserve', 0)
                    chosen = None
                    for size in range(spec['max_size'], spec['min_size'] - 1, -1):
                        font = ImageFont.truetype(str(path), size)
                        lines = wrap_paragraphs(draw, text, font, width - reserve, language)
                        if len(lines) > spec['max_lines']:
                            continue
                        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
                        if any(box[2] - box[0] > width - reserve for box in boxes):
                            continue
                        chosen = (size, font, lines, boxes)
                        break
                    if chosen is None:
                        raise SystemExit(
                            f'{rid}/{name}: {text!r} needs more than '
                            f'{spec["max_lines"]} lines at {spec["min_size"]}px')
                    size, font, lines, boxes = chosen
                    left = spec.get('left', 0)
                    top = spec.get('top', 0)
                    for index, line in enumerate(lines):
                        bbox = boxes[index]
                        ink_width = bbox[2] - bbox[0]
                        if spec.get('align', 'center') == 'left':
                            x = left - bbox[0]
                        else:
                            x = (width - ink_width) // 2 - bbox[0]
                        y = top + index * spec['line_height'] - bbox[1]
                        draw.text((x, y), line, font=font, fill=spec['ink'])
                        report['writes'].setdefault(language, {})[f'{name}[{index}]'] = {
                            'text': line, 'font': recipe['fonts'][language],
                            'font_size': size, 'y': y + bbox[1],
                        }
                else:
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


_UNIFONT_CELLS = {}


def unifont_cell(font, character, rid):
    key = ord(character)
    if key not in _UNIFONT_CELLS:
        raw = font.get(key)
        if raw is None:
            raise SystemExit(f'{rid}: Unifont has no glyph for U+{key:04X} ({character!r})')
        if len(raw) not in (16, 32):
            raise SystemExit(f'{rid}: U+{key:04X} has an unexpected Unifont cell')
        width = 16 if len(raw) == 32 else 8
        _UNIFONT_CELLS[key] = (Image.frombytes('1', (width, 16), raw), width)
    return _UNIFONT_CELLS[key]


_STORY_CLOSE = set('，。！？、；：…）】」』》〉”’,.;:!?)]}')
_STORY_OPEN = set('（【「『《〈“‘([{')


def _story_atoms(font, paragraph, language, max_width, rid):
    atoms = []
    if language in ('en-US', 'ko-KR'):
        for word in paragraph.split(' '):
            if not word:
                continue
            glyphs = [unifont_cell(font, char, rid) for char in word]
            width = sum(glyph[1] for glyph in glyphs)
            if width <= max_width:
                atoms.append((glyphs, width, False, word))
                continue
            chunk = []
            used = 0
            pieces = []
            for glyph, glyph_width in glyphs:
                if chunk and used + glyph_width > max_width:
                    pieces.append((chunk, used))
                    chunk = []
                    used = 0
                chunk.append((glyph, glyph_width))
                used += glyph_width
            if chunk:
                pieces.append((chunk, used))
            for index, (piece, piece_width) in enumerate(pieces):
                atoms.append((piece, piece_width, index > 0, word))
    else:
        for character in paragraph:
            glyph, width = unifont_cell(font, character, rid)
            atoms.append(([(glyph, width)], width, True, character))
    return atoms


def _wrap_story_paragraph(font, paragraph, language, max_width, space_width, rid):
    """Break one paragraph into balanced lines, keeping punctuation with its text."""
    atoms = _story_atoms(font, paragraph, language, max_width, rid)
    count = len(atoms)
    if count == 0:
        return []
    prefix = [0] * (count + 1)
    spaced = [0] * (count + 1)
    for index, (_, width, glue, _) in enumerate(atoms):
        prefix[index + 1] = prefix[index] + width
        spaced[index + 1] = spaced[index] + (0 if glue else 1)

    def line_width(start, end):
        return (prefix[end] - prefix[start] +
                space_width * (spaced[end] - spaced[start + 1]))

    def break_allowed(start, end):
        if end >= count or language in ('en-US', 'ko-KR'):
            return True
        if atoms[end][3] in _STORY_CLOSE:
            return False
        if start > 0 and atoms[start - 1][3] in _STORY_OPEN:
            return False
        return True

    infinity = 10 ** 9
    minimal = [infinity] * (count + 1)
    minimal[0] = 0
    for end in range(1, count + 1):
        for start in range(end):
            if (minimal[start] + 1 < minimal[end] and
                    line_width(start, end) <= max_width and break_allowed(start, end)):
                minimal[end] = minimal[start] + 1
    if minimal[count] >= infinity:
        return None

    cost = [infinity] * (count + 1)
    previous = [-1] * (count + 1)
    cost[0] = 0
    for end in range(1, count + 1):
        for start in range(end):
            if minimal[start] + 1 != minimal[end] or not break_allowed(start, end):
                continue
            width = line_width(start, end)
            if width > max_width:
                continue
            value = cost[start] + (max_width - width) ** 2
            if value < cost[end]:
                cost[end] = value
                previous[end] = start
    if previous[count] < 0:
        return None

    space = Image.new('1', (space_width, 16))
    lines = []
    end = count
    while end > 0:
        start = previous[end]
        if start < 0:
            return None
        line = []
        for index in range(start, end):
            if index > start and not atoms[index][2]:
                line.append((space, space_width))
            line.extend(atoms[index][0])
        lines.append(line)
        end = start
    lines.reverse()
    return lines


def wrap_story_unifont(font, text, language, max_width, compact, rid):
    """Wrap one story page using Unifont's native 8/16px cells."""
    space_width = 4 if compact else 8
    lines = []
    for paragraph in text.split('\n'):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        wrapped = _wrap_story_paragraph(font, paragraph, language, max_width,
                                        space_width, rid)
        if wrapped is None:
            return None
        lines.extend(wrapped)
    return lines


def paint_story_unifont(font, text, language, spec, rid):
    variants = [text]
    if '\n' in text:
        if language in ('en-US', 'ko-KR'):
            variants.append(' '.join(part.strip() for part in text.split('\n') if part.strip()))
        else:
            variants.append(''.join(part.strip() for part in text.split('\n') if part.strip()))
    for compact in (False, True):
        for variant in variants:
            lines = wrap_story_unifont(font, variant, language,
                                       spec['max_width'], compact, rid)
            if lines is None or len(lines) > spec['max_lines']:
                continue
            mask = Image.new('1', (spec['width'], spec['height']))
            for index, line in enumerate(lines):
                x = 0
                for glyph, width in line:
                    mask.paste(glyph, (x, index * spec['line_height']))
                    x += width
            return mask, spec['unifont_size'], len(lines)
    return None


def paint_story_ttf(path, text, language, spec, rid):
    draw = ImageDraw.Draw(Image.new('L', (1, 1)))
    variants = [text]
    if '\n' in text:
        if language in ('en-US', 'ko-KR'):
            variants.append(' '.join(part.strip() for part in text.split('\n') if part.strip()))
        else:
            variants.append(''.join(part.strip() for part in text.split('\n') if part.strip()))
    for size in range(spec['max_ttf_size'], spec['min_ttf_size'] - 1, -1):
        font = ImageFont.truetype(str(path), size)
        for variant in variants:
            lines = wrap_paragraphs(draw, variant, font, spec['max_width'], language)
            if len(lines) > spec['max_lines']:
                continue
            if any(draw.textlength(line, font=font) > spec['max_width'] for line in lines):
                continue
            mask = Image.new('1', (spec['width'], spec['height']))
            target = ImageDraw.Draw(mask)
            for index, line in enumerate(lines):
                bbox = draw.textbbox((0, 0), line, font=font)
                target.text((-bbox[0], index * spec['line_height'] - bbox[1]),
                            line, font=font, fill=1)
            return mask, size, len(lines)
    raise SystemExit(f'{rid}: {language} story text does not fit {spec["max_lines"]} lines '
                     f'at {spec["min_ttf_size"]}px: {text!r}')


def compose_story_image(mask, spec):
    shadow = Image.new('1', (spec['width'], spec['height']))
    shadow.paste(mask, (1, 1))
    for y in range(spec['line_height'], spec['height'], spec['line_height']):
        shadow.paste(0, (0, y, spec['width'], y + 1))
    image = Image.new('L', (spec['width'], spec['height']), 0)
    image.paste(85, (0, 0), shadow)
    image.paste(170, (0, 0), mask)
    span = 0
    for index in range(spec['height'] // spec['line_height']):
        band = image.crop((0, index * spec['line_height'],
                           spec['width'], (index + 1) * spec['line_height']))
        bbox = band.getbbox()
        if bbox:
            span = max(span, index * spec['width'] + bbox[2])
    timing = math.ceil(span / spec['columns_per_character'])
    return image, timing


def encode_story_2bpp(image):
    pixels = image.load()
    data = bytearray(2352)
    for column in range(588):
        for half in range(2):
            word = 0
            for y in range(8):
                value = pixels[column % 196, column // 196 * 16 + half * 8 + y] // 85
                word |= value << (14 - y * 2)
            struct.pack_into('>H', data, column * 4 + half * 2, word)
    return bytes(data)


def compile_story(recipe_dir, manifest, languages, fonts, unifont, oracle, blob,
                  preview_dir):
    settings = manifest['story']
    spec = read_json(recipe_dir / settings['recipe'])
    if spec.get('format') != STORY_FORMAT:
        raise SystemExit(f'unsupported story format {spec.get("format")!r}')
    pages = read_json(recipe_dir / settings['pages']).get('pages')
    if not isinstance(pages, list) or not pages:
        raise SystemExit('story page index is empty')
    translations = {}
    for language in languages:
        source = spec.get('sources', {}).get(language)
        if not source:
            raise SystemExit(f'story recipe has no source for {language}')
        rows = read_json(REPO / source)
        translations[language] = {str(row.get('id')): row for row in rows}
        if len(translations[language]) != len(rows):
            raise SystemExit(f'{source}: duplicate story page ids')

    resources = {}
    report = {}
    seen = set()
    for page in pages:
        pid = str(page.get('id'))
        if not pid or pid in seen:
            raise SystemExit(f'story page id is missing or repeated: {pid!r}')
        seen.add(pid)
        offset = page.get('archive_offset')
        size = page.get('size')
        if not isinstance(offset, int) or offset < 0 or offset % 2048:
            raise SystemExit(f'story page {pid}: archive_offset is not sector aligned')
        if size != 4096:
            raise SystemExit(f'story page {pid}: size must be 4096')
        texts = {}
        blank = True
        for language in languages:
            row = translations[language].get(pid)
            if row is None:
                raise SystemExit(f'story page {pid}: missing {language} translation')
            text = row.get('translation', '')
            if row.get('blank') or not text.strip():
                texts[language] = ''
            else:
                texts[language] = text
                blank = False
        if blank:
            continue
        if any(not text.strip() for text in texts.values()):
            raise SystemExit(f'story page {pid}: has text in some languages only')
        rid = f'P{pid}'
        operations = {}
        page_report = {}
        for language in languages:
            mask = paint_story_unifont(unifont, texts[language], language, spec, rid)
            if mask is None:
                role = spec.get('fonts', {}).get(language)
                if not role or role not in fonts:
                    raise SystemExit(f'story recipe has no font role for {language}')
                check_font_coverage(spec, language, fonts, texts[language], rid, 'story')
                mask = paint_story_ttf(fonts[role], texts[language], language, spec, rid)
                font_name = role
            else:
                font_name = 'Unifont'
            image, timing = compose_story_image(mask[0], spec)
            if timing > (spec['timing_end'] - spec['timing_table_offset']) // 4 - 1:
                raise SystemExit(f'{rid}: {language} timing table too long ({timing})')
            data = encode_story_2bpp(image)
            operations[language] = [{
                'type': 'story_page',
                'data_offset': len(blob),
                'data_length': len(data),
                'timing_characters': timing,
            }]
            blob.extend(data)
            page_report[language] = {
                'font': font_name,
                'font_size': mask[1],
                'lines': mask[2],
                'timing_characters': timing,
            }
            if preview_dir:
                preview_dir.mkdir(parents=True, exist_ok=True)
                image.resize((image.width * 3, image.height * 3),
                             Image.Resampling.NEAREST).save(
                    preview_dir / f'{language}-{rid}-mask.png')
        resources[rid] = {
            'format': STORY_FORMAT,
            'archive_offset': offset,
            'size': size,
            'original_sha256': page.get('original_sha256'),
            'expected': {language: oracle.get(rid, {}).get(language, '')
                         for language in languages},
            'operations': operations,
        }
        report[rid] = page_report
    if not resources:
        raise SystemExit('story recipe produced no pages')
    return resources, report


def texture_text_metrics(draw, text, font, stroke):
    if '\n' in text:
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=4,
                                       stroke_width=stroke)
    else:
        bbox = font.getbbox(text, stroke_width=stroke)
    return bbox, bbox[2] - bbox[0], bbox[3] - bbox[1]


def fit_text_layer(text, path, width, height, largest, smallest, stroke):
    draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    for size in range(largest, smallest - 1, -1):
        try:
            font = ImageFont.truetype(str(path), size)
        except OSError as error:
            raise SystemExit(f'cannot load font {path}: {error}') from None
        bbox, ink_width, ink_height = texture_text_metrics(draw, text, font, stroke)
        if ink_width <= width and ink_height <= height:
            return size, font, bbox, ink_width, ink_height
    raise SystemExit(f'texture text {text!r} does not fit {width}x{height} '
                     f'(font {path.name}, sizes {smallest}..{largest})')


def paint_text_layer(width, height, text, path, largest, smallest, align,
                     stroke=0, cell=None):
    """Render one RGBA text layer with a transparent background.

    ``cell`` is the horizontal range inside the part box that the original
    frontend layout used for its text.  It is relative to the left edge.  The
    installer blits the result over the original atlas, so leaving the rest of
    the layer transparent keeps every pixel outside the label untouched.
    """
    layer = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    if cell is None:
        left, right = 0, width
        limits = width - 8
    else:
        left, right = cell
        if align == 'left':
            limits = width - left - 4
        elif align == 'right':
            limits = right - 4
        else:
            limits = width - 8
    size, font, bbox, ink_width, ink_height = fit_text_layer(
        text, path, limits, height - 4, largest, smallest, stroke)
    if align == 'left':
        x = left
    elif align == 'right':
        x = right - ink_width
    else:
        x = (left + right - ink_width) // 2
    y = (height - ink_height) // 2
    draw = ImageDraw.Draw(layer)
    if '\n' in text:
        draw.multiline_text((x - bbox[0], y - bbox[1]), text, font=font,
                            fill=(255, 255, 255, 255), spacing=4, align=align,
                            stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    else:
        draw.text((x - bbox[0], y - bbox[1]), text, font=font,
                  fill=(255, 255, 255, 255), stroke_width=stroke,
                  stroke_fill=(0, 0, 0, 255))
    return layer, size


def encode_layer(layer):
    buffer = BytesIO()
    layer.save(buffer, format='PNG', compress_level=9)
    return buffer.getvalue()


def validate_texture_recipe(recipe, languages, fonts):
    if recipe.get('format') != TEXTURE_FORMAT:
        raise SystemExit(f'texture: unsupported format {recipe.get("format")!r}')
    if not isinstance(recipe.get('path'), str) or not recipe['path']:
        raise SystemExit('texture: path is missing')
    if len(recipe.get('original_sha256', '')) != 64:
        raise SystemExit('texture: original_sha256 is missing')
    size = recipe.get('size')
    if not isinstance(size, int) or size <= 0:
        raise SystemExit('texture: size must be positive')
    image_size = recipe.get('image_size')
    if (not isinstance(image_size, list) or len(image_size) != 2
            or any(not isinstance(value, int) or value <= 0 for value in image_size)):
        raise SystemExit('texture: image_size must be two positive integers')
    width, height = image_size
    part_count = recipe.get('part_count')
    if not isinstance(part_count, int) or part_count <= 0:
        raise SystemExit('texture: part_count must be positive')
    parts = recipe.get('parts')
    if not isinstance(parts, dict) or not parts:
        raise SystemExit('texture: parts are missing')
    for key, box in parts.items():
        if not key.isdigit() or int(key) >= part_count:
            raise SystemExit(f'texture: part key {key!r} is invalid')
        if (not isinstance(box, list) or len(box) != 4
                or any(not isinstance(value, int) for value in box)):
            raise SystemExit(f'texture: part {key} must be four integers')
        x0, y0, x1, y1 = box
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > width or y1 > height:
            raise SystemExit(f'texture: part {key} leaves the atlas')
    role_map = recipe.get('fonts')
    if not isinstance(role_map, dict):
        raise SystemExit('texture: fonts are missing')
    for language in languages:
        role = role_map.get(language)
        if role not in fonts:
            raise SystemExit(f'texture: unknown font role {role!r} for {language}')
    labels = recipe.get('labels')
    if not isinstance(labels, list) or not labels:
        raise SystemExit('texture: no labels')
    seen = set()
    for label in labels:
        name = label.get('name')
        if not name or name in seen:
            raise SystemExit('texture: label names must be unique')
        seen.add(name)
        part = label.get('part')
        if not isinstance(part, int) or str(part) not in parts:
            raise SystemExit(f'texture/{name}: part is missing from parts')
        x0, y0, x1, y1 = parts[str(part)]
        box_width = x1 - x0
        if label.get('align') not in ('left', 'center', 'right'):
            raise SystemExit(f'texture/{name}: align must be left, center or right')
        cell = label.get('cell')
        if (not isinstance(cell, list) or len(cell) != 2
                or any(not isinstance(value, int) for value in cell)
                or cell[0] < 0 or cell[1] > box_width or cell[0] >= cell[1]):
            raise SystemExit(f'texture/{name}: cell is invalid')
        for key in ('min_size', 'max_size'):
            value = label.get(key)
            if isinstance(value, dict):
                if set(value) != set(languages):
                    raise SystemExit(f'texture/{name}: {key} must cover every language')
            elif not isinstance(value, int):
                raise SystemExit(f'texture/{name}: {key} is missing')
        for language in languages:
            text = label.get('text', {}).get(language)
            if not isinstance(text, str) or not text:
                raise SystemExit(f'texture/{name}: missing {language} text')
            if any(ord(char) < 32 and char != '\n' for char in text):
                raise SystemExit(f'texture/{name}: control character in {language} text')
            largest = size_limit(label, 'max_size', language)
            smallest = size_limit(label, 'min_size', language, 8)
            if not isinstance(largest, int) or not isinstance(smallest, int) or smallest < 7:
                raise SystemExit(f'texture/{name}: invalid sizes for {language}')
            if smallest > largest:
                raise SystemExit(f'texture/{name}: min_size exceeds max_size')
            path = fonts[role_map[language]]
            cmap = font_cmap(path)
            missing = sorted({char for char in text if char != '\n' and ord(char) not in cmap})
            if missing:
                codes = ' '.join(f'U+{ord(char):04X}' for char in missing)
                raise SystemExit(f'texture/{name}: {language} font lacks {codes}')
    panels = recipe.get('panels')
    if not isinstance(panels, dict):
        raise SystemExit('texture: panels are missing')
    panel_parts = panels.get('parts')
    if not isinstance(panel_parts, list) or not panel_parts:
        raise SystemExit('texture: panel parts are missing')
    if len(set(panel_parts)) != len(panel_parts):
        raise SystemExit('texture: duplicate panel part')
    for part in panel_parts:
        if not isinstance(part, int) or str(part) not in parts:
            raise SystemExit(f'texture: panel part {part!r} is missing')
    for key in ('title_box', 'body_box'):
        box = panels.get(key)
        if (not isinstance(box, list) or len(box) != 4
                or any(not isinstance(value, int) for value in box)):
            raise SystemExit(f'texture: {key} is invalid')
        x0, y0, x1, y1 = box
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > 228 or y1 > 225:
            raise SystemExit(f'texture: {key} leaves the panel')
    text = panels.get('text')
    if not isinstance(text, dict):
        raise SystemExit('texture: panel text is missing')
    for language in languages:
        if not isinstance(text.get(language), dict):
            raise SystemExit(f'texture: missing {language} panel text')
        for key in ('title', 'button'):
            value = text[language].get(key)
            if not isinstance(value, str) or not value:
                raise SystemExit(f'texture: missing {language} panel {key}')
    for key in ('title_size', 'button_size', 'minimum_size'):
        if not isinstance(panels.get(key), int) or panels[key] < 7:
            raise SystemExit(f'texture: {key} must be at least 7')


def compile_texture(recipe_dir, manifest, languages, fonts, blob, preview_dir):
    recipe = read_json(recipe_dir / manifest['texture']['recipe'])
    validate_texture_recipe(recipe, languages, fonts)
    width, height = recipe['image_size']
    label_report = {}
    language_header = {}
    for language in languages:
        path = fonts[recipe['fonts'][language]]
        labels = []
        preview = None
        if preview_dir:
            preview = Image.new('RGBA', (width, height), (0, 0, 0, 255))
        details = []
        for label in recipe['labels']:
            part = label['part']
            x0, y0, x1, y1 = recipe['parts'][str(part)]
            box_width, box_height = x1 - x0, y1 - y0
            layer, chosen = paint_text_layer(
                box_width, box_height, label['text'][language], path,
                size_limit(label, 'max_size', language),
                size_limit(label, 'min_size', language, 8),
                label['align'], cell=label['cell'])
            data = encode_layer(layer)
            labels.append({'part': part, 'box': [x0, y0, x1, y1],
                           'data_offset': len(blob), 'data_length': len(data)})
            blob.extend(data)
            if preview is not None:
                preview.alpha_composite(layer, (x0, y0))
            details.append({'name': label['name'], 'part': part,
                            'font_size': chosen, 'box': [x0, y0, x1, y1],
                            'layer_bytes': len(data)})
        panels = recipe['panels']
        panel_layers = {}
        for key, stroke in (('title', 2), ('button', 0)):
            box = panels['title_box' if key == 'title' else 'body_box']
            layer, chosen = paint_text_layer(
                box[2] - box[0], box[3] - box[1], panels['text'][language][key],
                path, panels[f'{key}_size'], panels['minimum_size'], 'center',
                stroke=stroke)
            data = encode_layer(layer)
            panel_layers[key] = {'data_offset': len(blob), 'data_length': len(data)}
            blob.extend(data)
            details.append({'name': f'panel-{key}', 'part': -1,
                            'font_size': chosen, 'box': box, 'layer_bytes': len(data)})
        language_header[language] = {
            'labels': labels,
            'title': panel_layers['title'],
            'button': panel_layers['button'],
        }
        label_report[language] = {
            'labels': len(labels),
            'panel_layers': 2,
            'bytes': sum(item['layer_bytes'] for item in details),
            'min_font_size': min(item['font_size'] for item in details),
            'max_font_size': max(item['font_size'] for item in details),
            'details': details,
        }
        if preview is not None:
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview.save(preview_dir / f'{language}-texture-layers.png')
    header = {
        'kind': 'texture',
        'original_sha256': recipe['original_sha256'],
        'size': recipe['size'],
        'path': recipe['path'],
        'image_size': recipe['image_size'],
        'part_count': recipe['part_count'],
        'parts': recipe['parts'],
        'panels': {
            'parts': recipe['panels']['parts'],
            'title_box': recipe['panels']['title_box'],
            'body_box': recipe['panels']['body_box'],
        },
        'languages': language_header,
    }
    return header, label_report


def compile_executable(recipe_dir, manifest):
    recipe = read_json(recipe_dir / manifest['executable']['recipe'])
    if recipe.get('format') != BINARY_FORMAT:
        raise SystemExit(f'executable: unsupported format {recipe.get("format")!r}')
    for key in ('path', 'original_sha256', 'patched_sha256'):
        if not isinstance(recipe.get(key), str) or not recipe[key]:
            raise SystemExit(f'executable: {key} is missing')
    if len(recipe['original_sha256']) != 64 or len(recipe['patched_sha256']) != 64:
        raise SystemExit('executable: invalid SHA-256')
    for key in ('original_size', 'patched_size'):
        if not isinstance(recipe.get(key), int) or recipe[key] <= 0:
            raise SystemExit(f'executable: {key} must be positive')
    patches = recipe.get('patches')
    if not isinstance(patches, list) or not patches:
        raise SystemExit('executable: no patches')
    emitted = []
    for patch in patches:
        name = patch.get('name')
        offset = patch.get('offset')
        original = patch.get('original')
        patched = patch.get('patched')
        if not name or not isinstance(offset, int) or offset < 0:
            raise SystemExit('executable: patch name or offset is invalid')
        if not isinstance(original, str) or not isinstance(patched, str):
            raise SystemExit(f'executable/{name}: patch bytes are missing')
        try:
            original_bytes = bytes.fromhex(original)
            patched_bytes = bytes.fromhex(patched)
        except ValueError:
            raise SystemExit(f'executable/{name}: patch bytes are not hex') from None
        if not original_bytes or len(original_bytes) != len(patched_bytes):
            raise SystemExit(f'executable/{name}: patch lengths differ')
        if offset + len(original_bytes) > recipe['original_size']:
            raise SystemExit(f'executable/{name}: patch leaves the file')
        emitted.append({'name': name, 'offset': offset,
                        'original': original.lower(), 'patched': patched.lower()})
    return {
        'kind': 'binary',
        'path': recipe['path'],
        'original_sha256': recipe['original_sha256'],
        'size': recipe['original_size'],
        'patched_sha256': recipe['patched_sha256'],
        'patched_size': recipe['patched_size'],
        'patches': emitted,
    }


def compile_file_plan(recipe_dir, manifest, languages, fonts, blob, preview_dir):
    disc = manifest['disc']
    if len(disc.get('image_sha256', '')) != 64:
        raise SystemExit('disc: image_sha256 is missing')
    if not isinstance(disc.get('image_size'), int) or disc['image_size'] <= 0:
        raise SystemExit('disc: image_size must be positive')
    files = {
        disc['image']: {
            'kind': 'disc',
            'path': disc['image'],
            'original_sha256': disc['image_sha256'],
            'size': disc['image_size'],
        },
        disc['executable']: compile_executable(recipe_dir, manifest),
    }
    texture, report = compile_texture(recipe_dir, manifest, languages, fonts,
                                      blob, preview_dir)
    if texture['path'] in files:
        raise SystemExit(f'{texture["path"]}: file path collides with the disc or executable')
    files[texture['path']] = texture
    return files, report


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

    if manifest.get('story'):
        story_resources, story_report = compile_story(
            recipe_dir, manifest, languages, fonts, unifont, oracle, blob, preview_dir)
        for rid, resource_header in story_resources.items():
            if rid in header['resources']:
                raise SystemExit(f'{rid}: story page collides with a resource id')
            header['resources'][rid] = resource_header
        report['story'] = story_report

    if not allow_missing:
        for rid, resource in header['resources'].items():
            for language in languages:
                if not resource['expected'].get(language):
                    raise SystemExit(f'{rid}: missing {language} expected hash')

    header['files'], texture_report = compile_file_plan(
        recipe_dir, manifest, languages, fonts, blob, preview_dir)
    report['texture'] = texture_report

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
        'files': sorted(header['files']),
    }, ensure_ascii=False, sort_keys=True))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe-dir', type=Path, default=DEFAULT_RECIPE,
                        help='directory holding manifest.json (default: recipe/)')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                        help='plan path (default: patcher/plan/plan.bin)')
    parser.add_argument('--report', type=Path,
                        help='optional JSON build report; keep it outside patcher/plan/')
    parser.add_argument('--preview-dir', type=Path,
                        help='optional directory for per-language mask previews')
    parser.add_argument('--allow-missing-expected', action='store_true',
                        help='development only: compile without expected hashes')
    args = parser.parse_args()
    report = args.report
    if report is not None and str(report) in ('', '.'):
        report = None
    compile_plan(args.recipe_dir, args.output, report, args.preview_dir,
                 args.allow_missing_expected)
    return 0


if __name__ == '__main__':
    sys.exit(main())
