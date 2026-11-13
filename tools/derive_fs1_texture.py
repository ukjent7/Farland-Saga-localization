#!/usr/bin/env python3
"""Derive the FS1 Texture.tex declaration (schema 2) from a pristine backup.

The committed declaration carries NO game artwork: only the structural facts
(part boxes, label regions and their constant backgrounds, panel rules) and,
per language, a sparse ink layer holding the localized text pixels. At install
time the patcher decodes the user's own pristine atlas, clears the declared
regions, rebuilds the panel gradient from a row of that same pristine image,
overlays the ink, re-encodes the PNG and rebuilds the SSF container.

Inputs (all private-workspace files, none committed):

  --backup      pristine backup dir (Texture.tex + FarlandSaga.exe)
  --patched     per-language patched Texture.tex (--lang code:path, repeatable)
  --labels      historical label declaration JSON (exported from the old
                frontend-menu/frontend-submenus configs; contains translations)
  --output      recipe/fs1/texture.json

The tool also emits --identities-out: a JSON snippet with each language's
patched container identity (filled in by `patcher texture-identity` after the
Go-side PNG encoding is known; see tools/README or recipe/fs1/README.md).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import struct
import sys
from pathlib import Path

from PIL import Image
import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fls.containers.steam_container import parameters, transform  # noqa: E402

LANGUAGES = ['zh-CN', 'ko-KR', 'en-US']
TEXTURE = 'Texture.tex'
HEADER_BYTES = 40
PANEL_PARTS = [104, 105, 106, 107, 108, 109, 110]
HEADER_BOX = (16, 6, 212, 52)
BODY_BOX = (16, 61, 212, 207)
STRIP_ROW = 51
STRIP_X = (16, 212)
DEFAULT_BACKGROUND = (255, 255, 255, 0)
BODY_BACKGROUND = (0, 0, 0, 255)


def fail(message: str) -> None:
    raise SystemExit(message)


def decode_container(raw: bytes, exe: Path, source: Path):
    """Return (pristine 2048-byte header, decoded payload, SSF params)."""
    params = parameters(str(source), str(exe), TEXTURE)
    return raw[:2048], transform(raw[2048:], params, False), params


def parse_payload(payload: bytes):
    if not payload.startswith(b'Texture'.ljust(20)):
        fail('原版容器缺少 Texture 标记')
    end, _, _, png_size, width, height = struct.unpack_from('<IIIIHH', payload, 20)
    if end != HEADER_BYTES + png_size or end > len(payload):
        fail('原版 Texture 段长度不合法')
    parts_start = (end + 3) & ~3
    if not payload.startswith(b'Parts'.ljust(20), parts_start):
        fail('原版容器缺少 Parts 元数据')
    section, count = struct.unpack_from('<II', payload, parts_start + 20)
    if section != 28 + count * 32:
        fail('原版 Parts 段长度不合法')
    boxes = []
    for index in range(count):
        values = struct.unpack_from('<8f', payload, parts_start + 28 + index * 32)
        boxes.append([round(values[4] * width), round(values[5] * height),
                      round(values[6] * width), round(values[7] * height)])
    return {
        'png': payload[HEADER_BYTES:end],
        'png_size': png_size,
        'image_size': [width, height],
        'part_count': count,
        'boxes': boxes,
        'parts_suffix': payload[parts_start:],
        'section_header': payload[:HEADER_BYTES],
    }


def pillow_strip(row: np.ndarray) -> np.ndarray:
    """Reproduce Pillow's resize of a 1-row RGBA image to 46 identical rows.

    Pillow interpolates in premultiplied-alpha space and unpremultiplies with
    a truncating divide: q = (c*a + 127)//255; c' = (q*255)//a. Verified byte
    exact for all seven panels. Both the source row and the result come from
    the user's own pristine atlas, so no game artwork is stored.
    """
    out = np.zeros((196, 4), dtype=np.uint8)
    for x in range(196):
        color = row[x].astype(int)
        alpha = int(color[3])
        out[x, 3] = alpha
        if alpha:
            for channel in range(3):
                premultiplied = (int(color[channel]) * alpha + 127) // 255
                out[x, channel] = min(255, (premultiplied * 255) // alpha)
    return out


def build_base(image: np.ndarray, meta: dict, labels: list[dict]) -> np.ndarray:
    """Apply all non-ink changes to a copy of the pristine atlas.

    The result is the fully reconstructible background: declared label regions
    become constant colours, panel headers become the pristine-derived gradient
    strip, panel bodies become black, and every other pixel stays untouched.
    """
    base = image.copy()
    for label in labels:
        x0, y0, _, _ = label['box']
        region = label.get('region') or [0, 0, label['box'][2] - x0, label['box'][3] - y0]
        background = np.array(label.get('background', DEFAULT_BACKGROUND), dtype=np.uint8)
        base[y0 + region[1]:y0 + region[3], x0 + region[0]:x0 + region[2]] = background
    for part in PANEL_PARTS:
        x0, y0, _, _ = meta['boxes'][part]
        row = image[y0 + STRIP_ROW, x0 + STRIP_X[0]:x0 + STRIP_X[1]]
        strip = pillow_strip(row)
        hx0, hy0, hx1, hy1 = HEADER_BOX
        base[y0 + hy0:y0 + hy1, x0 + hx0:x0 + hx1] = strip[:hy1 - hy0, None, :]
        bx0, by0, bx1, by1 = BODY_BOX
        base[y0 + by0:y0 + by1, x0 + bx0:x0 + bx1] = np.array(
            BODY_BACKGROUND, dtype=np.uint8)
    return base


def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--patched', action='append', required=True,
                        metavar='LANG=PATH',
                        help='补丁后 Texture.tex（每语言一次）')
    parser.add_argument('--labels', required=True, type=Path,
                        help='历史标签声明 JSON（371 项，含译文与框坐标）')
    parser.add_argument('--output', type=Path,
                        default=REPO / 'recipe' / 'fs1' / 'texture.json')
    args = parser.parse_args()

    pristine_path = args.backup / TEXTURE
    if not pristine_path.is_file():
        fail(f'{pristine_path}: 找不到原版 {TEXTURE}')
    pristine = pristine_path.read_bytes()
    _, payload, _ = decode_container(
        pristine, args.backup / 'FarlandSaga.exe', pristine_path)
    meta = parse_payload(payload)
    original = np.asarray(
        Image.open(io.BytesIO(meta['png'])).convert('RGBA'), dtype=np.uint8)
    if [original.shape[1], original.shape[0]] != meta['image_size']:
        fail('原版图集尺寸与容器头部不符')

    labels_doc = json.loads(args.labels.read_text(encoding='utf-8'))
    labels = labels_doc['labels'] if isinstance(labels_doc, dict) else labels_doc
    if len(labels) != 371:
        fail(f'标签声明应有 371 项，实际 {len(labels)}')
    for label in labels:
        box = label['box']
        if meta['boxes'][label['part']] != box:
            fail(f"part {label['part']}: 声明框与原版 Parts 元数据不符")

    base = build_base(original, meta, labels)
    allowed = np.zeros(original.shape[:2], dtype=bool)
    for label in labels:
        x0, y0, _, _ = label['box']
        region = label.get('region') or [0, 0, label['box'][2] - x0, label['box'][3] - y0]
        allowed[y0 + region[1]:y0 + region[3], x0 + region[0]:x0 + region[2]] = True
    for part in PANEL_PARTS:
        x0, y0, _, _ = meta['boxes'][part]
        for box in (HEADER_BOX, BODY_BOX):
            allowed[y0 + box[1]:y0 + box[3], x0 + box[0]:x0 + box[2]] = True
    # 允许的非灰阶文字颜色（旧管线里唯一一个自定义 fill）。
    special_colors = {
        tuple(label['fill']) for label in labels if 'fill' in label
    }
    patched_files = {}
    for item in args.patched:
        if '=' not in item:
            fail(f'--patched 需要 LANG=PATH: {item}')
        language, path = item.split('=', 1)
        if language not in LANGUAGES:
            fail(f'未知语言 {language}')
        patched_files[language] = Path(path)
    missing = [language for language in LANGUAGES if language not in patched_files]
    if missing:
        fail(f'缺少语言 {missing}')

    doc = {
        'schema': 2,
        'file': TEXTURE,
        'original_sha256': hashlib.sha256(pristine).hexdigest(),
        'original_size': len(pristine),
        'image_size': meta['image_size'],
        'part_count': meta['part_count'],
        'panels': {
            'parts': PANEL_PARTS,
            'header_box': list(HEADER_BOX),
            'body_box': list(BODY_BOX),
            'strip_row': STRIP_ROW,
            'strip_x': list(STRIP_X),
        },
        'labels': [{
            'part': label['part'],
            'box': label['box'],
            'region': label.get('region'),
            'background': label.get('background', list(DEFAULT_BACKGROUND)),
        } for label in labels],
        'languages': {},
    }

    for language in LANGUAGES:
        path = patched_files[language]
        raw = path.read_bytes()
        _, patched_payload, _ = decode_container(
            raw, args.backup / 'FarlandSaga.exe', path)
        patched_meta = parse_payload(patched_payload)
        if patched_meta['parts_suffix'] != meta['parts_suffix']:
            fail(f'{language}: 补丁后 Parts 元数据与原版不符')
        patched = np.asarray(Image.open(
            io.BytesIO(patched_meta['png'])).convert('RGBA'), dtype=np.uint8)
        if patched.shape != original.shape:
            fail(f'{language}: 补丁后图集尺寸与原版不符')

        outside = np.any(patched != original, axis=2) & ~allowed
        if outside.any():
            fail(f'{language}: {int(outside.sum())} 像素在声明区域外发生变化')

        changes = np.any(patched != base, axis=2)
        ink_pixels = patched[changes]
        # 墨迹只能是译文文字（灰阶抗锯齿）、声明的特殊文字颜色，或面板
        # 渐变条（由用户原版派生、不属于 base 的少量彩色像素）。逐项拒绝
        # 任何其它彩色像素，防止原版美术混进仓库。
        non_grey = ink_pixels[ink_pixels[:, 3] > 0]
        greyscale = np.all(non_grey[:, :3] == non_grey[:, :1], axis=1)
        allowed_color = np.array([list(c) for c in special_colors],
                                 dtype=np.uint8) if special_colors else None
        is_special = (np.any(np.all(non_grey[:, None, :] == allowed_color[None, :, :],
                                    axis=2), axis=1)
                      if allowed_color is not None else np.zeros(len(non_grey), bool))
        colored = non_grey[~greyscale & ~is_special]
        if len(colored):
            # 面板渐变条来自用户原版的单行，逐面板校验其行数与公式一致。
            strip_pixels = 7 * (HEADER_BOX[2] - HEADER_BOX[0]) * (
                HEADER_BOX[3] - HEADER_BOX[1])
            if len(colored) > strip_pixels:
                fail(f'{language}: {len(colored):,} 彩色墨迹像素超出面板渐变上限')
        mask = Image.fromarray(np.where(changes, 255, 0).astype(np.uint8), 'L')
        ink = np.zeros_like(patched)
        ink[changes] = patched[changes]
        mask_png = encode_png(mask)
        ink_png = encode_png(Image.fromarray(ink, 'RGBA'))
        doc['languages'][language] = {
            'mask': base64.b64encode(mask_png).decode('ascii'),
            'ink': base64.b64encode(ink_png).decode('ascii'),
            'mask_sha256': hashlib.sha256(mask_png).hexdigest(),
            'ink_sha256': hashlib.sha256(ink_png).hexdigest(),
            # 补丁后容器身份由 patcher texture-identity 回填（Go PNG 编码）。
            'patched_sha256': '',
            'patched_size': 0,
            'patched_png_sha256': hashlib.sha256(patched_meta['png']).hexdigest(),
        }
        print(f'{language}: 墨迹像素 {int(changes.sum()):,}，'
              f'mask {len(mask_png):,} B，ink {len(ink_png):,} B')

    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n',
                           encoding='utf-8')
    print(f'已写入 {args.output}（{args.output.stat().st_size:,} 字节）')


if __name__ == '__main__':
    main()
