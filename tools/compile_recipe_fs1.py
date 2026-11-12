#!/usr/bin/env python3
"""Compile the FARLAND SAGA I install plan from the committed language payloads.

The release form mirrors the sequel's: a single executable embeds
``patcher/plan/saga1/plan.bin``; the installer reads the user's pristine files and
replays the plan's diff records against them. This tool packs the committed
per-language payload ZIPs in ``recipe/fs1/payload/`` into that plan:

- Every language must carry the same target files with identical original
  identities; the diff records are copied verbatim (the installer replays them
  against the original backup exactly like the ZIP engine did).
- Records are re-walked here so a truncated or out-of-order object fails at
  compile time instead of on a player's machine.
- The plan file starts with ``FS1PLAN1\\n``, a little-endian u32 header length,
  a compact JSON header, and a gzip blob. The blob holds the raw record
  streams; the header holds every file's identity and each language's version
  and per-file patched identity plus blob ranges.

The gzip wrapper depends on the host zlib, so ``plan.lock.json`` pins the
uncompressed blob's length and SHA-256 (the same policy as the sequel's
compiler). A mismatch is a hard error; rerun with ``--write-plan-lock`` after
reviewing an intentional data change. The compiler never reads the game, so
CI and a clean checkout produce the same blob byte for byte.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import struct
import sys
import zipfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PLAN_MAGIC = b'FS1PLAN1\n'
PLAN_FORMAT = 2
PLAN_GAME = 'saga1'
DECL_DIR = REPO / 'recipe' / 'fs1'
DECL_EXE = DECL_DIR / 'exe.json'
DECL_UNITY = DECL_DIR / 'unity.json'
DECL_DISC = DECL_DIR / 'disc.json'
LANGUAGES = ['zh-CN', 'ko-KR', 'en-US']
# 一代差异对象的记录格式：<偏移 u64 小端, 长度 u32 小端> + 块数据，单块上限
# 与 payload.go 的 blockSize 一致。
RECORD_STRUCT = struct.Struct('<QI')
MAX_RECORD = 4096
MAX_HEADER_LINE = 65536

DEFAULT_PAYLOAD_DIR = REPO / 'recipe' / 'fs1' / 'payload'
DEFAULT_OUTPUT = REPO / 'patcher' / 'plan' / 'saga1' / 'plan.bin'
DEFAULT_LOCK = REPO / 'recipe' / 'fs1' / 'plan.lock.json'

# 光盘镜像走流式重建，其余目标文件整体重建；与 plan_saga1.go 的 kind 一致。
DISC_IMAGE = 'FarlandSaga.bin'


def fail(message: str) -> None:
    raise SystemExit(message)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        fail(f'{path}: {error}')


def walk_records(stream: bytes, entry: dict, label: str) -> None:
    """Validate one diff-object record stream against its manifest entry.

    The stream must end exactly where its records say, stay inside the patched
    size, and advance monotonically — the same rules the installer enforces,
    applied here so bad data never reaches a release build.
    """
    patched_size = entry['patched_size']
    records = 0
    offset = 0
    cursor = 0
    while cursor < len(stream):
        if len(stream) - cursor < RECORD_STRUCT.size:
            fail(f'{label}: 记录头被截断（记录 {records}）')
        raw_offset, length = RECORD_STRUCT.unpack_from(stream, cursor)
        cursor += RECORD_STRUCT.size
        if length == 0 or length > MAX_RECORD:
            fail(f'{label}: 记录长度 {length} 越界（记录 {records}）')
        if raw_offset < offset:
            fail(f'{label}: 记录偏移 {raw_offset} 早于已回放位置 {offset}')
        if raw_offset + length > patched_size:
            fail(f'{label}: 记录越过补丁后文件大小 {patched_size}')
        if len(stream) - cursor < length:
            fail(f'{label}: 记录数据被截断（记录 {records}）')
        cursor += length
        offset = raw_offset + length
        records += 1
    if records == 0:
        fail(f'{label}: 没有任何差异记录')


def object_records(zip_file: zipfile.ZipFile, entry: dict, label: str) -> bytes:
    """Return one object's record stream, verifying its header line first."""
    try:
        compressed = zip_file.read(entry['object'])
    except (KeyError, zipfile.BadZipFile) as error:
        fail(f'{label}: 无法读取差异对象 {entry["object"]}: {error}')
    handle = gzip.GzipFile(fileobj=io.BytesIO(compressed))
    try:
        header_line = handle.readline(MAX_HEADER_LINE + 1)
        if not header_line.endswith(b'\n') or len(header_line) > MAX_HEADER_LINE:
            fail(f'{label}: 差异对象缺少头部行')
        try:
            header = json.loads(header_line)
        except json.JSONDecodeError as error:
            fail(f'{label}: 差异对象头部解析失败: {error}')
        if header.get('format') not in (1, 2):
            fail(f'{label}: 不支持的差异对象格式 {header.get("format")}')
        if header.get('sha256') != entry['patched_sha256'] or header.get('size') != entry['patched_size']:
            fail(f'{label}: 差异对象身份与 manifest 不一致')
        base = header.get('base') or {}
        if base.get('sha256') != entry['original_sha256'] or base.get('size') != entry['original_size']:
            fail(f'{label}: 差异对象的原版基准与 manifest 不一致')
        rest = handle.read()
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as error:
        fail(f'{label}: 差异对象不是有效的 gzip: {error}')
    finally:
        handle.close()
    walk_records(rest, entry, label)
    return rest


def render_glyph(font, token: str, language: str) -> bytes:
    if language == 'en-US':
        return font.ascii_pair(token)
    return font.full_cell(token)


def load_declarations(font, decl_exe: Path = DECL_EXE,
                      decl_unity: Path = DECL_UNITY) -> dict[str, dict[str, dict]]:
    """Turn the committed exe/Unity declarations into per-language plan ops.

    The compiler renders every declared glyph token through Unifont, so the
    plan carries rendered bytes while the repository stays reviewable text.
    Returns {language: {path: {'ops': [...], 'data': bytes, 'patched_sha256',
    'size'}}}.
    """
    exe_decl = read_json(decl_exe)
    unity_decl = read_json(decl_unity)
    if exe_decl.get('schema') != 1 or exe_decl.get('file') != 'FarlandSaga.exe':
        fail(f'{decl_exe}: 结构不符')
    if unity_decl.get('schema') != 1:
        fail(f'{decl_unity}: 结构不符')
    declarations: dict[str, dict[str, dict]] = {language: {} for language in LANGUAGES}

    font_cfg = exe_decl['font']
    try:
        font_start = int(font_cfg['start'], 16)
        font_limit = int(font_cfg['limit'], 16)
    except (KeyError, ValueError) as error:
        fail(f'{DECL_EXE}: font 区域不合法: {error}')
    cells = font_cfg['cells']
    if not 0 < font_start < font_limit or cells <= 0:
        fail(f'{decl_exe}: font 区域不合法')

    for language in LANGUAGES:
        section = exe_decl['languages'].get(language)
        if section is None:
            fail(f'{DECL_EXE}: 缺少语言 {language}')
        ops: list[dict] = []
        data = bytearray()
        slot_rows = sorted(section.get('glyph_slots', []), key=lambda row: row['slot'])
        previous = -1
        glyphs = bytearray()
        for row in slot_rows:
            slot = row['slot']
            if slot <= previous or slot >= cells:
                fail(f'{decl_exe}: {language} 槽位 {slot} 越界或乱序')
            previous = slot
            try:
                glyphs += render_glyph(font, row['token'], language)
            except (KeyError, AssertionError, ValueError) as error:
                fail(f'{decl_exe}: {language} 槽位 {slot} 的 token {row["token"]!r} 无法渲染: {error}')
        if slot_rows:
            ops.append({'type': 'font', 'name': 'overlay_font',
                        'start': font_start, 'limit': font_limit, 'cells': cells,
                        'slots': [row['slot'] for row in slot_rows],
                        'glyphs_offset': len(data)})
            data += glyphs
        for patch in sorted(section.get('patches', []), key=lambda item: int(item['offset'], 16)):
            payload = bytes.fromhex(patch['hex'])
            if not payload:
                fail(f'{decl_exe}: {language} 补丁 {patch["name"]} 没有数据')
            ops.append({'type': 'patch', 'name': patch['name'],
                        'offset': int(patch['offset'], 16),
                        'data_offset': len(data), 'data_length': len(payload)})
            data += payload
        declarations[language]['FarlandSaga.exe'] = {
            'ops': ops, 'data': bytes(data),
            'patched_sha256': section['patched_sha256'], 'size': section['size'],
        }

    for language in LANGUAGES:
        section = unity_decl['languages'].get(language)
        if section is None:
            fail(f'{decl_unity}: 缺少语言 {language}')
        for name, file_decl in section.items():
            if file_decl.get('schema') if isinstance(file_decl, dict) else False:
                fail(f'{decl_unity}: {name} 不应携带 schema')
            ops = []
            data = bytearray()
            for patch in sorted(file_decl['patches'], key=lambda item: int(item['offset'], 16)):
                payload = bytes.fromhex(patch['hex'])
                if not payload:
                    fail(f'{decl_unity}: {language}/{name} 补丁 {patch["name"]} 没有数据')
                ops.append({'type': 'patch', 'name': patch['name'],
                            'offset': int(patch['offset'], 16),
                            'data_offset': len(data), 'data_length': len(payload)})
                data += payload
            declarations[language][name] = {
                'ops': ops, 'data': bytes(data),
                'patched_sha256': file_decl['patched_sha256'], 'size': file_decl['size'],
            }
    return declarations


def load_disc_declarations(decl_disc: Path) -> tuple[dict, dict]:
    """读取盘片声明：共享的文件身份（LBA/尺寸）与每语言的盘片操作。

    事件操作携带新增字形对与每条已翻译行的替换序列；拼接操作携带定长
    字节补丁；字库操作携带 Unifont 字形槽位。每个语言的数据段独立成段，
    各操作的数据偏移按段内相对计算，拼进全局数据段时再整体平移。
    """
    document = read_json(decl_disc)
    if document.get('schema') != 1 or document.get('file') != DISC_IMAGE:
        fail(f'{decl_disc}: 结构不符')
    shared = document.get('files')
    if not isinstance(shared, dict) or not shared:
        fail(f'{decl_disc}: 缺少文件身份')
    languages = document.get('languages')
    if not isinstance(languages, dict):
        fail(f'{decl_disc}: 缺少语言')
    for language in LANGUAGES:
        if language not in languages:
            fail(f'{decl_disc}: 缺少语言 {language}')
    for language, section in languages.items():
        if language not in LANGUAGES:
            fail(f'{decl_disc}: 未知语言 {language}')
        if not section.get('events'):
            fail(f'{decl_disc}: {language} 没有事件操作')
        ranges = section.get('ranges')
        if not ranges:
            fail(f'{decl_disc}: {language} 缺少记录裁剪区间')

    declarations = {}
    for language in LANGUAGES:
        section = languages[language]
        ops: list[dict] = []
        data = bytearray()
        for path in sorted(section['events']):
            event = section['events'][path]
            ident = shared.get(path)
            if ident is None:
                fail(f'{decl_disc}: {path} 缺少文件身份')
            op = {'type': 'event', 'name': path, 'lba': ident['lba'], 'size': ident['size'],
                  'size_patched': event['size_patched']}
            if event.get('relocate'):
                op['relocate_lba'] = event['relocate']['lba']
            pairs = bytes.fromhex(event['pairs_added'])
            if not pairs:
                fail(f'{decl_disc}: {language}/{path} 缺少新增字形对')
            offset, length = len(data), len(pairs)
            op['pairs_offset'], op['pairs_length'] = offset, length
            data += pairs
            lines = []
            for line in sorted(event['lines'], key=lambda row: row['line']):
                payload = bytes.fromhex(line['codes'])
                if not payload:
                    fail(f'{decl_disc}: {language}/{path} 行 {line["line"]} 没有数据')
                offset, length = len(data), len(payload)
                lines.append({'line': line['line'], 'data_offset': offset, 'data_length': length})
                data += payload
            op['lines'] = lines
            ops.append(op)
        for path in sorted(section.get('splices', {})):
            patches_decl = section['splices'][path]
            ident = shared.get(path)
            if ident is None:
                fail(f'{decl_disc}: {path} 缺少文件身份')
            op = {'type': 'splice', 'name': path, 'lba': ident['lba'], 'size': ident['size']}
            patches = []
            for patch in patches_decl:
                payload = bytes.fromhex(patch['hex'])
                offset, length = len(data), len(payload)
                patches.append({'offset': int(patch['offset'], 16),
                                'data_offset': offset, 'data_length': length})
                data += payload
            op['patches'] = patches
            ops.append(op)
        for path in sorted(section.get('font', {})):
            cells = section['font'][path]['cells']
            ident = shared.get(path)
            if ident is None:
                fail(f'{decl_disc}: {path} 缺少文件身份')
            op = {'type': 'discfont', 'name': path, 'lba': ident['lba'], 'size': ident['size'],
                  'cells': ident['size'] // 32}
            slots = []
            glyphs_offset = None
            for cell in cells:
                slot, glyph = cell[0], bytes.fromhex(cell[1])
                if len(glyph) != 32:
                    fail(f'{decl_disc}: {language}/{path} 字形不是 32 字节')
                if glyphs_offset is None:
                    glyphs_offset = len(data)
                slots.append(int(slot))
                data += glyph
            op['slots'] = slots
            op['glyphs_offset'] = glyphs_offset
            ops.append(op)
        declarations[language] = {'ops': ops, 'data': bytes(data),
                                  'ranges': section['ranges']}
    return shared, declarations


def prune_disc_records(records: bytes, ranges: list[list[int]]) -> bytes:
    """丢弃完全落在声明扇区区间里的记录：那些字节由操作在安装期重建。

    跨越区间边界的记录整条保留。容器层 SSF 密文是链式密码，块内字节的
    密文依赖同一 SSF 块里相邻字节的真实历史；操作从纯原版镜像重建声明
    扇区时，共享块里未声明部分的链状态与真实补丁器不一致，产生的密文与
    真实容器逐字节不同（cdrom 层明文一致，容器层密文带恒定偏移）。跨界
    记录携带的原始容器字节（含相邻未声明扇区的密文）必须在操作完成后
    原样落盘才能收敛到真实容器。"""
    kept = bytearray()
    index = 0
    while index < len(records):
        offset = int.from_bytes(records[index:index + 8], 'little')
        length = int.from_bytes(records[index + 8:index + 12], 'little')
        end = offset + length
        inside = any(lo <= offset and end <= hi for lo, hi in ranges)
        if not inside:
            kept.extend(records[index:index + 12 + length])
        index += 12 + length
    return bytes(kept)


def load_payloads(payload_dir: Path, font, decl_exe: Path = DECL_EXE,
                  decl_unity: Path = DECL_UNITY,
                  decl_disc: Path = DECL_DISC) -> tuple[list[dict], dict, dict, bytes]:
    """Load every language ZIP and the declarations; return the plan parts.

    Files covered by declarations become install-time operations and their
    diff records are dropped; every other file keeps its record stream. The
    ZIP manifests stay the authority for original identities and patched
    sizes, and the declarations must agree with them on the patched hash.
    """
    zips = sorted(payload_dir.glob('*.zip'))
    if not zips:
        fail(f'{payload_dir}: 没有语言载荷')
    known = {path.name for path in zips}
    strays = [path.name for path in payload_dir.iterdir()
              if path.is_file() and path.name not in known]
    if strays:
        fail(f'{payload_dir}: 存在未知文件 {sorted(strays)}，语言载荷目录必须只包含 *.zip')

    declarations = load_declarations(font, decl_exe, decl_unity)
    disc_files, disc_declarations = load_disc_declarations(decl_disc)
    declared_paths = set()
    for language in LANGUAGES:
        declared_paths.update(declarations[language])
    for path in sorted(declared_paths):
        for language in LANGUAGES:
            if path not in declarations[language]:
                fail(f'{path}: 声明缺少语言 {language}')

    # 先逐个读 manifest 确定语言，再按固定的中、韩、英顺序处理，保证头部
    # 与数据段的字节顺序与压缩包文件名无关。
    manifests: dict[str, tuple[Path, dict]] = {}
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as archive:
            manifest = read_manifest(archive, zip_path)
        language = manifest['language']
        if language in manifests:
            fail(f'{zip_path.name}: 语言 {language} 有多个载荷')
        manifests[language] = (zip_path, manifest)
    missing = [language for language in LANGUAGES if language not in manifests]
    if missing:
        fail(f'缺少语言载荷：{missing}')
    extra = [language for language in manifests if language not in LANGUAGES]
    if extra:
        fail(f'未知的语言载荷：{extra}')

    languages: list[dict] = []
    files_declared: dict[str, dict] = {}
    entries: dict[str, dict] = {}
    blob_parts: list[bytes] = []
    blob_length = 0

    for language in LANGUAGES:
        zip_path, manifest = manifests[language]
        with zipfile.ZipFile(zip_path) as archive:
            declared = {item['path']: item for item in manifest['files']}
            if not entries:
                files_declared = declared
            else:
                check_same_files(files_declared, declared, language, LANGUAGES[0])
            absent = sorted(declared_paths - set(declared))
            if absent:
                fail(f'{language}: 载荷缺少声明覆盖的文件 {absent}')
            per_language: dict[str, dict] = {}
            for name in sorted(declared):
                entry = dict(declared[name])
                label = f'{language}/{name}'
                if name == DISC_IMAGE:
                    # 盘片条目：裁剪后的记录流（标题图等仍按差异携带）与盘片
                    # 操作共存；清单仍然是容器身份的权威。
                    records = object_records(archive, entry, label)
                    pruned = prune_disc_records(records, disc_declarations[language]['ranges'])
                    # 操作的数据段排在裁剪记录流之后，平移基址要加上它的长度。
                    shift = blob_length + len(pruned)
                    disc_ops = []
                    for op in disc_declarations[language]['ops']:
                        moved = dict(op)
                        if moved['type'] == 'event':
                            moved['pairs_offset'] += shift
                            moved['lines'] = [dict(line, data_offset=line['data_offset'] + shift)
                                              for line in moved['lines']]
                        elif moved['type'] == 'splice':
                            moved['patches'] = [dict(patch, data_offset=patch['data_offset'] + shift)
                                                for patch in moved['patches']]
                        else:
                            moved['glyphs_offset'] += shift
                        disc_ops.append(moved)
                    per_language[name] = {
                        'patched_sha256': entry['patched_sha256'],
                        'patched_size': entry['patched_size'],
                        'records_offset': blob_length,
                        'records_length': len(pruned),
                        'ops': disc_ops,
                    }
                    blob_parts.append(pruned)
                    blob_length += len(pruned)
                    blob_parts.append(disc_declarations[language]['data'])
                    blob_length += len(disc_declarations[language]['data'])
                    continue
                if name in declarations[language]:
                    declaration = declarations[language][name]
                    if entry['patched_sha256'] != declaration['patched_sha256']:
                        fail(f'{label}: 声明的补丁后哈希与载荷不一致')
                    if entry['patched_size'] != declaration['size']:
                        fail(f'{label}: 声明的补丁后大小与载荷不一致')
                    # 声明内的数据偏移按每文件独立数据段计算；拼进全局数据段
                    # 时必须整体平移到本文件的段基址。
                    shift = blob_length
                    for op in declaration['ops']:
                        if op['type'] == 'font':
                            op['glyphs_offset'] += shift
                        else:
                            op['data_offset'] += shift
                    per_language[name] = {
                        'patched_sha256': declaration['patched_sha256'],
                        'patched_size': entry['patched_size'],
                        'records_offset': 0,
                        'records_length': 0,
                        'ops': declaration['ops'],
                    }
                    blob_parts.append(declaration['data'])
                    blob_length += len(declaration['data'])
                    continue
                records = object_records(archive, entry, label)
                per_language[name] = {
                    'patched_sha256': entry['patched_sha256'],
                    'patched_size': entry['patched_size'],
                    'records_offset': blob_length,
                    'records_length': len(records),
                }
                blob_parts.append(records)
                blob_length += len(records)
            entries[language] = {'version': manifest['version'], 'files': per_language}
            languages.append({'code': language, 'version': manifest['version']})

    files = {
        name: {
            'kind': 'disc' if name == DISC_IMAGE else 'binary',
            'path': name,
            'original_sha256': item['original_sha256'],
            'size': item['original_size'],
        }
        for name, item in sorted(files_declared.items())
    }
    blob = b''.join(blob_parts)
    return languages, files, entries, blob


def read_manifest(archive: zipfile.ZipFile, zip_path: Path) -> dict:
    try:
        data = archive.read('manifest.json')
    except KeyError as error:
        fail(f'{zip_path.name}: 缺少 manifest.json: {error}')
    try:
        manifest = json.loads(data)
    except json.JSONDecodeError as error:
        fail(f'{zip_path.name}: manifest.json 解析失败: {error}')
    if manifest.get('kind') != 'farland-saga-patch':
        fail(f'{zip_path.name}: 不是本项目的补丁载荷')
    if manifest.get('format') != 1:
        fail(f'{zip_path.name}: 不支持的载荷格式 {manifest.get("format")}')
    if not manifest.get('version') or not manifest.get('language'):
        fail(f'{zip_path.name}: manifest 缺少版本或语言')
    if not manifest.get('files'):
        fail(f'{zip_path.name}: manifest 没有列出任何文件')
    return manifest


def check_same_files(expected: dict, actual: dict, language: str, first_language: str) -> None:
    if set(expected) != set(actual):
        fail(f'{language} 的目标文件集合与 {first_language} 不一致')
    for name, item in expected.items():
        other = actual[name]
        if item['original_sha256'] != other['original_sha256'] or item['original_size'] != other['original_size']:
            fail(f'{language}/{name} 的原版身份与 {first_language} 不一致')


def compile_plan(payload_dir: Path, output: Path, lock_path: Path,
                 write_plan_lock: bool, report_path: Path | None,
                 declarations_dir: Path | None = None) -> None:
    decl_dir = declarations_dir or DECL_DIR
    from fls.containers.unifont_bitmap import Unifont
    languages, files, entries, blob = load_payloads(
        payload_dir, Unifont(), decl_dir / 'exe.json', decl_dir / 'unity.json',
        decl_dir / 'disc.json')
    digest = hashlib.sha256(blob).hexdigest()

    header = {
        'format': PLAN_FORMAT,
        'game': PLAN_GAME,
        'languages': [item['code'] for item in languages],
        'versions': {item['code']: item['version'] for item in languages},
        'files': files,
        'languages_data': entries,
        'binary_sha256': digest,
    }
    header_bytes = json.dumps(header, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')).encode('utf-8')
    plan = PLAN_MAGIC + struct.pack('<I', len(header_bytes)) + header_bytes + gzip.compress(blob, mtime=0)

    locked = {
        'format': PLAN_FORMAT,
        'versions': dict(sorted(header['versions'].items())),
        'binary_bytes': len(blob),
        'binary_sha256': digest,
    }
    if write_plan_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(locked, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
        print(f'已写入 plan lock: {lock_path}', file=sys.stderr)
    else:
        if not lock_path.is_file():
            fail(f'missing plan lock: {lock_path}; rerun with --write-plan-lock')
        existing = read_json(lock_path)
        if existing != locked:
            differences = [
                f'{key}: compiled {locked.get(key)!r}, locked {existing.get(key)!r}'
                for key in sorted(set(locked) | set(existing))
                if locked.get(key) != existing.get(key)
            ]
            fail(f'plan does not match {lock_path}:\n  ' + '\n  '.join(differences) +
                 '\nIf these bytes are intended, rerun with --write-plan-lock.')

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(plan)
    result = {
        'output': str(output),
        'plan_bytes': len(plan),
        'blob_bytes': len(blob),
        'blob_sha256': digest,
        'languages': header['languages'],
        'versions': header['versions'],
        'files': sorted(files),
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                               encoding='utf-8')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload-dir', type=Path, default=DEFAULT_PAYLOAD_DIR,
                        help=f'语言载荷目录（默认 {DEFAULT_PAYLOAD_DIR.relative_to(REPO)}）')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                        help='plan 输出路径（默认 patcher/plan/saga1/plan.bin）')
    parser.add_argument('--lock', type=Path, default=DEFAULT_LOCK,
                        help='plan lock 路径（默认 recipe/fs1/plan.lock.json）')
    parser.add_argument('--declarations', type=Path, default=None,
                        help=f'声明目录（默认 {DECL_DIR}，含 exe.json、unity.json 与 disc.json）')
    parser.add_argument('--write-plan-lock', action='store_true',
                        help='接受本次编译的字节并改写 plan lock')
    parser.add_argument('--report', type=Path,
                        help='可选的 JSON 构建报告路径；不要放在 patcher/plan/saga1/ 内')
    arguments = parser.parse_args()
    result = compile_plan(arguments.payload_dir, arguments.output, arguments.lock,
                          arguments.write_plan_lock, arguments.report,
                          arguments.declarations)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
