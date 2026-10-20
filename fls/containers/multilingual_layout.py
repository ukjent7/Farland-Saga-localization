"""Multilingual dialogue rendering and layout engine for Farland Saga II.

Supports English (en-US, word-wrap), Korean (ko-KR, syllable/word-wrap),
and Chinese (zh-CN, character-wrap) into 196x48 2bpp rasters.
"""
import math

from PIL import Image

from fls.containers.unifont_bitmap import Unifont


def glyph_mask(font, ch, size):
    raw = font.glyph(ch)
    width = 8 if len(raw) == 16 else 16
    if ch == '—':
        raw, width = font.full_cell(ch), 16
    cell = Image.frombytes('1', (width, 16), raw)
    return cell.resize((max(1, round(width * size / 16)), size), Image.Resampling.NEAREST)


def wrap_text_pass(font, text: str, size: int, max_width: int = 195, lang: str = 'zh-CN', compact_space: bool = False):
    """Single pass of text wrapping for given parameters."""
    paragraphs = text.split('\n')
    lines = []
    is_korean = lang.startswith('ko')
    is_english = lang.startswith('en')

    # Space glyph adjustment
    base_space = glyph_mask(font, ' ', size)
    space_width = max(3, round(base_space.width * (0.6 if compact_space else 1.0)))
    space_glyph = base_space.crop((0, 0, space_width, base_space.height))

    for para in paragraphs:
        if not para:
            lines.append([])
            continue

        if is_english:
            words = para.split(' ')
            line = []
            used = 0

            for word in words:
                if not word:
                    continue
                word_glyphs = [glyph_mask(font, ch, size) for ch in word]
                word_width = sum(g.width for g in word_glyphs)
                prefix_width = space_glyph.width if line else 0

                if line and (used + prefix_width + word_width > max_width):
                    lines.append(line)
                    line = []
                    used = 0
                    prefix_width = 0

                if line:
                    line.append(space_glyph)
                    used += space_glyph.width

                if word_width > max_width:
                    for g in word_glyphs:
                        if line and (used + g.width > max_width):
                            lines.append(line)
                            line = []
                            used = 0
                        line.append(g)
                        used += g.width
                else:
                    line.extend(word_glyphs)
                    used += word_width

            if line:
                lines.append(line)

        elif is_korean:
            # Korean supports word-wrap first, but can break at syllable if line overflow occurs
            words = para.split(' ')
            line = []
            used = 0

            for word in words:
                if not word:
                    continue
                word_glyphs = [glyph_mask(font, ch, size) for ch in word]
                word_width = sum(g.width for g in word_glyphs)
                prefix_width = space_glyph.width if line else 0

                if line and (used + prefix_width + word_width > max_width):
                    # Try word wrap first
                    if used + prefix_width + word_width <= max_width:
                        pass
                    else:
                        lines.append(line)
                        line = []
                        used = 0
                        prefix_width = 0

                if line:
                    line.append(space_glyph)
                    used += space_glyph.width

                for g in word_glyphs:
                    if line and (used + g.width > max_width):
                        lines.append(line)
                        line = []
                        used = 0
                    line.append(g)
                    used += g.width

            if line:
                lines.append(line)

        else:
            # Chinese/Japanese: character wrap
            line = []
            used = 0
            for ch in para:
                glyph = glyph_mask(font, ch, size)
                if line and used + glyph.width > max_width:
                    lines.append(line)
                    line = []
                    used = 0
                line.append(glyph)
                used += glyph.width
            if line:
                lines.append(line)

    return lines


def render_dialogue(font, text: str, lang: str = 'zh-CN'):
    """Render dialogue text with shadow into 196x48 2bpp layout."""
    # Text variants: 1) original text with explicit '\n'; 2) collapsed text with '\n' removed/spaced
    variants = [text]
    if '\n' in text:
        if lang.startswith(('en', 'ko')):
            collapsed = ' '.join(p.strip() for p in text.split('\n') if p.strip())
        else:
            collapsed = ''.join(p.strip() for p in text.split('\n') if p.strip())
        variants.append(collapsed)

    for size in (16, 14, 12):
        for compact in (False, True):
            for txt in variants:
                lines = wrap_text_pass(font, txt, size, max_width=195, lang=lang, compact_space=compact)
                if len(lines) <= 3:
                    mask = Image.new('1', (196, 48))
                    for n, line in enumerate(lines):
                        x = 0
                        for glyph in line:
                            mask.paste(glyph, (x, n * 16))
                            x += glyph.width

                    shadow = Image.new('1', mask.size)
                    shadow.paste(mask, (1, 1))
                    shadow.paste(0, (0, 16, 196, 17))
                    shadow.paste(0, (0, 32, 196, 33))

                    image = Image.new('L', mask.size)
                    image.paste(85, (0, 0), shadow)
                    image.paste(170, (0, 0), mask)

                    span = max((y // 16 * 196 + x + 1 for y in range(48) for x in range(196)
                                if image.getpixel((x, y))), default=0)
                    return image, {
                        'font_pixels': size,
                        'lines': len(lines),
                        'timing_characters': math.ceil(span / 14),
                        'foreground_index': 2,
                        'shadow_index': 1,
                        'scaled': size != 16,
                    }

    raise ValueError(f'Text does not fit three lines at 12 pixels or larger [{lang}]: {text!r}')
