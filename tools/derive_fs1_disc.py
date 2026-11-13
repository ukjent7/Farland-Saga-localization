"""Derive and verify the FS1 disc declarations (recipe/fs1/disc.json).

The disc side of the plan is declarative: the installer rebuilds every
declared file from the pristine disc at install time, so the repository
carries no game-derived bytes for those files. The declarations hold public
translation content in the games' own encodings (glyph-pair sequences for
event scripts, Unifont glyph cells for the font bank, named byte patches for
fixed-capacity fields and same-size graphics) plus structural facts (LBA,
sizes, container offsets, ISO directory record positions).

Modes:
  extract --backup <pristine backup dir> --patched-bin <container> --language <code>
      Rebuilds one language's declaration section from the pristine and
      patched discs. The patched container is that language's installed
      FarlandSaga.bin; needs the fls machinery and pefile, so it is a
      maintainer step. The game is only read, never launched or modified.
  merge --inputs <partial.json ...> [--output <path>]
      Merge the per-language extract documents into recipe/fs1/disc.json.
  verify --backup <pristine backup dir> [--language <code>]
      Golden gate with no patched disc: rebuild every declared file from the
      pristine container and the declarations, apply the pruned ZIP records,
      and require the container SHA-256 to match the payload manifest. This
      is what the Go installer must reproduce byte for byte.
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fls.core.path_compat import REPO, WORK
from fls.containers.steam_container import parameters, transform
from fls.disc.cdrom import finish_sector
from fls.disc.disc_decode import tables, sector_transform
from fls.disc.iso_layout import scan_directories
from fls.text.extract_text import event_header, event_strings

DISC_JSON = REPO / 'recipe/fs1/disc.json'
PAYLOAD_DIR = REPO / 'recipe/fs1/payload'
PERM_BASE = 283840  # the FS1 disc's permutation-image index inside the exe tables
LANGUAGES = ('zh-CN', 'ko-KR', 'en-US')
SPLICE_FILES = {'SYSTEM/ITEM.DAT', 'SYSTEM/MAGIC.DAT', 'SYSTEM/MENUINFO.DAT',
                'SYSTEM/MAIN.DAT', 'SYSTEM/UNIT1.DAT', '1ST.BIN',
                # 同尺寸的界面图形与标题图：补丁只改调色板化像素区。
                'SYSTEM/MENU.CEL', 'SYSTEM/MENU_S.SPR', 'SYSTEM/N_MENU.DAT',
                'SYSTEM/RESET.DAT'}


def fail(message):
    raise SystemExit(message)


def zip_disc_entry(language):
    name = (f'farland-patch-{language}-unifont08.zip' if language == 'zh-CN'
            else f'farland-patch-{language}-unifont-full07.zip')
    import gzip
    import zipfile
    with zipfile.ZipFile(PAYLOAD_DIR / name) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        entry = next(f for f in manifest['files'] if f['path'] == 'FarlandSaga.bin')
        text = gzip.decompress(archive.read(entry['object']))
    newline = text.index(b'\n')
    return manifest, entry, text[newline + 1:]


def is_event(path):
    return path.endswith('.EVT') and path.startswith(('EVE/', 'EVE2/'))


def is_splice(path):
    return path in SPLICE_FILES or path.startswith('SYSTEM/SUBMAP') or \
        (path.startswith('TITLE/TITLE') and path.endswith('.SPR'))


def merge(intervals):
    intervals = sorted(intervals)
    out = [list(intervals[0])]
    for lo, hi in intervals[1:]:
        if lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def diff_ranges(before, after, gap=8):
    spans, current = [], None
    for index in range(len(before)):
        if before[index] != after[index]:
            if current and index - current[1] <= gap:
                current[1] = index + 1
            else:
                if current:
                    spans.append(current)
                current = [index, index + 1]
    if current:
        spans.append(current)
    return spans


def le_key(pair):
    return int.from_bytes(pair, 'little')


class Disc:
    """Read/write access to the 2352-sector stream inside the SSF container."""

    def __init__(self, container, params, dwords, qwords, perm):
        self.container = bytearray(container)
        self.params, self.dwords, self.qwords, self.perm = params, dwords, qwords, perm
        self.blocks = {}

    def physical(self, lba):
        return (lba & ~63) + self.perm[PERM_BASE + lba]

    def block_plain(self, index):
        if index not in self.blocks:
            start = 2048 + 2048 * index
            raw = bytes(self.container[start:start + 2048])
            self.blocks[index] = bytearray(transform(raw, self.params, encode=False))
        return self.blocks[index]

    def set_block(self, index, plain):
        self.blocks[index] = bytearray(plain)
        cipher = transform(bytes(plain), self.params, encode=True)
        start = 2048 + 2048 * index
        self.container[start:start + 2048] = cipher

    def read_sector(self, lba):
        offset = self.physical(lba) * 2352
        first, last = offset // 2048, (offset + 2352 + 2047) // 2048
        stream = b''.join(bytes(self.block_plain(b)) for b in range(first, last))
        window = stream[offset - first * 2048:offset - first * 2048 + 2352]
        return sector_transform(window, lba, self.dwords, self.qwords)

    def read_file(self, lba, size):
        out = bytearray()
        for index in range((size + 2047) // 2048):
            out += self.read_sector(lba + index)[16:2064]
        return bytes(out[:size])

    def write_sector(self, lba, payload, fresh=False):
        payload = bytes(payload) + bytes(2048 - len(payload))
        if fresh:
            # A relocated payload leaves the pressed image: fresh sector.
            frame = lba + 150
            sector = bytearray(2352)
            sector[0:12] = b'\x00' + b'\xff' * 10 + b'\x00'
            sector[12:15] = bytes((frame // 75 // 60, frame // 75 % 60, frame % 75))
            sector[15] = 1
        else:
            # In-place rewrite keeps the pressed sector's MSF header.
            sector = bytearray(self.read_sector(lba))
        sector[16:2064] = payload
        sector = bytearray(finish_sector(sector))
        cipher = sector_transform(bytes(sector), lba, self.dwords, self.qwords, encode=True)
        assert sector_transform(cipher, lba, self.dwords, self.qwords) == bytes(sector)
        offset = self.physical(lba) * 2352
        first, last = offset // 2048, (offset + 2352 + 2047) // 2048
        rem, position = cipher, offset
        for index in range(first, last):
            plain = self.block_plain(index)
            base = index * 2048
            within = position - base
            size = min(len(rem), 2048 - within)
            plain[within:within + size] = rem[:size]
            self.set_block(index, plain)
            rem = rem[size:]
            position += size


def patch_directory_record(disc, sector, offset, size, extent):
    """Rewrite one ISO directory record's extent/size fields (both endians)."""
    payload = bytearray(disc.read_sector(sector)[16:2064])
    within = offset - 16
    if within < 0 or within + 34 > 2048:
        fail(f'目录记录偏移越界: ({sector}, {offset})')
    struct.pack_into('<I', payload, within + 2, extent)
    struct.pack_into('>I', payload, within + 6, extent)
    struct.pack_into('<I', payload, within + 10, size)
    struct.pack_into('>I', payload, within + 14, size)
    disc.write_sector(sector, bytes(payload))


def directory_updates(decl, language):
    """Which declared event files need their ISO directory record updated."""
    section = decl['languages'][language]
    files = decl['files']
    updates = []
    for path, event in sorted(section['events'].items()):
        ident = files[path]
        if event['size_patched'] == ident['size'] and not event.get('relocate'):
            continue
        extent = event['relocate']['lba'] if event.get('relocate') else ident['lba']
        updates.append((path, ident, event['size_patched'], extent))
    return updates


def rebuild(language, decl, pristine_container, params, dwords, qwords, perm, records):
    """Rebuild one language's patched container from pristine + declarations.

    Returns (container bytes, kept-records, dropped-records)."""
    section = decl['languages'][language]
    files = decl['files']
    disc = Disc(pristine_container, params, dwords, qwords, perm)
    events = section['events']

    corpus = json.loads((REPO / 'translation/corpus.json').read_text())
    rows_by_file = {}
    for row in corpus['rows']:
        rows_by_file.setdefault(row['file'], []).append(row)

    for path, event in sorted(events.items()):
        ident = files[path]
        data = disc.read_file(ident['lba'], ident['size'])
        header = event_header(data)
        strings = event_strings(data, header)
        rows = sorted(rows_by_file.get(path, []), key=lambda row: row['id'])
        if len(rows) != len(strings):
            fail(f'{path}: corpus rows {len(rows)} != script lines {len(strings)}')
        original_pairs = [data[p:p + 2] for p in
                          range(header['table_offset'] + 2, header['script_start'], 2)]
        declared = {line['line']: bytes.fromhex(line['codes']) for line in event['lines']}
        effective = []
        for index, (row, entry) in enumerate(zip(rows, strings), 1):
            if index in declared:
                codes = declared[index]
                effective.append([codes[i:i + 2] for i in range(0, len(codes), 2)])
            else:
                effective.append([original_pairs[c - 1] for c in entry['codes']])
        pairs = set(original_pairs) | {p for text in effective for p in text} | {b'\x81\x40'}
        allocation = (len(data) + 2047) // 2048 * 2048
        if header['table_offset'] + 2 + len(pairs) * 2 + len(data) - header['script_start'] > allocation:
            # Out of room: drop original pairs no line uses any more.
            pairs = {p for text in effective for p in text}
        ordered = sorted(pairs, key=le_key)
        indices = {pair: i + 1 for i, pair in enumerate(ordered)}
        script = bytearray(data[header['script_start']:])
        for row, entry, text_pairs in zip(rows, strings, effective):
            capacity = len(entry['codes'])
            codebytes = b''.join(indices[p].to_bytes(2, 'big') for p in text_pairs)
            replacement = codebytes + b'\0\0' + b'$' * (2 * (capacity - len(text_pairs)))
            offset = entry['offset'] - header['script_start'] + 1
            script[offset:offset + len(replacement)] = replacement
        patched = data[:header['table_offset']] + len(ordered).to_bytes(2, 'big') + b''.join(ordered) + script
        if len(patched) != event['size_patched']:
            fail(f'{path}: rebuilt {len(patched)} bytes, declared {event["size_patched"]}')
        if 'relocate' in event:
            start = event['relocate']['lba']
            for offset in range(0, len(patched), 2048):
                disc.write_sector(start + offset // 2048, patched[offset:offset + 2048], fresh=True)
        else:
            if len(patched) > allocation:
                fail(f'{path}: patched file outgrows its extent without a relocation')
            for offset in range(0, allocation, 2048):
                disc.write_sector(ident['lba'] + offset // 2048, patched[offset:offset + 2048])

    for path, patches in sorted(section['splices'].items()):
        ident = files[path]
        data = bytearray(disc.read_file(ident['lba'], ident['size']))
        for patch in patches:
            offset = int(patch['offset'], 16)
            payload = bytes.fromhex(patch['hex'])
            data[offset:offset + len(payload)] = payload
        patched = bytes(data)
        for offset in range(0, len(patched), 2048):
            disc.write_sector(ident['lba'] + offset // 2048, patched[offset:offset + 2048])

    for path, font in sorted(section.get('font', {}).items()):
        ident = files[path]
        data = bytearray(disc.read_file(ident['lba'], ident['size']))
        for cell, glyph in font['cells']:
            data[32 * cell:32 * cell + 32] = bytes.fromhex(glyph)
        patched = bytes(data)
        for offset in range(0, len(patched), 2048):
            disc.write_sector(ident['lba'] + offset // 2048, patched[offset:offset + 2048])

    # ISO directory records: the installer rewrites size/extent fields of the
    # events that grew or moved, exactly like the real patch does.
    dir_records = decl.get('dir_records', {})
    for path, ident, size, extent in directory_updates(decl, language):
        record = dir_records.get(path)
        if record is None:
            fail(f'{path}: 声明缺少目录记录位置')
        patch_directory_record(disc, record[0], record[1], size, extent)

    # Records for anything the operations do not regenerate still carry real
    # container bytes; with the graphics and directory declared this is empty.
    ranges = section['ranges']
    kept = dropped = 0
    for offset, length, data in records:
        if any(a <= offset and offset + length <= b for a, b in ranges):
            dropped += 1
            continue
        disc.container[offset:offset + length] = data
        kept += 1
    return bytes(disc.container), kept, dropped


def cmd_verify(backup, only_language=None):
    backup = Path(backup)
    decl = json.loads(DISC_JSON.read_text())
    if decl.get('schema') != 1 or decl.get('file') != 'FarlandSaga.bin':
        fail(f'{DISC_JSON}: 结构不符')
    languages = (only_language,) if only_language else LANGUAGES
    pristine = (backup / 'FarlandSaga.bin').read_bytes()
    params = parameters(backup / 'FarlandSaga.bin', backup / 'FarlandSaga.exe', 'FarlandSaga.bin')
    dwords, qwords, perm = tables(backup / 'FarlandSaga.exe')
    ok = True
    for language in languages:
        manifest, entry, records_raw = zip_disc_entry(language)
        records = []
        index = 0
        while index < len(records_raw):
            offset = int.from_bytes(records_raw[index:index + 8], 'little')
            length = int.from_bytes(records_raw[index + 8:index + 12], 'little')
            records.append((offset, length, records_raw[index + 12:index + 12 + length]))
            index += 12 + length
        got, kept, dropped = rebuild(language, decl, pristine, params, dwords, qwords, perm, records)
        match = hashlib.sha256(got).hexdigest() == entry['patched_sha256']
        print(f'{language}: {"MATCH" if match else "MISMATCH"} '
              f'(records kept {kept}, replaced by operations {dropped})')
        ok = ok and match
    if not ok:
        fail('容器黄金比对失败')
    print('verify OK')


def cmd_extract(backup, patched_bin, language, output):
    backup = Path(backup)
    patched_bin = Path(patched_bin)
    output = Path(output) if output else DISC_JSON
    from fls.containers.steam_container import convert
    from fls.disc import disc_decode
    from fls.disc.disc_extract import extract

    decoded = WORK / 'derive-disc'
    decoded.mkdir(parents=True, exist_ok=True)
    for name, source in (('pristine', backup / 'FarlandSaga.bin'), ('patched', patched_bin)):
        convert(source, decoded / f'{name}.bin', backup / 'FarlandSaga.exe', 'FarlandSaga.bin')
        disc_decode.decode(decoded / f'{name}.bin', decoded / f'{name}_track1.bin',
                           backup / 'FarlandSaga.exe', 128075, None)
        extract(decoded / f'{name}_track1.bin', decoded / name)
    pristine_man = {e['path']: e for e in extract(decoded / 'pristine_track1.bin', decoded / 'pristine')['files']
                    if e.get('sha256')}
    patched_man = {e['path']: e for e in extract(decoded / 'patched_track1.bin', decoded / 'patched')['files']
                   if e.get('sha256')}

    corpus = json.loads((REPO / 'translation/corpus.json').read_text())
    rows_by_file = {}
    for row in corpus['rows']:
        rows_by_file.setdefault(row['file'], []).append(row)

    events, splices, fonts, files = {}, {}, None, {}
    for path, patched_entry in sorted(patched_man.items()):
        entry = pristine_man.get(path)
        if entry is None or (entry['sha256'] == patched_entry['sha256']
                             and entry['lba'] == patched_entry['lba']):
            continue
        if not is_event(path) and path != 'SYSTEM/KANJI.FON' and not is_splice(path):
            fail(f'{path}: 未声明的盘片差异（既非事件/字库也非同尺寸拼接）')
        before = (decoded / 'pristine' / path).read_bytes()
        after = (decoded / 'patched' / path).read_bytes()
        if is_event(path):
            header = event_header(before)
            strings = event_strings(before, header)
            patched_header = event_header(after)
            patched_strings = event_strings(after, patched_header)
            rows = sorted(rows_by_file.get(path, []), key=lambda row: row['id'])
            if len(rows) != len(strings):
                fail(f'{path}: corpus rows {len(rows)} != script lines {len(strings)}')
            original_pairs = [before[p:p + 2] for p in
                              range(header['table_offset'] + 2, header['script_start'], 2)]
            new_pairs = [after[p:p + 2] for p in
                         range(patched_header['table_offset'] + 2, patched_header['script_start'], 2)]
            lines = []
            for index, (row, line_entry, patched_entry_line) in enumerate(zip(rows, strings, patched_strings), 1):
                if int(row['offset'], 16) != line_entry['offset']:
                    fail(f'{path} 行 {index}: corpus 偏移与脚本不符')
                if row['translations'].get(language):
                    codes = b''.join(new_pairs[c - 1] for c in patched_entry_line['codes'])
                    lines.append({'line': index, 'codes': codes.hex()})
            if not lines:
                fail(f'{path}: 无已翻译行')
            added = b''.join(sorted(set(new_pairs) - set(original_pairs), key=le_key))
            event = {'pairs_added': added.hex(), 'lines': lines, 'size_patched': len(after)}
            if patched_entry['lba'] != entry['lba']:
                event['relocate'] = {'lba': patched_entry['lba']}
            events[path] = event
        elif path == 'SYSTEM/KANJI.FON':
            if len(before) != len(after) or len(before) % 32:
                fail(f'{path}: 字库尺寸不符')
            cells = [[cell, after[32 * cell:32 * cell + 32].hex()]
                     for cell in range(len(after) // 32)
                     if before[32 * cell:32 * cell + 32] != after[32 * cell:32 * cell + 32]]
            if not cells:
                fail(f'{path}: 无字形差异')
            fonts = {path: {'cells': cells}}
        else:
            if len(before) != len(after) or entry['lba'] != patched_entry['lba']:
                fail(f'{path}: 拼接文件的尺寸或 LBA 发生变化')
            patches = [{'offset': hex(lo), 'hex': after[lo:hi].hex()}
                       for lo, hi in diff_ranges(before, after)]
            if not patches:
                fail(f'{path}: 无内容差异')
            splices[path] = patches
        files[path] = {'lba': entry['lba'], 'size': entry['size']}

    if not events:
        fail(f'{language}: 无事件声明')
    section = {'events': events, 'splices': splices}
    if fonts:
        section['font'] = fonts

    # ISO 目录记录：补丁会改写增长/迁移事件的 size/extent 字段。从真实
    # 容器的目录差推出每条记录的位置，并核对字段与事件声明一致。
    dwords, qwords, perm = tables(backup / 'FarlandSaga.exe')
    records, _ = scan_directories((decoded / 'pristine_track1.bin').read_bytes())
    pristine_track = (decoded / 'pristine_track1.bin').read_bytes()
    patched_track = (decoded / 'patched_track1.bin').read_bytes()
    dir_records = {}
    for path, (sector, offset) in sorted(records.items()):
        pos = sector * 2352 + offset
        old = pristine_track[pos:pos + 34]
        new = patched_track[pos:pos + 34]
        if old == new:
            continue
        if path not in events:
            fail(f'{path}: 目录记录变化但文件不是已声明事件')
        old_size = int.from_bytes(old[10:14], 'little')
        old_extent = int.from_bytes(old[2:6], 'little')
        new_size = int.from_bytes(new[10:14], 'little')
        new_extent = int.from_bytes(new[2:6], 'little')
        if old_size != files[path]['size'] or old_extent != files[path]['lba']:
            fail(f'{path}: 原版目录记录与提取的文件身份不符')
        ident = pristine_man[path]
        expect_extent = events[path].get('relocate', {'lba': ident['lba']})['lba']
        if new_size != events[path]['size_patched'] or new_extent != expect_extent:
            fail(f'{path}: 目录记录的新 size/extent 与事件声明不符')
        dir_records[path] = [sector, offset]

    section['ranges'] = _declared_ranges(section, files, perm, dir_records)
    document = {'schema': 1, 'file': 'FarlandSaga.bin', 'perm_index': 1,
                'files': files, 'dir_records': dir_records,
                'languages': {language: section}}
    output.write_text(json.dumps(document, ensure_ascii=False, indent=1))
    print(f'written {output} ({language}: {len(events)} events, {len(splices)} splices, '
          f'{len(dir_records)} directory records)')


def cmd_merge(inputs, output):
    output = Path(output) if output else DISC_JSON
    merged = None
    for item in inputs:
        document = json.loads(Path(item).read_text())
        if merged is None:
            merged = document
            continue
        if document['schema'] != merged['schema'] or document['file'] != merged['file']:
            fail(f'{item}: 结构不符')
        for path, ident in document['files'].items():
            if path in merged['files'] and merged['files'][path] != ident:
                fail(f'{item}: {path} 的文件身份与其它语言不一致')
            merged['files'][path] = ident
        for path, record in document.get('dir_records', {}).items():
            if path in merged.get('dir_records', {}) and merged['dir_records'][path] != record:
                fail(f'{item}: {path} 的目录记录位置与其它语言不一致')
            merged.setdefault('dir_records', {})[path] = record
        for language, section in document['languages'].items():
            if language in merged['languages']:
                fail(f'{item}: 语言 {language} 重复')
            merged['languages'][language] = section
    missing = [language for language in LANGUAGES if language not in merged['languages']]
    if missing:
        fail(f'缺少语言: {missing}')
    merged['languages'] = {language: merged['languages'][language] for language in LANGUAGES}
    for language, section in merged['languages'].items():
        if not section.get('ranges'):
            fail(f'{language}: partial 缺少裁剪区间')
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
    print(f'written {output} (files {len(merged["files"])}, dir_records {len(merged.get("dir_records", {}))})')


def _declared_ranges(section, shared_files, perm, dir_records=None):
    """Container-offset intervals of every sector the operations regenerate.

    The installer rewrites a declared file's whole pressed allocation (the
    payload is zero-padded to it and the parity recomputed); a relocated file
    claims only its new run, and the directory op claims the sectors hosting
    updated records. Records inside these intervals are dropped by the
    compiler because the operations reproduce those bytes."""
    if dir_records is None:
        dir_records = {}
    intervals = []
    for path in list(section['events']) + list(section['splices']) + list(section.get('font', {})):
        event = section['events'].get(path)
        if event and 'relocate' in event:
            lba, count = event['relocate']['lba'], (event['size_patched'] + 2047) // 2048
        else:
            ident = shared_files[path]
            lba, count = ident['lba'], (ident['size'] + 2047) // 2048
        for index in range(count):
            physical = ((lba + index) & ~63) + perm[PERM_BASE + lba + index]
            intervals.append((2048 + physical * 2352, 2048 + physical * 2352 + 2352))
    for path in section['events']:
        event = section['events'][path]
        ident = shared_files[path]
        if event['size_patched'] == ident['size'] and 'relocate' not in event:
            continue
        record = dir_records.get(path)
        if record is None:
            fail(f'{path}: 声明缺少目录记录位置')
        physical = (record[0] & ~63) + perm[PERM_BASE + record[0]]
        intervals.append((2048 + physical * 2352, 2048 + physical * 2352 + 2352))
    return merge(intervals)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    extract = sub.add_parser('extract', help='从原版与补丁容器推出单语言声明')
    extract.add_argument('--backup', required=True, help='原版备份目录（含 FarlandSaga.bin/.exe）')
    extract.add_argument('--patched-bin', required=True, help='该语言已安装补丁的 FarlandSaga.bin')
    extract.add_argument('--language', required=True, choices=LANGUAGES)
    extract.add_argument('--output', help='输出路径（缺省 recipe/fs1/disc.json）')
    merge = sub.add_parser('merge', help='合并单语言声明为完整 disc.json（并计算裁剪区间）')
    merge.add_argument('--inputs', nargs='+', required=True, help='各语言的 partial JSON')
    merge.add_argument('--output', help='输出路径（缺省 recipe/fs1/disc.json）')
    verify = sub.add_parser('verify', help='黄金门禁：从原版容器重建并比对清单哈希')
    verify.add_argument('--backup', required=True, help='原版备份目录（含 FarlandSaga.bin/.exe）')
    verify.add_argument('--language', choices=LANGUAGES, help='只验证一种语言')
    arguments = parser.parse_args()
    if arguments.mode == 'extract':
        cmd_extract(arguments.backup, arguments.patched_bin, arguments.language, arguments.output)
    elif arguments.mode == 'merge':
        cmd_merge(arguments.inputs, arguments.output)
    else:
        cmd_verify(arguments.backup, arguments.language)


if __name__ == '__main__':
    main()
