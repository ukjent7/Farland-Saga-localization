"""Derive and verify the FS1 disc declarations (recipe/fs1/disc.json).

The disc side of the plan is declarative: the installer rebuilds every
declared file from the pristine disc at install time, so the repository
carries no game-derived bytes for those files. The declarations hold public
translation content in the games' own encodings (glyph-pair sequences for
event scripts, Unifont glyph cells for the font bank, named byte patches for
fixed-capacity fields) plus structural facts (LBA, sizes, container offsets).

Modes:
  extract --backup <pristine backup dir> [--patched-bin <container>]
      Rebuilds recipe/fs1/disc.json from the pristine and patched discs.
      The patched container is the installed game's FarlandSaga.bin (or an
      explicit path); needs the fls machinery and pefile, so it is a
      maintainer step. The game is only read, never launched or modified.
  verify --backup <pristine backup dir> [--language <code>]
      Golden gate with no patched disc: rebuild every declared file from the
      pristine container and the declarations, apply the pruned ZIP records,
      and require the container SHA-256 to match the payload manifest. This
      is what the Go installer must reproduce byte for byte.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fls.core.path_compat import REPO, WORK
from fls.containers.steam_container import parameters, transform
from fls.disc.cdrom import finish_sector
from fls.disc.disc_decode import tables, sector_transform
from fls.text.extract_text import event_header, event_strings

DISC_JSON = REPO / 'recipe/fs1/disc.json'
PAYLOAD_DIR = REPO / 'recipe/fs1/payload'
PERM_BASE = 283840  # the FS1 disc's permutation-image index inside the exe tables
LANGUAGES = ('zh-CN', 'ko-KR', 'en-US')
RECORD_RETAINED = ('SYSTEM/MENU.CEL', 'SYSTEM/MENU_S.SPR', 'SYSTEM/N_MENU.DAT',
                   'SYSTEM/RESET.DAT')
SPLICE_FILES = {'SYSTEM/ITEM.DAT', 'SYSTEM/MAGIC.DAT', 'SYSTEM/MENUINFO.DAT',
                'SYSTEM/MAIN.DAT', 'SYSTEM/UNIT1.DAT', '1ST.BIN'}


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
    return path in SPLICE_FILES or path.startswith('SYSTEM/SUBMAP')


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

    # Records for the retained regions (title sprites, parity-only rewrites,
    # directory sectors) still carry real container bytes; records fully inside
    # the declared sectors' container ranges are replaced by the operations.
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


def cmd_extract(backup, patched_bin, output):
    backup = Path(backup)
    patched_bin = Path(patched_bin)
    output = Path(output) if output else DISC_JSON
    from fls.containers.steam_container import convert
    from fls.disc import disc_decode
    from fls.disc.disc_extract import extract

    decoded = WORK / 'derive-disc'
    decoded.mkdir(parents=True, exist_ok=True)
    trees = {}
    for name, source in (('pristine', backup / 'FarlandSaga.bin'), ('patched', patched_bin)):
        convert(source, decoded / f'{name}.bin', backup / 'FarlandSaga.exe', 'FarlandSaga.bin')
        disc_decode.decode(decoded / f'{name}.bin', decoded / f'{name}_track1.bin',
                           backup / 'FarlandSaga.exe', 128075, None)
        report = extract(decoded / f'{name}_track1.bin', WORK / 'derive-disc' / name)
        trees[name] = {e['path']: e for e in report['files'] if e.get('sha256')}
    pristine_man, patched_man = trees['pristine'], trees['patched']

    corpus = json.loads((REPO / 'translation/corpus.json').read_text())
    rows_by_file = {}
    for row in corpus['rows']:
        rows_by_file.setdefault(row['file'], []).append(row)

    languages = {}
    for language in LANGUAGES:
        events, splices, fonts, files = {}, {}, None, {}
        for path, patched_entry in sorted(patched_man.items()):
            entry = pristine_man.get(path)
            if entry is None or (entry['sha256'] == patched_entry['sha256']
                                 and entry['lba'] == patched_entry['lba']):
                continue
            if not (is_event(path) or path == 'SYSTEM/KANJI.FON' or is_splice(path)):
                continue  # record-retained: title sprites and theme files
            before = (WORK / 'derive-disc/pristine' / path).read_bytes()
            after = (WORK / 'derive-disc/patched' / path).read_bytes()
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
                for index, (row, entry, patched_entry_line) in enumerate(zip(rows, strings, patched_strings), 1):
                    if int(row['offset'], 16) != entry['offset']:
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
            elif is_splice(path):
                patches = [{'offset': hex(lo), 'hex': after[lo:hi].hex()}
                           for lo, hi in diff_ranges(before, after)]
                splices[path] = patches
            files[path] = {'lba': entry['lba'], 'size': entry['size']}
        if not events:
            fail(f'{language}: 无事件声明')
        section = {'events': events, 'splices': splices, 'files': files}
        if fonts:
            section['font'] = fonts
        languages[language] = section

    dwords, qwords, perm = tables(backup / 'FarlandSaga.exe')
    shared_files = None
    for language, section in languages.items():
        if shared_files is None:
            shared_files = section['files']
        elif section['files'] != shared_files:
            fail(f'{language}: 文件身份与其它语言不一致')
        del section['files']
        section['ranges'] = _declared_ranges(section, shared_files, perm)
    document = {'schema': 1, 'file': 'FarlandSaga.bin', 'perm_index': 1,
                'files': shared_files, 'languages': languages}
    output.write_text(json.dumps(document, ensure_ascii=False, indent=1))
    print(f'written {output}')


def _declared_ranges(section, shared_files, perm):
    """Container-offset intervals of every sector the operations regenerate.

    The installer rewrites a declared file's whole pressed allocation (the
    payload is zero-padded to it and the parity recomputed); a relocated file
    claims only its new run. Records inside these intervals are dropped by the
    compiler because the operations reproduce those bytes."""
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
    return merge(intervals)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['extract', 'verify'])
    parser.add_argument('--backup', required=True, help='原版备份目录（含 FarlandSaga.bin/.exe）')
    parser.add_argument('--patched-bin', help='补丁后的 FarlandSaga.bin（extract；缺省取游戏目录）')
    parser.add_argument('--language', choices=LANGUAGES, help='verify 只验证一种语言')
    parser.add_argument('--output', help='extract 输出路径（缺省 recipe/fs1/disc.json）')
    arguments = parser.parse_args()
    if arguments.mode == 'verify':
        cmd_verify(arguments.backup, arguments.language)
    else:
        patched = arguments.patched_bin
        if not patched:
            fail('extract 需要 --patched-bin（游戏目录里已安装补丁的 FarlandSaga.bin）')
        cmd_extract(arguments.backup, patched, arguments.output)


if __name__ == '__main__':
    main()
