#!/usr/bin/env python3
"""Derive the FS1 declarative exe/Unity recipes from a verified patch payload.

The FS1 install plan carries diff records for five target files. Three of
them — ``FarlandSaga.exe``, ``S-Tribute_Data/level0`` and
``S-Tribute_Data/resources.assets`` — change only in ways that are themselves
public localisation data:

- the Steam overlay font inside the exe is ``pack_lz(expand_font(mono))`` of a
  16x16 glyph table whose only changes are new glyph slots, every one of which
  is a GNU Unifont rendering (``fls.containers.unifont_bitmap``);
- the remaining exe changes are 24 declared runtime word writes
  (``config/patches/save-folder.json``), one jump byte, and UTF-8 gamepad
  label strings replaced with their translations;
- the Unity files change only in UTF-8 translation text at fixed offsets.

This tool replays each language's payload against the pristine originals,
extracts those changes, proves the Unifont provenance of every glyph slot
(rendering the resolved token must reproduce the extracted bytes exactly),
and writes ``recipe/fs1/exe.json`` and ``recipe/fs1/unity.json``. The compiler
(``compile_recipe_fs1.py``) then turns those declarations into plan
operations, so the plan no longer carries any exe or Unity game bytes.

Run modes:

- ``extract`` (default) needs the pristine originals (a Steam backup
  directory) plus the committed payload ZIPs; it writes the declarations.
- ``verify`` needs only the declarations and the pristine originals; it
  rebuilds every language's exe and Unity files and checks their SHA-256
  against the declared expectations. No payload ZIPs are read.

The declarations stay human-reviewable: glyph slots are stored as
``{"slot": N, "token": "字"}`` (rendered by the compiler through Unifont), and
text patches as ``{"name": ..., "offset": "0x...", "hex": ...}`` where the hex
bytes are translation text, never original game bytes.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import struct
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fls.containers.unifont_bitmap import Unifont  # noqa: E402

PAYLOAD_DIR = REPO / 'recipe' / 'fs1' / 'payload'
EXE_DECL = REPO / 'recipe' / 'fs1' / 'exe.json'
UNITY_DECL = REPO / 'recipe' / 'fs1' / 'unity.json'

EXE_NAME = 'FarlandSaga.exe'
UNITY_NAMES = ['S-Tribute_Data/level0', 'S-Tribute_Data/resources.assets']
FONT_START = 0x866AC0
FONT_LIMIT = 0x8D8150
FONT_CELLS = 7806
JUMP_OFFSET = 0xE8FD1
WORD_WRITE_REGION = (0x40000, 0x41000)
RDATA_REGION = (0x10B9000, 0x10BA000)
LANGUAGES = ['zh-CN', 'ko-KR', 'en-US']
STAGES = {'zh-CN': 'zh-CN-unifont08', 'ko-KR': 'ko-KR-unifont-full07',
          'en-US': 'en-US-unifont-full07'}


def fail(message: str) -> None:
    raise SystemExit(message)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        fail(f'{path}: {error}')


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unpack_lz(data: bytes) -> bytes:
    size, tokens, literal_offset = struct.unpack_from('>III', data)
    control = 12
    literal = literal_offset
    out = bytearray()
    mask = 0
    for _ in range(tokens):
        if not mask:
            if control >= len(data):
                fail('LZ 控制流越界')
            flags = data[control]
            control += 1
            mask = 128
        if flags & mask:
            if literal + 1 >= len(data):
                fail('LZ 引用流越界')
            distance, count = data[literal], data[literal + 1] + 3
            literal += 2
            if not distance or distance > len(out):
                fail('LZ 回溯距离非法')
            for _ in range(count):
                out.append(out[-distance])
        else:
            if literal >= len(data):
                fail('LZ 字面流越界')
            out.append(data[literal])
            literal += 1
        mask >>= 1
    if len(out) != size:
        fail(f'LZ 解压大小不符: {size} != {len(out)}')
    return bytes(out)


def rgba_to_mono(rgba: bytes) -> bytes:
    """Invert expand_font: one strict 0/255 RGBA byte group per mono byte."""
    if len(rgba) % 32:
        fail(f'RGBA 长度 {len(rgba)} 不是 32 的倍数')
    mono = bytearray(len(rgba) // 32)
    for off in range(len(mono)):
        value = 0
        for bit in range(8):
            base = off * 32 + bit * 4
            if rgba[base] or rgba[base + 1] or rgba[base + 2]:
                fail(f'RGBA 字节组 {off}/{bit} 的 RGB 通道非零')
            alpha = rgba[base + 3]
            if alpha == 255:
                value |= 128 >> bit
            elif alpha != 0:
                fail(f'RGBA 字节组 {off}/{bit} 的 alpha 非严格 0/255')
        mono[off] = value
    return bytes(mono)


def replay_payload_records(payload_path: Path, name: str, original: bytes) -> bytes:
    """Rebuild one patched file by replaying its payload diff object."""
    with zipfile.ZipFile(payload_path) as archive:
        manifest = read_manifest(archive, payload_path)
        entry = next((item for item in manifest['files'] if item['path'] == name), None)
        if entry is None:
            fail(f'{payload_path.name}: 载荷没有 {name}')
        compressed = archive.read(entry['object'])
    plain = gzip.decompress(compressed)
    header_end = plain.index(b'\n')
    body = plain[header_end + 1:]
    buffer = bytearray(original)
    pos = 0
    while pos < len(body):
        offset = int.from_bytes(body[pos:pos + 8], 'little')
        length = int.from_bytes(body[pos + 8:pos + 12], 'little')
        if offset + length > len(buffer):
            fail(f'{payload_path.name}: {name} 的记录越过文件')
        buffer[offset:offset + length] = body[pos + 12:pos + 12 + length]
        pos += 12 + length
    if sha256(buffer) != entry['patched_sha256']:
        fail(f'{payload_path.name}: {name} 回放结果与载荷声明的哈希不符')
    return bytes(buffer), entry


def read_manifest(archive: zipfile.ZipFile, path: Path) -> dict:
    try:
        manifest = json.loads(archive.read('manifest.json'))
    except KeyError as error:
        fail(f'{path.name}: 缺少 manifest.json: {error}')
    return manifest


def changed_runs(original: bytes, patched: bytes, merge: int) -> list[list[int]]:
    runs: list[list[int]] = []
    start = None
    last = -10**9
    for index in range(len(patched)):
        if original[index] != patched[index]:
            if start is None:
                start = index
                runs.append([index, index + 1])
            elif index - last > merge:
                runs.append([index, index + 1])
            else:
                runs[-1][1] = index + 1
            last = index
    return runs


def utf8_boundaries(data: bytes) -> list[bool]:
    """Mark every position where a UTF-8 decoder would start a new unit.

    Valid multi-byte sequences mark their continuation positions as
    non-boundaries; invalid bytes (including orphaned continuation bytes that
    appear in the shipped data) are units of their own, so the positions right
    before and after them stay boundaries.
    """
    marks = [True] * len(data)
    pos = 0
    while pos < len(data):
        lead = data[pos]
        if 0xC2 <= lead < 0xF5:
            if lead < 0xE0:
                length = 2
            elif lead < 0xF0:
                length = 3
            else:
                length = 4
            end = pos + length
            if end <= len(data) and all((data[k] & 0xC0) == 0x80 for k in range(pos + 1, end)):
                for k in range(pos + 1, end):
                    marks[k] = False
                pos = end
                continue
        pos += 1
    return marks


def utf8_align_runs(original: bytes, patched: bytes, runs: list[list[int]]) -> list[list[int]]:
    """Extend runs to UTF-8 codepoint boundaries on both sides, then merge.

    A byte-diff inside a multi-byte codepoint would otherwise cut the
    declaration in half; the aligned runs must each decode as valid UTF-8 on
    both sides, which the caller asserts. Boundaries come from utf8_boundaries
    so orphaned continuation bytes in the shipped data do not swallow runs.
    """
    original_marks = utf8_boundaries(original)
    patched_marks = utf8_boundaries(patched)
    aligned = True
    while aligned:
        aligned = False
        adjusted = []
        for start, end in runs:
            while start > 0 and not (original_marks[start] and patched_marks[start]):
                start -= 1
                aligned = True
            while end < len(patched) and not (original_marks[end] and patched_marks[end]):
                end += 1
                aligned = True
            adjusted.append([start, end])
        runs = []
        for start, end in adjusted:
            if runs and start - runs[-1][1] <= 16:
                runs[-1][1] = max(runs[-1][1], end)
            else:
                runs.append([start, end])
    return runs


def render_token(font: Unifont, token: str, language: str) -> bytes:
    if language == 'en-US':
        return font.ascii_pair(token)
    return font.full_cell(token)


def build_reverse_maps(font: Unifont):
    full_map: dict[bytes, list[str]] = {}
    for code in sorted(font.glyphs):
        try:
            cell = font.full_cell(chr(code))
        except AssertionError:
            continue
        full_map.setdefault(cell, []).append(chr(code))
    ascii_map: dict[bytes, list[str]] = {}
    printable = [chr(value) for value in range(0x20, 0x7F)]
    for first in printable:
        for second in printable:
            try:
                cell = font.ascii_pair(first + second)
            except (AssertionError, KeyError):
                continue
            ascii_map.setdefault(cell, []).append(first + second)
    return full_map, ascii_map


def load_vocabularies() -> dict[str, set[str]]:
    """Characters each language may legitimately render, from public tables."""
    vocab = {language: set() for language in LANGUAGES}
    corpus = read_json(REPO / 'translation' / 'corpus.json')
    rows = corpus if isinstance(corpus, list) else corpus.get('rows', [])
    for row in rows:
        translations = row.get('translations') or {}
        for language in LANGUAGES:
            text = translations.get(language)
            if text:
                vocab[language].update(text)
    for pattern in ('translation/*.json', 'config/patches/*.json'):
        for path in sorted(REPO.glob(pattern)):
            data = read_json(path)

            def walk(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key in LANGUAGES and isinstance(value, str):
                            vocab[key].update(value)
                        elif key == 'text' and isinstance(value, dict):
                            for language in LANGUAGES:
                                if isinstance(value.get(language), str):
                                    vocab[language].update(value[language])
                        else:
                            walk(value)
                elif isinstance(node, list):
                    for value in node:
                        walk(value)

            walk(data)
    return vocab


def extract_font_slots(original_mono: bytes, patched_mono: bytes, language: str,
                       font: Unifont, full_map, ascii_map, vocab: set[str],
                       zh_config_slots: dict[int, str]) -> list[dict]:
    if len(original_mono) != len(patched_mono):
        fail('字体 mono 长度不一致')
    declarations = []
    unresolved = []
    for slot in range(len(original_mono) // 32):
        start = slot * 32
        before = original_mono[start:start + 32]
        after = patched_mono[start:start + 32]
        if before == after:
            continue
        token = None
        candidates = full_map.get(after, []) + ascii_map.get(after, [])
        in_vocab = sorted(candidate for candidate in candidates if candidate in vocab)
        if len(in_vocab) >= 1:
            token = in_vocab[0]
        elif len(candidates) >= 1:
            token = sorted(candidates)[0]
        if token is not None:
            rendered = render_token(font, token, language)
            if rendered != after:
                fail(f'{language} 槽位 {slot} 的 token {token!r} 渲染与提取位图不符')
            declarations.append({'slot': slot, 'token': token})
        else:
            unresolved.append(slot)
            declarations.append({'slot': slot, 'glyph_hex': after.hex()})
    if unresolved:
        fail(f'{language}: {len(unresolved)} 个字形槽位无法解析为 Unifont token: {unresolved[:8]}')
    if language == 'zh-CN':
        # 每一个提交在 config/glyph-slots.zh-CN.json 的槽位都必须能对上提取
        # 结果或原版位图（Unifont 渲染与原版字形恰好相同的槽位不会出现在
        # 变化集里，那类槽位写入是空操作，声明里省略它们）。
        extracted = {row['slot']: row.get('token') for row in declarations}
        for row in zh_config_slots:
            slot, character = row['slot'], row['character']
            token = extracted.get(slot)
            if token is not None:
                if font.full_cell(token) != font.full_cell(character):
                    fail(f'config 槽位 {slot} 的字符 {character} 与提取 token {token} 渲染不符')
                continue
            start = slot * 32
            if original_mono[start:start + 32] == font.full_cell(character):
                continue
            fail(f'config 槽位 {slot} ({character}) 既不在变化集里也与原版位图不符')
    return declarations


def extract_exe(original: bytes, patched: bytes, language: str, font: Unifont,
                full_map, ascii_map, vocab: set[str],
                zh_config_slots: dict[int, str], save_folder: dict) -> dict:
    if len(original) != len(patched):
        fail('exe 长度不一致')
    rgba = unpack_lz(patched[FONT_START:FONT_LIMIT])
    mono_patched = rgba_to_mono(rgba)
    mono_original = rgba_to_mono(unpack_lz(original[FONT_START:FONT_LIMIT]))
    glyph_slots = extract_font_slots(mono_original, mono_patched, language, font,
                                     full_map, ascii_map, vocab, zh_config_slots)

    # Runtime word writes: every changed u32 inside .text must be declared in
    # config/patches/save-folder.json, in order, with a matching label.
    config_offsets = {}
    for label in save_folder['labels']:
        for index, write in enumerate(label['word_writes']):
            config_offsets[int(write['offset'], 16)] = (label['id'], index)
    patches = []
    for start, end in changed_runs(original[WORD_WRITE_REGION[0]:WORD_WRITE_REGION[1]],
                                   patched[WORD_WRITE_REGION[0]:WORD_WRITE_REGION[1]], merge=4):
        absolute = start + WORD_WRITE_REGION[0]
        if absolute not in config_offsets:
            fail(f'{language}: .text 游程 0x{absolute:X} 不在 save-folder 配置中')
        label_id, index = config_offsets[absolute]
        patches.append({'name': f'word-{label_id}-{index}',
                        'offset': f'0x{absolute:X}',
                        'hex': patched[absolute:absolute + (end - start)].hex()})
    # The jump byte.
    if original[JUMP_OFFSET] == patched[JUMP_OFFSET]:
        fail(f'{language}: 0x{JUMP_OFFSET:X} 处缺少跳转补丁')
    patches.append({'name': 'skip-check', 'offset': f'0x{JUMP_OFFSET:X}',
                    'hex': patched[JUMP_OFFSET:JUMP_OFFSET + 1].hex()})
    # .rdata gamepad label strings. The raw runs are relative to the region;
    # shift them to absolute file offsets before aligning to codepoint
    # boundaries on the full buffers.
    rdata_runs = [[start + RDATA_REGION[0], end + RDATA_REGION[0]]
                  for start, end in changed_runs(original[RDATA_REGION[0]:RDATA_REGION[1]],
                                                 patched[RDATA_REGION[0]:RDATA_REGION[1]], merge=16)]
    for start, end in utf8_align_runs(original, patched, rdata_runs):
        absolute = start
        segment = patched[absolute:absolute + (end - start)]
        try:
            segment.decode('utf-8')
            original[absolute:absolute + (end - start)].decode('utf-8')
        except UnicodeDecodeError:
            fail(f'{language}: rdata 游程 0x{absolute:X} 不是两侧均为 UTF-8 的文本')
        patches.append({'name': f'gamepad-0x{absolute:X}',
                        'offset': f'0x{absolute:X}', 'hex': segment.hex()})

    covered = [(WORD_WRITE_REGION[0], WORD_WRITE_REGION[1]), (RDATA_REGION[0], RDATA_REGION[1]),
               (JUMP_OFFSET, JUMP_OFFSET + 1), (FONT_START, FONT_LIMIT)]
    for start, end in changed_runs(original, patched, merge=64):
        if not any(lo <= start and end <= hi for lo, hi in covered):
            fail(f'{language}: exe 在未声明区域 0x{start:X}..0x{end:X} 有变化')
    return {
        'patched_sha256': sha256(patched),
        'size': len(patched),
        'glyph_slots': glyph_slots,
        'patches': sorted(patches, key=lambda item: int(item['offset'], 16)),
    }


def extract_unity(name: str, original: bytes, patched: bytes, language: str) -> dict:
    declarations = []
    for start, end in utf8_align_runs(original, patched, changed_runs(original, patched, merge=16)):
        segment = patched[start:end]
        try:
            segment.decode('utf-8')
            original[start:end].decode('utf-8')
        except UnicodeDecodeError:
            fail(f'{language}/{name}: 游程 0x{start:X} 不是两侧均为 UTF-8 的文本')
        declarations.append({'name': f'text-0x{start:X}',
                             'offset': f'0x{start:X}', 'hex': segment.hex()})
    return {'patched_sha256': sha256(patched), 'size': len(patched),
            'patches': declarations}


def apply_declarations(original: bytes, language_decl: dict, language: str,
                       font: Unifont) -> bytes:
    buffer = bytearray(original)
    for declaration in language_decl.get('patches', []):
        offset = int(declaration['offset'], 16)
        data = bytes.fromhex(declaration['hex'])
        if offset + len(data) > len(buffer):
            fail(f"{declaration['name']}: 补丁越过文件")
        buffer[offset:offset + len(data)] = data
    slots = language_decl.get('glyph_slots') or []
    rgba = unpack_lz(bytes(buffer[FONT_START:FONT_LIMIT]))
    mono = bytearray(rgba_to_mono(rgba))
    for declaration in slots:
        slot = declaration['slot']
        if not 0 <= slot < FONT_CELLS or slot * 32 + 32 > len(mono):
            fail(f'槽位 {slot} 越界')
        glyph = render_token(font, declaration['token'], language)
        mono[slot * 32:slot * 32 + 32] = glyph
    rgba_new = bytearray(len(rgba))
    for off, value in enumerate(mono):
        for bit in range(8):
            rgba_new[off * 32 + bit * 4 + 3] = 255 if value & (128 >> bit) else 0
    packed = pack_lz(bytes(rgba_new))
    if len(packed) > FONT_LIMIT - FONT_START:
        fail(f'{language}: 重打包字体超过保留区 ({len(packed)} > {FONT_LIMIT - FONT_START})')
    buffer[FONT_START:FONT_START + len(packed)] = packed
    buffer[FONT_START + len(packed):FONT_LIMIT] = bytes(FONT_LIMIT - FONT_START - len(packed))
    return bytes(buffer)


def pack_lz(data: bytes) -> bytes:
    flags = bytearray()
    payload = bytearray()
    pos = 0
    tokens = 0
    flag = 0
    flag_bits = 0
    size = len(data)
    while pos < size:
        maximum = min(258, size - pos)
        start = max(0, pos - 255)
        found = data.rfind(data[pos:pos + 3], start, pos + 2) if maximum >= 3 else -1
        count = 1
        if found >= 0:
            low, high = 3, maximum
            while low < high:
                mid = (low + high + 1) // 2
                if data.rfind(data[pos:pos + mid], start, pos + mid - 1) >= 0:
                    low = mid
                else:
                    high = mid - 1
            count = low
            found = data.rfind(data[pos:pos + count], start, pos + count - 1)
            if not 0 < pos - found <= 255:
                fail(f'LZ 距离越界于偏移 {pos}')
            payload.extend((pos - found, count - 3))
            flag |= 1 << (7 - flag_bits)
        else:
            payload.append(data[pos])
        pos += count
        tokens += 1
        flag_bits += 1
        if flag_bits == 8:
            flags.append(flag)
            flag = 0
            flag_bits = 0
    if flag_bits:
        flags.append(flag)
    return struct.pack('>3I', size, tokens, 12 + len(flags)) + flags + payload


def originals_from(backup: Path) -> dict[str, bytes]:
    files = {EXE_NAME: backup / EXE_NAME}
    for name in UNITY_NAMES:
        files[name] = backup / name
    data = {}
    for name, path in files.items():
        if not path.is_file():
            fail(f'缺少原版文件 {path}')
        data[name] = path.read_bytes()
    return data


def payload_map() -> dict[str, Path]:
    paths = {}
    for language in LANGUAGES:
        path = PAYLOAD_DIR / f'farland-patch-{STAGES[language]}.zip'
        if not path.is_file():
            fail(f'缺少语言载荷 {path}')
        paths[language] = path
    return paths


def cmd_extract(backup: Path) -> None:
    font = Unifont()
    full_map, ascii_map = build_reverse_maps(font)
    vocab = load_vocabularies()
    zh_slots = read_json(REPO / 'config' / 'glyph-slots.zh-CN.json')['added']
    save_folder = read_json(REPO / 'config' / 'patches' / 'save-folder.json')['runtime_word_writes']
    originals = originals_from(backup)
    payloads = payload_map()

    exe_original = originals[EXE_NAME]
    exe_decl = {
        'schema': 1,
        'file': EXE_NAME,
        'original_sha256': sha256(exe_original),
        'size': len(exe_original),
        'font': {'start': f'0x{FONT_START:X}', 'limit': f'0x{FONT_LIMIT:X}',
                 'cells': FONT_CELLS,
                 'note': 'pack_lz(expand_font(mono)); the pristine stream lives in the user\'s exe'},
        'languages': {},
    }
    unity_decl = {'schema': 1, 'languages': {}}
    for language in LANGUAGES:
        patched, entry = replay_payload_records(payloads[language], EXE_NAME, exe_original)
        if entry['original_sha256'] != sha256(exe_original):
            fail(f'{language}: 载荷的原版哈希与本地原版不符')
        exe_decl['languages'][language] = extract_exe(
            exe_original, patched, language, font, full_map, ascii_map,
            vocab[language], zh_slots, save_folder)
        unity_decl['languages'].setdefault(language, {})
        for name in UNITY_NAMES:
            original = originals[name]
            patched, entry = replay_payload_records(payloads[language], name, original)
            if entry['original_sha256'] != sha256(original):
                fail(f'{language}: {name} 的原版哈希与本地原版不符')
            unity_decl['languages'][language][name] = extract_unity(name, original, patched, language)

    EXE_DECL.write_text(json.dumps(exe_decl, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    (REPO / 'recipe' / 'fs1' / 'unity.json').write_text(
        json.dumps(unity_decl, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'wrote {EXE_DECL} ({EXE_DECL.stat().st_size:,} bytes)')
    print(f'wrote recipe/fs1/unity.json '
          f'({(REPO / "recipe" / "fs1" / "unity.json").stat().st_size:,} bytes)')


def cmd_verify(backup: Path) -> None:
    font = Unifont()
    originals = originals_from(backup)
    exe_decl = read_json(EXE_DECL)
    unity_decl = read_json(REPO / 'recipe' / 'fs1' / 'unity.json')
    for language in LANGUAGES:
        rebuilt = apply_declarations(originals[EXE_NAME], exe_decl['languages'][language],
                                     language, font)
        expected = exe_decl['languages'][language]['patched_sha256']
        if sha256(rebuilt) != expected:
            fail(f'{language}: exe 重建哈希不符: {sha256(rebuilt)} != {expected}')
        for name in UNITY_NAMES:
            declaration = unity_decl['languages'][language][name]
            buffer = bytearray(originals[name])
            for patch in declaration['patches']:
                offset = int(patch['offset'], 16)
                data = bytes.fromhex(patch['hex'])
                buffer[offset:offset + len(data)] = data
            if sha256(buffer) != declaration['patched_sha256']:
                fail(f'{language}: {name} 重建哈希不符')
        print(f'{language}: exe + Unity 重建哈希全部一致')
    print('verify OK')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', default='extract', choices=['extract', 'verify'])
    parser.add_argument('--backup', required=True,
                        help='原版备份目录（含 FarlandSaga.exe、level0、resources.assets）')
    arguments = parser.parse_args()
    backup = Path(arguments.backup)
    if arguments.mode == 'extract':
        cmd_extract(backup)
    else:
        cmd_verify(backup)


if __name__ == '__main__':
    main()
