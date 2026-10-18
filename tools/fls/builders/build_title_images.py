"""Render trilingual titles into the original indexed SPR regions.

All layouts have fixed dimensions. Missing characters or overflowing text
stop the build. Original palettes, numeric sprites and file sizes survive.
"""
import argparse
import json
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from fontTools.ttLib import TTFont
from build_alpha import WORK, SOURCE, local_path, digest
from path_compat import REPO
from title_sprites import decode
from workspace_assets import save_preview
from candidate import Candidate

TRANSLATIONS = REPO/'translation/titles.json'
FONTS = {'zh-CN': 'C:/Windows/Fonts/simhei.ttf',
         'ko-KR': 'C:/Windows/Fonts/malgunbd.ttf',
         'en-US': 'C:/Windows/Fonts/ariali.ttf'}
HEADERS = {'zh-CN': {'stage': '关卡', 'final': '最终'},
           'ko-KR': {'stage': '스테이지', 'final': '최종'}}


def render(text, font_path, size, slant):
    font = ImageFont.truetype(str(font_path), size)
    bbox = font.getbbox(text)
    ink = Image.new('L', (bbox[2]-bbox[0], bbox[3]-bbox[1]))
    ImageDraw.Draw(ink).text((-bbox[0], -bbox[1]), text, font=font, fill=255)
    assert ink.getbbox(), text
    ink = ink.crop(ink.getbbox())
    if slant:
        width, height = ink.size
        italic = Image.new('L', (width+math.ceil(slant*(height-1)), height))
        for y in range(height):
            italic.paste(ink.crop((0, y, width, y+1)), (round(slant*(height-1-y)), y))
        ink = italic.crop(italic.getbbox())
    padded = Image.new('L', (ink.width+2, ink.height+2))
    padded.paste(ink, (1, 1))
    outline = padded.filter(ImageFilter.MaxFilter(3))
    return padded, outline


def fit(text, short, language, font_path, frame, style):
    width, height = frame
    slant = 0.18 if language != 'en-US' else 0
    for candidate in [text] + ([short] if short else []):
        for size in range(style['preferred_size'], style['minimum_size']-1, -1):
            ink, outline = render(candidate, font_path, size, slant)
            if ink.width <= width-2 and ink.height <= height:
                return candidate, size, ink, outline
    raise ValueError(f'Overflow: {language} {text!r} needs more than {width}x{height} '
                     f'at minimum font size {style["minimum_size"]}')


def indexed(ink, outline, original):
    palette = original.getpalette()
    image = Image.new('P', original.size, 0)
    image.putpalette(palette)
    image.info['transparency'] = 0
    colors = [tuple(palette[i*3:i*3+3]) for i in range(256)]
    gray = {v: min(range(1, 256), key=lambda i: sum((c-v)**2 for c in colors[i]))
            for v in range(256)}
    left, top = (image.width-ink.width)//2, (image.height-ink.height)//2
    for y in range(ink.height):
        for x in range(ink.width):
            if outline.getpixel((x, y)) >= 96:
                # A dark outline and light face, quantized to the original palette.
                brightness = 32 + ink.getpixel((x, y))*223//255
                image.putpixel((left+x, top+y), gray[brightness])
    bbox = image.getbbox()
    assert bbox and bbox[0] > 0 and bbox[2] < image.width
    return image


def build(language, stage):
    draft = json.loads(TRANSLATIONS.read_text(encoding='utf-8'))
    font_path = local_path(FONTS[language])
    cmap = TTFont(str(font_path)).getBestCmap()
    changes, reports, previews = {}, [], []
    for row in draft['rows']:
        original = (SOURCE/row['file']).read_bytes()
        images = decode(original)
        translation = row['translations'][language]
        quoted = lambda text: f'"{text}"' if language == 'en-US' else f'「{text}」'
        requests = {'title': quoted(translation)}
        if language in HEADERS:
            requests.update({name: value for name, value in HEADERS[language].items() if name in images})
        patched = bytearray(original)
        final_images = {key: entry['image'] for key, entry in images.items()}
        for name, text in requests.items():
            short = row['short_translations'].get(language) if name == 'title' else None
            short = quoted(short) if short else None
            missing = sorted({ord(ch) for ch in text+(short or '') if not ch.isspace() and ord(ch) not in cmap})
            if missing:
                raise ValueError(f'{row["id"]}: missing font glyphs: {missing}')
            entry = images[name]
            style = row['style']
            assert style['stroke_pixels'] == 1 and style['alignment'] == 'center'
            chosen, size, ink, outline = fit(text, short, language, font_path, entry['image'].size, style)
            image = indexed(ink, outline, entry['image'])
            offset, length = entry['offset'], entry['size']
            patched[offset:offset+length] = image.tobytes()
            final_images[name] = image
            reports.append({'id': row['id'], 'file': row['file'], 'region': name,
                            'text': chosen, 'font_pixels': size, 'frame': list(image.size),
                            'ink_with_outline': list(ink.size),
                            'reduced_font': size < style['preferred_size'],
                            'used_short_translation': chosen != text, 'overflow': False,
                            'missing_glyphs': [], 'bounds': list(image.getbbox())})
        assert len(patched) == len(original) and patched[:512] == original[:512]
        if 'number' in images:
            entry = images['number']
            assert patched[entry['offset']:entry['offset']+entry['size']] == entry['image'].tobytes()
        redecoded = decode(bytes(patched))
        assert set(redecoded) == set(final_images)
        for name, expected in final_images.items():
            assert redecoded[name]['image'].tobytes() == expected.tobytes(), (row['id'], name)
        changes[row['file']] = bytes(patched)
        target = stage/'resources'/row['file']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(patched)
        previews.append((row['id'], final_images))
    report = {'language': language, 'title_files': len(changes), 'regions': len(reports),
              'font_source': font_path.name, 'font_sha256': digest(font_path),
              'overflow_count': 0, 'missing_glyph_count': 0,
              'preserved_palettes_and_file_sizes': True, 'preserved_number_sprites': True,
              'decode_encode_pixel_check': 'passed', 'translation_status': draft['status'],
              'rows': reports}
    (stage/'title_layout_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (stage/'inputs').mkdir(exist_ok=True)
    (stage/'inputs/titles.json').write_bytes(TRANSLATIONS.read_bytes())
    sheet = Image.new('RGB', (660, 25*34), '#6d7060')
    draw = ImageDraw.Draw(sheet)
    for i, (identifier, images) in enumerate(previews):
        x, y = (i//25)*330, (i%25)*34
        draw.text((x+2, y+8), identifier[-2:], fill='white')
        title = images['title'].convert('RGBA')
        sheet.paste(title, (x+30, y+4), title)
    save_preview(stage/'title_contact_sheet.png',sheet)
    return changes, report


def main():
    parser = argparse.ArgumentParser(description='Build translated title images into a fresh candidate.')
    parser.add_argument('--base', required=True, help='Ready candidate to clone')
    parser.add_argument('--output', required=True, help='New output directory under staging/ or analysis/')
    parser.add_argument('--language', required=True, choices=tuple(FONTS))
    parser.add_argument('--resources-only', action='store_true')
    args = parser.parse_args()

    candidate = Candidate(args.base, args.output)
    changes, report = build(args.language, candidate.stage)
    candidate.resources.update(changes)
    candidate.report['title_image_build'] = {
        'language': args.language,
        'title_files': report['title_files'],
        'regions': report['regions'],
        'translation_status': report['translation_status'],
        'layout_report': 'title_layout_report.json',
        'layout_report_sha256': digest(candidate.stage / 'title_layout_report.json'),
    }
    candidate.finalize(resources_only=args.resources_only)
    print(json.dumps(candidate.report['title_image_build'], ensure_ascii=False))


if __name__ == '__main__':
    main()
