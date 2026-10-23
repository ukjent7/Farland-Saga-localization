"""Render the seven opening-CG character cards into 8-bit tilemaps.

The cards are 160x240 (Radish is 320x120) tilemap resources with an embedded
palette.  The recipe stores the tile geometry and the palette indices used by
the artwork; the original header and palette stay untouched.  Only the tile map
and the tile graphics are generated, so the installer can write those regions
back into the user's pristine resource.
"""
import struct

from PIL import Image, ImageDraw

WHITE = 1
CELL = 8
BODY_HEIGHT = {'zh-CN': 16, 'ko-KR': 16, 'en-US': 12}


def glyph_mask(font, character, height=16):
    raw = font.get(ord(character))
    if raw is None:
        raise ValueError(f'Unifont has no glyph for U+{ord(character):04X} ({character!r})')
    if len(raw) == 32:
        rows = [int.from_bytes(raw[i * 2:i * 2 + 2], 'big') for i in range(16)]
        source = 16
    else:
        rows = list(raw)
        source = 8
    image = Image.new('L', (source, 16), 0)
    pixels = image.load()
    for y in range(16):
        for x in range(source):
            if rows[y] & (1 << (source - 1 - x)):
                pixels[x, y] = 255
    if height != 16:
        size = (max(1, round(source * height / 16)), height)
        if height < 16:
            image = image.resize(size, Image.Resampling.BOX).point(
                lambda value: 255 if value >= 96 else 0)
        else:
            image = image.resize(size, Image.Resampling.NEAREST)
    return image, image.width


def text_mask(font, text, height=16, spacing=0):
    cells = []
    total = 0
    for character in text:
        image, advance = glyph_mask(font, character, height)
        cells.append(image)
        total += advance + spacing
    total = max(1, total - spacing)
    out = Image.new('L', (total, height), 0)
    x = 0
    for image in cells:
        out.paste(image, (x, 0))
        x += image.width + spacing
    return out


def fit(mask, max_width):
    if mask.width <= max_width:
        return mask
    return mask.resize((max_width, mask.height), Image.Resampling.BOX).point(
        lambda value: 255 if value >= 128 else 0)


def draw_mask(image, mask, x, y, color, shadow=None, shadow_offset=(1, 1)):
    pixels = image.load()
    if shadow is not None:
        sx, sy = shadow_offset
        for yy in range(mask.height):
            for xx in range(mask.width):
                if mask.getpixel((xx, yy)):
                    gx, gy = x + xx + sx, y + yy + sy
                    if 0 <= gx < image.width and 0 <= gy < image.height:
                        pixels[gx, gy] = shadow
    for yy in range(mask.height):
        for xx in range(mask.width):
            if mask.getpixel((xx, yy)):
                gx, gy = x + xx, y + yy
                if 0 <= gx < image.width and 0 <= gy < image.height:
                    pixels[gx, gy] = color


def draw_colon(image, x, y, color, height=16):
    pixels = image.load()
    rows = (y + 4, y + 5, y + 10, y + 11) if height >= 16 else (y + 3, y + 4, y + 8, y + 9)
    for yy in rows:
        for xx in (x, x + 1):
            if 0 <= xx < image.width and 0 <= yy < image.height:
                pixels[xx, yy] = color


def rotate_paste(image, mask, x, y, angle, color, shade):
    rotated = mask.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    rotated = rotated.point(lambda value: 255 if value >= 96 else 0)
    if shade is not None:
        draw_mask(image, rotated, x - rotated.width // 2 + 2,
                  y - rotated.height // 2 + 2, shade)
    draw_mask(image, rotated, x - rotated.width // 2, y - rotated.height // 2, color)


def _blank(card):
    width = card['tilemap']['tiles_x'] * CELL
    height = card['tilemap']['tiles_y'] * CELL
    return Image.new('L', (width, height), card['colors']['black'])


def standard_card(font, card, language):
    image = _blank(card)
    shadow = card['colors']['shadow']
    accent = card['colors']['accent']
    shade = card['colors']['shade']
    body = BODY_HEIGHT[language]
    deco_mask = text_mask(font, card['deco'][language], card['deco_height'][language],
                          spacing=2)
    spots = [(-10, 10, -26), (image.width - 40, 5, 20),
             (-20, image.height - 100, 24), (image.width - 45, image.height - 70, -20)]
    for x, y, angle in spots:
        rotate_paste(image, deco_mask, x, y, angle, accent, shade)
    title_mask = text_mask(font, card['title'][language], card['title_height'],
                           spacing=card['title_spacing'][language])
    draw_mask(image, title_mask, (image.width - title_mask.width) // 2,
              card['title_y'], WHITE, shadow, (2, 2))
    for (label, value), y in zip(card['lines'][language], card['ys']):
        if label:
            label_mask = text_mask(font, label, body)
            draw_mask(image, label_mask, 76 - label_mask.width, y, WHITE, shadow)
            draw_colon(image, 80, y, WHITE, body)
        value_mask = fit(text_mask(font, value, body), 156 - 88)
        draw_mask(image, value_mask, 88, y, WHITE, shadow)
    return image


def wide_card(font, card, language):
    image = _blank(card)
    shadow = card['colors']['shadow']
    accent = card['colors']['accent']
    shade = card['colors']['shade']
    body = BODY_HEIGHT[language]
    deco_mask = text_mask(font, card['deco'][language], card['deco_height'][language],
                          spacing=2)
    for x, y, angle in [(-10, 5, -22), (image.width - 40, 5, 20),
                        (-10, image.height - 50, 20), (image.width - 50, image.height - 55, -18)]:
        rotate_paste(image, deco_mask, x, y, angle, accent, shade)
    title_mask = text_mask(font, card['title'][language], card['title_height'],
                           spacing=card['title_spacing'][language])
    draw_mask(image, title_mask, (image.width - title_mask.width) // 2,
              card['title_y'], WHITE, shadow, (2, 2))
    for (label, value), y in zip(card['left'][language], [62, 82, 101]):
        label_mask = text_mask(font, label, body)
        draw_mask(image, label_mask, 70 - label_mask.width, y, WHITE, shadow)
        draw_colon(image, 74, y, WHITE, body)
        value_mask = fit(text_mask(font, value, body), 158 - 82)
        draw_mask(image, value_mask, 82, y, WHITE, shadow)
    for (label, value), y in zip(card['right'][language], [61, 85, 101]):
        label_mask = text_mask(font, label, body)
        draw_mask(image, label_mask, 242 - label_mask.width, y, WHITE, shadow)
        draw_colon(image, 246, y, WHITE, body)
        value_mask = fit(text_mask(font, value, body), 316 - 256)
        draw_mask(image, value_mask, 256, y, WHITE, shadow)
    return image


def lulu_card(font, card, language):
    image = _blank(card)
    shadow = card['colors']['shadow']
    red = card['colors']['red']
    green = card['colors']['green']
    dark = card['colors']['dark']
    body = BODY_HEIGHT[language]

    def carrot(cx, cy, size, angle):
        layer = Image.new('RGBA', (size * 4, size * 4), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        centre = size * 2
        draw.polygon([(centre - size // 2, centre - size // 2),
                      (centre + size // 2, centre - size // 2),
                      (centre, centre + size)], fill=(255, 0, 0, 255))
        for dx in (-size // 2, 0, size // 2):
            draw.line([(centre, centre - size // 2), (centre + dx, centre - size * 1.3)],
                      fill=(0, 255, 0, 255), width=max(2, size // 5))
        layer = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
        pixels = layer.load()
        for y in range(layer.height):
            for x in range(layer.width):
                r, g, b, a = pixels[x, y]
                if a < 96:
                    continue
                gx, gy = cx + x - layer.width // 2, cy + y - layer.height // 2
                if 0 <= gx < image.width and 0 <= gy < image.height:
                    image.putpixel((gx, gy), red if r > g else (green if g > r else dark))

    for cx, cy, size, angle in [(28, 25, 11, 20), (132, 18, 10, -25), (18, 115, 11, -15),
                                (142, 108, 10, 25), (38, 210, 10, -20), (124, 212, 11, 20)]:
        carrot(cx, cy, size, angle)
    title_mask = text_mask(font, card['title'][language], card['title_height'],
                           spacing=card['title_spacing'][language])
    draw_mask(image, title_mask, (image.width - title_mask.width) // 2,
              card['title_y'], WHITE, shadow, (2, 2))
    for (label, value), y in zip(card['lines'][language], card['ys']):
        if label:
            label_mask = text_mask(font, label, body)
            draw_mask(image, label_mask, 76 - label_mask.width, y, WHITE, shadow)
            draw_colon(image, 80, y, WHITE, body)
        value_mask = fit(text_mask(font, value, body), 156 - 88)
        draw_mask(image, value_mask, 88, y, WHITE, shadow)
    return image


def render(font, card, language):
    layout = card['layout']
    if layout == 'wide':
        return wide_card(font, card, language)
    if layout == 'lulu':
        return lulu_card(font, card, language)
    return standard_card(font, card, language)


def encode(card, image):
    tilemap = card['tilemap']
    tiles_x, tiles_y = tilemap['tiles_x'], tilemap['tiles_y']
    if image.size != (tiles_x * CELL, tiles_y * CELL):
        raise ValueError(f'{card["resource"]}: rendered size {image.size} != tilemap size')
    contents = {}
    order = []
    entries = []
    for cy in range(tiles_y):
        for cx in range(tiles_x):
            block = bytes(image.getpixel((cx * CELL + dx, cy * CELL + dy))
                          for dy in range(CELL) for dx in range(CELL))
            if block not in contents:
                contents[block] = 2 * len(order)
                order.append(block)
            entries.append(contents[block])
    capacity = tilemap['size_field'] // 64
    if len(order) > capacity:
        raise ValueError(f'{card["resource"]}: {len(order)} tiles for {capacity} slots')
    if any(entry >= 2 * capacity or entry & 1 for entry in entries):
        raise ValueError(f'{card["resource"]}: map entry outside the tile area')
    map_bytes = bytearray(2 * tiles_x * tiles_y)
    struct.pack_into(f'>{tiles_x * tiles_y}H', map_bytes, 0, *entries)
    graphics = bytearray(tilemap['size_field'])
    for index, block in enumerate(order):
        graphics[index * 64:(index + 1) * 64] = block
    return bytes(map_bytes), bytes(graphics), len(order)


def preview(card, image, path, scale=3):
    """Write a preview using the recipe's logical colours."""
    palette = [(0, 0, 0)] * 256
    colors = card['colors']
    accent = tuple(card['accent_rgb'])
    palette[colors['black']] = (0, 0, 0)
    palette[colors['shadow']] = (80, 80, 80)
    palette[colors['accent']] = accent
    palette[colors['shade']] = tuple(int(value * 0.55) for value in accent)
    palette[WHITE] = (255, 255, 255)
    if card['layout'] == 'lulu':
        palette[colors['red']] = accent
        palette[colors['green']] = (0, 80, 0)
        palette[colors['dark']] = (60, 0, 0)
    out = Image.new('RGB', image.size)
    out.putdata([palette[value] for value in image.get_flattened_data()])
    out.resize((image.width * scale, image.height * scale),
               Image.Resampling.NEAREST).save(path)
