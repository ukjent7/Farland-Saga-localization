#!/usr/bin/env python3
"""Derive the FS1 Texture.tex declaration from a pristine backup + payloads.

Run on a machine with the game installed (the pristine backup and the
language payloads are the inputs; nothing here runs at install time):

    FARLAND_WORK=... uv.sh run --with pefile python tools/derive_fs1_texture.py \
        --backup "/mnt/e/.../FarlandSagaPatcher-backup" \
        --payloads recipe/fs1/payload --output recipe/fs1/texture.json

The tool decodes the pristine SSF container, reconstructs each language's
patched container from its diff object (kept in the private workspace; the
repository no longer carries the ZIPs), verifies the manifest
hash, and records the localized PNG plus the shared Parts metadata. The
compiler embeds those bytes into the plan's texture operation.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import struct
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fls.containers.steam_container import parameters, transform  # noqa: E402

LANGUAGES = ['zh-CN', 'ko-KR', 'en-US']
TEXTURE = 'Texture.tex'


def fail(message: str) -> None:
    raise SystemExit(message)


def rebuild_container(container_gz: bytes, pristine: bytes) -> bytes:
    """Apply one diff object's records to the pristine container."""
    index = container_gz.index(b'}\n') + 2
    meta = json.loads(container_gz[:index])
    body = container_gz[index:]
    out = bytearray(pristine)
    position = 0
    while position < len(body):
        offset, length = struct.unpack_from('<QI', body, position)
        position += 12
        out[offset:offset + length] = body[position:position + length]
        position += length
    del out[meta['size']:]
    return bytes(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path,
                        help='原版备份目录（含 Texture.tex 与 FarlandSaga.exe）')
    parser.add_argument('--payloads', required=True, type=Path,
                        help='历史语言载荷目录（含 diff 对象；ZIP 已从仓库移除，'
                             '放在私有工作区即可）')
    parser.add_argument('--output', type=Path,
                        default=REPO / 'recipe' / 'fs1' / 'texture.json')
    args = parser.parse_args()

    pristine_path = args.backup / TEXTURE
    if not pristine_path.is_file():
        fail(f'{pristine_path}: 找不到原版 {TEXTURE}')
    pristine = pristine_path.read_bytes()
    params = parameters(str(pristine_path),
                        str(args.backup / 'FarlandSaga.exe'), TEXTURE)
    payload = transform(pristine[2048:], params)
    end = struct.unpack_from('<I', payload, 20)[0]
    suffix = payload[(end + 3) & ~3:]
    if not suffix.startswith(b'Parts'.ljust(20)):
        fail('原版容器缺少 Parts 元数据')

    doc = {
        'schema': 1,
        'file': TEXTURE,
        'original_sha256': hashlib.sha256(pristine).hexdigest(),
        'original_size': len(pristine),
        'suffix_size': len(suffix),
        'languages': {},
    }
    for zip_path in sorted(args.payloads.glob('*.zip')):
        archive = zipfile.ZipFile(zip_path)
        try:
            manifest = json.loads(archive.read('manifest.json'))
            language = manifest['language']
            entry = next((item for item in manifest['files']
                          if item['path'] == TEXTURE), None)
            if entry is None:
                fail(f'{zip_path.name}: 载荷缺少 {TEXTURE}')
            container_gz = gzip.decompress(archive.read(entry['object']))
        finally:
            archive.close()
        rebuilt = rebuild_container(container_gz, pristine)
        if hashlib.sha256(rebuilt).hexdigest() != entry['patched_sha256']:
            fail(f'{language}: 重建容器与清单哈希不符')
        localized = transform(rebuilt[2048:], params)
        png_end = struct.unpack_from('<I', localized, 20)[0]
        png = localized[40:png_end]
        if localized[(png_end + 3) & ~3:] != suffix:
            fail(f'{language}: 补丁后容器元数据与原版不符')
        doc['languages'][language] = {
            'patched_sha256': entry['patched_sha256'],
            'patched_size': entry['patched_size'],
            'png_sha256': hashlib.sha256(png).hexdigest(),
            'png_size': len(png),
            'png': base64.b64encode(png).decode('ascii'),
            'suffix': base64.b64encode(suffix).decode('ascii'),
        }
        print(f'{language}: png {len(png)} 字节')

    missing = [language for language in LANGUAGES if language not in doc['languages']]
    if missing:
        fail(f'缺少语言 {missing}')
    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n',
                           encoding='utf-8')
    print(f'已写入 {args.output}（{args.output.stat().st_size} 字节）')


if __name__ == '__main__':
    main()
