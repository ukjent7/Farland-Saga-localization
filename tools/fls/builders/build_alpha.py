"""Build a Simplified Chinese playable candidate, entirely under work.

Uses tables reconstructed with IDALib; preserves script instruction positions.
CD MODE1 EDC/ECC uses the standard polynomial and P/Q interleave (ECM reference:
https://github.com/alucryd/ecm-tools/blob/master/ecm.c).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
from workspace_assets import save_preview
from file_hash import sha256 as digest
from path_compat import REPO, WORK, local_path

import pefile
from PIL import Image, ImageDraw, ImageFont
import iso_layout
from disc_decode import tables, sector_transform
from extract_text import event_header, event_strings
from steam_container import parameters, transform
from translation_data import read_corpus_rows

ROOT = WORK.parent
SOURCE = WORK / 'extracted/steam'
STAGE = WORK / 'staging/alpha'
EXE = WORK / 'analysis/FarlandSaga.exe'
GLYPH_SLOTS = (
    REPO / 'config/glyph-slots.zh-CN.json'
    if (REPO / 'config/glyph-slots.zh-CN.json').is_file()
    else WORK / 'assets/sources/glyph-slots.zh-CN.json'
)
# --game 指向的已购安装根；单独检出仓库时用它解析原版容器，见 original_file。
GAME_ROOT = None


def original_file(name):
    """Locate the pristine original of a game file.

    An installation record wins when present. Otherwise prefer the analyzed
    copy under work/analysis, then the game root passed with --game, so a
    standalone checkout with FARLAND_WORK pointing elsewhere still resolves the
    executable and container it was analyzed against.
    """
    record = WORK / 'install_report.json'
    if record.exists():
        report = json.loads(record.read_text(encoding='utf-8-sig'))
        candidate = (local_path(report['backup_directory']) / name).resolve()
        assert candidate.is_relative_to(WORK.resolve())
        assert digest(candidate) == report['files'][name]['original_sha256']
        return candidate
    analyzed = WORK / 'analysis' / name
    if analyzed.is_file():
        return analyzed
    if GAME_ROOT is not None and (GAME_ROOT / name).is_file():
        return GAME_ROOT / name
    return ROOT / name


def font_index(pair):
    hi, lo = pair
    return (hi - (0x81 if hi < 0xA0 else 0xC1)) * 188 + lo - (0x40 if lo < 0x7F else 0x41)


def catalog_capacity(row):
    """Keep legacy input metadata, but never write ITEM's price at +18."""
    capacity = int(row['capacity_bytes'])
    if row['file'] == 'SYSTEM/ITEM.DAT':
        assert int(row['offset'], 16) % 36 == 0, row['id']
        capacity = min(capacity, 18)
    return capacity


def catalog_text(row, text, language):
    """Apply reviewed capacity abbreviations without changing frozen drafts."""
    if row.get('file') != 'SYSTEM/ITEM.DAT' or language != 'en':
        return text
    recipe = json.loads((REPO / 'config/patches/item-prices.json').read_text())
    for field in recipe.get('fields', []):
        if int(row['offset'], 16) == int(field['offset'], 16):
            if text == field['before']['en-US']:
                return field['text']['en-US']
    return text


class ResourceCapacityError(ValueError):
    """A changed native resource cannot fit its frozen ISO extent."""

    def __init__(self, overflows):
        self.capacity_overflows = list(overflows)
        details = []
        for item in self.capacity_overflows:
            identifiers = item.get('translation_ids') or []
            id_text = f"; IDs: {', '.join(identifiers[:4])}" if identifiers else ''
            if len(identifiers) > 4:
                id_text += f" (+{len(identifiers)-4} more)"
            details.append(
                f"{item['file']} needs {item['required_bytes']} bytes "
                f"({item['required_sectors']} sectors), but its ISO extent has "
                f"{item['available_bytes']} bytes ({item['available_sectors']} sectors); "
                f"overflow {item['overflow_bytes']} bytes{id_text}"
            )
        super().__init__('ISO resource capacity exceeded: ' + '; '.join(details))


def capacity_overflows(event_reports):
    """Summarize event files whose rebuilt payload exceeds source allocation."""
    return [{
        'file': row['file'],
        'required_bytes': row['patched_bytes'],
        'available_bytes': row['allocated_bytes'],
        'overflow_bytes': row['patched_bytes'] - row['allocated_bytes'],
        'required_sectors': (row['patched_bytes'] + 2047) // 2048,
        'available_sectors': row['allocated_bytes'] // 2048,
        'translation_ids': row.get('translation_ids', []),
    } for row in event_reports if row['patched_bytes'] > row['allocated_bytes']]


def verify_item_attributes(data):
    original = (SOURCE / 'SYSTEM/ITEM.DAT').read_bytes()
    assert len(data) == len(original) == 36 * 200
    for off in range(0, len(original), 36):
        assert data[off+18:off+36] == original[off+18:off+36], ('Item attributes changed', off // 36)


def build_events(event_rows, native, *, enforce_capacity=True):
    """Replace indexed operands while retaining every script instruction address."""
    grouped = {}
    for row in event_rows:
        grouped.setdefault(row['file'], []).append(row)
    changes, padding, reports = {}, [], []
    for name, rows in grouped.items():
        if not any(r['translation'] for r in rows):
            continue
        data = (SOURCE / name).read_bytes()
        header = event_header(data)
        strings = event_strings(data, header)
        assert len(rows) == len(strings), f'{name}: extraction row count changed'
        start = header['table_offset'] + 2
        original_pairs = [data[p:p+2] for p in range(start, header['script_start'], 2)]
        effective = []
        for line, (row, entry) in enumerate(zip(rows, strings), 1):
            assert row['id'] == f'{name}:{line:04d}'
            assert int(row['offset'], 16) == entry['offset'], row['id']
            assert row['source'] == entry['text'], row['id']
            assert int(row['original_units']) == len(entry['codes']), row['id']
            assert row['raw_codes'] == ' '.join(f'{c:04X}' for c in entry['codes']), row['id']
            if row['translation']:
                assert len(row['translation']) <= len(entry['codes']), row['id']
                effective.append([native[ch] for ch in row['translation']])
            else:
                effective.append([original_pairs[c-1] for c in entry['codes']])
        required_pairs = {pair for text in effective for pair in text}
        pairs = set(original_pairs) | required_pairs | {native['\u3000']}
        allocation = (len(data)+2047)//2048*2048
        compacted = False
        if header['table_offset']+2+len(pairs)*2+len(data)-header['script_start'] > allocation:
            # All uses of indexed glyphs are text operands. Unused original
            # glyphs can be dropped after every operand is remapped below.
            pairs = required_pairs
            compacted = True
        pairs = sorted(pairs, key=lambda p: int.from_bytes(p, 'little'))
        indices = {pair: i+1 for i, pair in enumerate(pairs)}
        script = bytearray(data[header['script_start']:])
        allowed = set()
        for line, (row, entry, text_pairs) in enumerate(zip(rows, strings, effective), 1):
            capacity = len(entry['codes'])
            codebytes = b''.join(indices[pair].to_bytes(2, 'big') for pair in text_pairs)
            replacement = codebytes + b'\0\0' + b'$'*(2*(capacity-len(text_pairs)))
            off = entry['offset']-header['script_start']+1
            assert len(replacement) == capacity*2+2
            script[off:off+len(replacement)] = replacement
            allowed.update(range(off, entry['end']-header['script_start']))
            assert [pairs[int.from_bytes(codebytes[i:i+2], 'big')-1]
                    for i in range(0, len(codebytes), 2)] == text_pairs
            if row['translation']:
                padding.append({'id': row['id'], 'file': name, 'line': line,
                                'removed_blank_glyphs': capacity-len(text_pairs)})
        assert all(i in allowed for i, (a, b) in enumerate(zip(data[header['script_start']:], script)) if a != b)
        patched = data[:header['table_offset']] + len(pairs).to_bytes(2, 'big') + b''.join(pairs) + script
        assert patched[:header['table_offset']] == data[:header['table_offset']]
        # Reparse the final script to catch shifted/extra literal boundaries.
        parsed_header = event_header(patched)
        parsed = event_strings(patched, parsed_header)
        assert len(parsed) == len(strings), name
        assert parsed_header['events'] == header['events'], name
        for before, after, expected in zip(strings, parsed, effective):
            assert after['offset']-parsed_header['script_start'] == before['offset']-header['script_start'], name
            assert [pairs[c-1] for c in after['codes']] == expected, name
        changes[name] = patched
        reports.append({'file': name, 'translated_lines': sum(bool(r['translation']) for r in rows),
                        'glyphs': len(pairs), 'original_glyphs': len(original_pairs),
                        'compacted_table': compacted, 'allocated_bytes': allocation,
                        'patched_bytes': len(patched), 'remaining_bytes': max(0, allocation-len(patched)),
                        'overflow_bytes': max(0, len(patched)-allocation),
                        'required_sectors': (len(patched)+2047)//2048,
                        'available_sectors': allocation//2048,
                        'translation_ids': [row['id'] for row in rows if row['translation']]})
    # A payload that outgrows its frozen extent is moved into an unclaimed
    # sector run; the game opens event files by ISO path, so only the
    # directory record has to follow it.
    needs = {row['file']: row['patched_bytes'] for row in reports
             if row['patched_bytes'] > row['allocated_bytes']}
    relocations, unplaced = iso_layout.relocation_plan(needs)
    for row in reports:
        moved = relocations.get(row['file'])
        if moved is None:
            continue
        lba, sectors = moved
        row['allocated_bytes'] = sectors*2048
        row['available_sectors'] = sectors
        row['remaining_bytes'] = max(0, sectors*2048-row['patched_bytes'])
        row['overflow_bytes'] = 0
        row['relocated_lba'] = lba
        row['relocated_sectors'] = sectors
    overflows = capacity_overflows(reports)
    if enforce_capacity and overflows:
        raise ResourceCapacityError(overflows)
    return changes, padding, reports


def sync_resource_mirrors(changes, rows):
    # MAIN.DAT is the runtime resource bundle, and contains complete copies
    # of ITEM.DAT and MENUINFO.DAT plus a unit-name prefix. Patch both forms.
    original_main = (SOURCE / 'SYSTEM/MAIN.DAT').read_bytes()
    verify_item_attributes(changes.get('SYSTEM/ITEM.DAT', (SOURCE / 'SYSTEM/ITEM.DAT').read_bytes()))
    main = bytearray(original_main)
    mirrors = []
    for name in ['SYSTEM/ITEM.DAT', 'SYSTEM/MENUINFO.DAT']:
        original = (SOURCE / name).read_bytes()
        off = original_main.find(original)
        assert off >= 0 and original_main.find(original, off+1) == -1
        replacement = changes.get(name, original)
        assert len(replacement) == len(original)
        main[off:off+len(original)] = replacement
        mirrors.append({'resource': name, 'offset': hex(off), 'size': len(original)})
    unit_rows = [r for r in rows if r['group'] == 'unit']
    original_units = (SOURCE / 'SYSTEM/UNIT1.DAT').read_bytes()
    first_unit = unit_rows[0]
    first_off = int(first_unit['offset'], 16)
    first_capacity = int(first_unit['capacity_bytes'])
    unit_base = original_main.find(original_units[first_off:first_off+first_capacity]) - first_off
    assert unit_base >= 0
    for r in rows:
        if r['group'] != 'unit':
            continue
        off, capacity = int(r['offset'], 16), int(r['capacity_bytes'])
        field = (SOURCE / r['file']).read_bytes()[off:off+capacity]
        target = unit_base + off
        assert original_main[target:target+capacity] == field, r['id']
        main[target:target+capacity] = changes[r['file']][off:off+capacity]
        mirrors.append({'resource': r['id'], 'offset': hex(target), 'size': capacity})
    # MAGIC.DAT has identical name fields here, but some other record bytes
    # differ. Copy only the translated names to retain runtime attributes.
    magic_rows = [r for r in rows if r['group'] == 'magic']
    if magic_rows:
        original_magic = (SOURCE / 'SYSTEM/MAGIC.DAT').read_bytes()
        prefix = original_magic[:32]
        candidates = []
        candidate = original_main.find(prefix)
        while candidate >= 0:
            if all(original_main[candidate+off:candidate+off+22] == original_magic[off:off+22]
                   for off in range(0, len(original_magic), 32)):
                candidates.append(candidate)
            candidate = original_main.find(prefix, candidate+1)
        assert len(candidates) == 1, 'Runtime magic-name table must match all 90 fields uniquely'
        magic_base = candidates[0]
        for r in magic_rows:
            off, capacity = int(r['offset'], 16), int(r['capacity_bytes'])
            target = magic_base+off
            assert original_main[target:target+capacity] == original_magic[off:off+capacity], r['id']
            main[target:target+capacity] = changes[r['file']][off:off+capacity]
            mirrors.append({'resource': r['id'], 'offset': hex(target), 'size': capacity})
    assert len(main) == len(original_main)
    changes['SYSTEM/MAIN.DAT'] = bytes(main)
    return changes, mirrors


def build_resources(font_source='simsun', *, enforce_capacity=True):
    rows = [r for r in read_corpus_rows('zh-CN', {'catalog'}) if r['translation']]
    event_rows = read_corpus_rows('zh-CN', {'event'})
    descriptions = read_corpus_rows('zh-CN', {'description'})
    conditions = read_corpus_rows('zh-CN', {'condition'})
    texts = ([r['translation'] for r in event_rows if r['translation']]
             + [r['translation'] for r in rows]
             + [r['translation'] for r in descriptions]
             + [r['translation'] for r in conditions])
    native = {}
    valid = []
    for hi in list(range(0x81, 0xA0)) + list(range(0xE0, 0xEB)):
        for lo in range(0x40, 0xFD):
            if lo == 0x7F:
                continue
            pair = bytes((hi, lo))
            if font_index(pair) >= 7806:
                continue
            try:
                pair.decode('cp932')
            except UnicodeDecodeError:
                continue
            valid.append(pair)
    used = set()
    for path in SOURCE.rglob('*.EVT'):
        data = path.read_bytes()
        h = event_header(data)
        start = h['table_offset'] + 2
        used.update(data[p:p+2] for p in range(start, h['script_start'], 2))
    # Protect original literal characters, including the second unit data file.
    for name in ['1ST.BIN', 'SYSTEM/ITEM.DAT', 'SYSTEM/MAGIC.DAT', 'SYSTEM/UNIT1.DAT', 'SYSTEM/UNIT2.DAT', 'SYSTEM/MENUINFO.DAT']:
        data = (SOURCE / name).read_bytes()
        used.update(data[p:p+2] for p in range(len(data)-1))
    characters = set(''.join(texts)) | {'\u3000'}
    slots_path = GLYPH_SLOTS
    baseline = json.loads(slots_path.read_text(encoding='utf-8')) if slots_path.exists() else {'added': []}
    reserved_pairs = {bytes.fromhex(r['cp932_hex']) for r in baseline['added']}
    missing = []
    for ch in sorted(characters):
        try:
            pair = ch.encode('cp932')
        except UnicodeEncodeError:
            missing.append(ch)
            continue
        if len(pair) != 2 or pair not in valid or pair in reserved_pairs:
            missing.append(ch)
        else:
            native[ch] = pair
            used.add(pair)
    # Keep released glyph assignments stable when text is edited or removed.
    # Otherwise changing a description shifts every later allocated character.
    assignments = {}
    if slots_path.exists():
        for entry in baseline['added']:
            ch, pair = entry['character'], bytes.fromhex(entry['cp932_hex'])
            assert pair in valid and font_index(pair) == entry['slot']
            assert pair not in used and pair not in assignments.values(), ch
            assert ch not in native, ch
            assignments[ch] = pair
    free = [p for p in reversed(valid) if p not in used and p not in assignments.values() and font_index(p) >= 4000]
    pending = [ch for ch in missing if ch not in assignments]
    assert len(free) >= len(pending), 'Not enough unused font slots'
    assignments.update(zip(pending, free))
    font = bytearray((SOURCE / 'SYSTEM/KANJI.FON').read_bytes())
    from unifont_bitmap import Unifont
    unifont = Unifont() if font_source == 'unifont' else None
    ttf = None if unifont else ImageFont.truetype(str(local_path('C:/Windows/Fonts/simsun.ttc')), 16)
    added = []
    for ch, pair in assignments.items():
        native[ch] = pair
        if unifont:
            packed = unifont.full_cell(ch)
        else:
            tile = Image.new('1', (16, 16))
            draw = ImageDraw.Draw(tile)
            box = ttf.getbbox(ch)
            # Native horizontal marks sit on row 8, rather than the top.
            ink_top = 8 if ch == '\u2014' else 0
            draw.text((0, ink_top-box[1]), ch, font=ttf, fill=1)
            assert tile.getbbox(), f'Empty glyph: {ch}'
            packed = b''.join(sum(int(tile.getpixel((x, y))) << (15-x) for x in range(16)).to_bytes(2, 'big') for y in range(16))
        slot = font_index(pair)
        font[slot*32:(slot+1)*32] = packed
        added.append({'character': ch, 'cp932_hex': pair.hex(), 'slot': slot})
    if unifont:
        # Include already encodable Chinese characters for consistent styling;
        # preserve every released CP932 assignment and unreferenced glyph.
        for ch, pair in native.items():
            slot = font_index(pair)
            font[slot*32:(slot+1)*32] = unifont.full_cell(ch)
        unifont.export(STAGE)
    (STAGE / 'glyph_map.json').write_text(json.dumps({'added': added, 'all': {ch: p.hex() for ch, p in native.items()}}, ensure_ascii=False, indent=2), encoding='utf-8')
    def encode(text):
        return b''.join(native[ch] for ch in text)
    changes = {'SYSTEM/KANJI.FON': bytes(font)}
    for r in rows:
        name = r['file']
        data = bytearray(changes.get(name, (SOURCE / name).read_bytes()))
        off, capacity = int(r['offset'], 16), catalog_capacity(r)
        payload = encode(r['translation'])
        assert len(payload) < capacity, r['id']
        data[off:off+capacity] = payload + bytes(capacity-len(payload))
        assert len(data) == (SOURCE / name).stat().st_size
        changes[name] = bytes(data)
    for r in descriptions:
        name = r['file']
        original = (SOURCE / name).read_bytes()
        off = int(r['offset'], 16)
        end = original.index(0, off)
        assert original[off:end].decode('cp932') == r['source']
        payload = encode(r['translation'])
        capacity = end+1-off
        assert len(payload) < capacity
        data = bytearray(changes.get(name, original))
        data[off:end+1] = payload + bytes(capacity-len(payload))
        changes[name] = bytes(data)
    changes, mirrors = sync_resource_mirrors(changes, rows)
    event_changes, padding_removed, event_reports = build_events(
        event_rows, native, enforce_capacity=enforce_capacity)
    overflows = capacity_overflows(event_reports)
    changes.update(event_changes)
    (STAGE / 'event_capacity.json').write_text(json.dumps(event_reports, ensure_ascii=False, indent=2), encoding='utf-8')
    (STAGE / 'resource_mirrors.json').write_text(json.dumps(mirrors, indent=2), encoding='utf-8')
    (STAGE / 'dialogue_padding.json').write_text(json.dumps(padding_removed, indent=2), encoding='utf-8')
    for name, payload in changes.items():
        target = STAGE / 'resources' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    # Render using the actual patched 16x16 bitmap, not the desktop font.
    opening_samples = [r['translation'] for r in event_rows if r.get('file') == 'EVE/EVE01_1.EVT' and r.get('translation')][:9]
    samples = ['系统  胜利条件  全局地图', '里昂  拉尔夫  巴斯', *[r['translation'] for r in descriptions], *opening_samples]
    preview = Image.new('RGB', (max(720, max(map(len, samples))*32+20), len(samples)*44+20), '#171d25')
    for y, text in enumerate(samples):
        for x, ch in enumerate(text):
            if ch == ' ':
                continue
            pair = native[ch]
            bits = font[font_index(pair)*32:(font_index(pair)+1)*32]
            tile = Image.new('RGB', (16, 16), '#171d25')
            for row in range(16):
                v = int.from_bytes(bits[row*2:row*2+2], 'big')
                for col in range(16):
                    if v & (1 << (15-col)):
                        tile.putpixel((col, row), (238, 244, 250))
            preview.paste(tile.resize((32, 32), Image.Resampling.NEAREST), (10+x*32, 10+y*44))
    save_preview(STAGE / 'font_preview.png',preview)
    translated_events = sum(bool(r['translation']) for r in event_rows)
    opening_lines = sum(bool(r['translation']) and r['file'] == 'EVE/EVE01_1.EVT' for r in event_rows)
    event_glyphs = {r['file']: r['glyphs'] for r in event_reports}
    print(f'Resources: {len(changes)}, texts: {len(rows)+translated_events+len(descriptions)}, new glyphs: {len(added)}, event files: {len(event_reports)}', flush=True)
    return changes, {'translated_texts': len(rows)+translated_events+len(descriptions),
                     'opening_lines': opening_lines, 'event_lines': translated_events,
                     'event_files': len(event_reports), 'catalog_lines': len(rows),
                     'item_names': sum(r['group']=='item' for r in rows),
                     'magic_names': sum(r['group']=='magic' for r in rows),
                     'unit_names': sum(r['group']=='unit' for r in rows),
                     'ui_literals': sum(r['group'] in ('ui', 'menu') for r in rows),
                     'item_descriptions': sum(r.get('group', 'items')=='items' for r in descriptions),
                     'unit_descriptions': sum(r.get('group')=='units' for r in descriptions),
                     'skill_descriptions': sum(r.get('group')=='skills' for r in descriptions),
                     'removed_blank_glyphs': sum(r['removed_blank_glyphs'] for r in padding_removed),
                     'new_glyphs': len(added), 'event_glyphs': event_glyphs,
                     'compacted_event_tables': sum(r['compacted_table'] for r in event_reports),
                     'packable': not overflows, 'capacity_overflows': overflows,
                     'packing_block_reason': ('ISO extent capacity exceeded; ' + '; '.join(
                         f"{r['file']} needs {r['required_bytes']} bytes, "
                         f"{r['available_bytes']} allocated ({r['overflow_bytes']} byte overflow)"
                         for r in overflows)) if overflows else None}



def make_cd_tables():
    edc, forward, backward = [], [], [0]*256
    for i in range(256):
        v = i
        for _ in range(8):
            v = (v >> 1) ^ (0xD8018001 if v & 1 else 0)
        edc.append(v)
        v = (i << 1) ^ (0x11D if i & 128 else 0)
        forward.append(v)
        backward[i ^ v] = i
    return edc, forward, backward


EDC, FORWARD, BACKWARD = make_cd_tables()


def finish_sector(sector):
    assert len(sector) == 2352 and sector[15] == 1
    v = 0
    for b in sector[:2064]:
        v = (v >> 8) ^ EDC[(v ^ b) & 255]
    sector[2064:2068] = v.to_bytes(4, 'little')
    sector[2068:2076] = bytes(8)
    def parity(majors, minors, multiplier, increment, dest):
        size = majors * minors
        for major in range(majors):
            index = (major >> 1)*multiplier + (major & 1)
            a = b = 0
            for _ in range(minors):
                value = sector[12+index]
                index = (index+increment) % size
                a = FORWARD[a ^ value]
                b ^= value
            a = BACKWARD[FORWARD[a] ^ b]
            sector[dest+major] = a
            sector[dest+major+majors] = a ^ b
    parity(86, 24, 2, 86, 2076)
    parity(52, 43, 86, 88, 2248)
    return bytes(sector)


def mode1_sector(lba, payload):
    """Start a fresh MODE1 sector for an extent outside the pressed image."""
    assert len(payload) <= 2048
    sector = bytearray(2352)
    sector[0:12] = b'\x00' + b'\xff'*10 + b'\x00'
    frame = lba + 150
    sector[12:15] = bytes((frame//75//60, frame//75 % 60, frame % 75))
    sector[15] = 1
    sector[16:16+len(payload)] = payload
    return sector


def build_disc(changes, keep_intermediate=False):
    manifest = json.loads((SOURCE / '_manifest.json').read_text(encoding='utf-8'))['files']
    entries = {r['path']: r for r in manifest}
    raw = (WORK / 'decoded/FarlandSaga_track1.bin').read_bytes()
    changed = {}
    def sector(lba):
        if lba not in changed:
            changed[lba] = bytearray(raw[lba*2352:(lba+1)*2352])
        return changed[lba]
    directory_locations, _ = iso_layout.scan_directories()
    needs = {name: len(payload) for name, payload in changes.items()
             if len(payload) > (entries[name]['size']+2047)//2048*2048}
    relocations, unplaced = iso_layout.relocation_plan(needs)
    if unplaced:
        raise ResourceCapacityError([{
            'file': name, 'required_bytes': needs[name],
            'available_bytes': (entries[name]['size']+2047)//2048*2048,
            'overflow_bytes': needs[name]-(entries[name]['size']+2047)//2048*2048,
            'required_sectors': (needs[name]+2047)//2048,
            'available_sectors': (entries[name]['size']+2047)//2048*2048//2048,
            'translation_ids': [],
        } for name in unplaced])
    for name, payload in changes.items():
        entry = entries[name]
        moved = relocations.get(name)
        if moved is None:
            allocation = (entry['size']+2047)//2048*2048
            assert len(payload) <= allocation, name
            for off in range(0, allocation, 2048):
                lba = entry['lba'] + off//2048
                block = payload[off:off+2048]
                sector(lba)[16:2064] = block + bytes(2048-len(block))
        else:
            # The game opens event files by ISO path, so a relocated payload
            # only needs its directory record repointed at the free extent.
            start, sectors = moved
            assert len(payload) <= sectors*2048, name
            for off in range(0, len(payload), 2048):
                block = payload[off:off+2048]
                changed[start+off//2048] = mode1_sector(start+off//2048,
                                                        block + bytes(2048-len(block)))
        if moved is not None or len(payload) != entry['size']:
            lba, offset = directory_locations[name]
            assert offset+18 <= 2064
            extent_lba = moved[0] if moved is not None else entry['lba']
            sector(lba)[offset+2:offset+6] = extent_lba.to_bytes(4, 'little')
            sector(lba)[offset+6:offset+10] = extent_lba.to_bytes(4, 'big')
            sector(lba)[offset+10:offset+14] = len(payload).to_bytes(4, 'little')
            sector(lba)[offset+14:offset+18] = len(payload).to_bytes(4, 'big')
    for lba in changed:
        changed[lba] = finish_sector(changed[lba])
    # Match the original pressed-disc MODE1 parity on an unchanged sector. The
    # Steam build carries no pressed-disc EDC/ECC, so a standalone checkout falls
    # back to the repository's known-answer vector; see cd_parity.
    from cd_parity import pressed_reference, verify
    original, parity_source = pressed_reference()
    verify(original, parity_source)
    # The packing intermediate is rebuildable, so an explicit RAM-disk override
    # keeps it off the workspace drive without touching the candidate itself.
    pack_tmp = os.environ.get('FARLAND_PACK_TMPDIR')
    inner = (Path(pack_tmp) / 'inner.bin') if pack_tmp else STAGE / 'inner.bin'
    inner.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(WORK / 'decoded/FarlandSaga.bin', inner)
    dwords, qwords, perm = tables(EXE)
    physical = {}
    with inner.open('r+b') as f:
        for lba, block in sorted(changed.items()):
            target = (lba & ~63) + perm[283840+lba]
            assert target not in physical
            physical[target] = lba
            cipher = sector_transform(block, lba, dwords, qwords, encode=True)
            assert sector_transform(cipher, lba, dwords, qwords) == block
            f.seek(target*2352)
            f.write(cipher)
    # Outer encryption preserves the authenticated header and original length.
    out = STAGE / 'FarlandSaga.bin'
    original_container = original_file('FarlandSaga.bin')
    params = parameters(original_container, EXE, 'FarlandSaga.bin')
    with inner.open('rb') as src, out.open('wb') as dst:
        dst.write(params['header'])
        total = 0
        while block := src.read(32768):
            dst.write(transform(block, params, encode=True))
            total += len(block)
            if total % (64*1024*1024) == 0:
                print(f'Packed {total//1048576} MiB', flush=True)
    assert out.stat().st_size == original_container.stat().st_size
    # Read the final encrypted candidate, independently undo both layers.
    with out.open('rb') as f:
        cache = {}
        def inner_bytes(offset, length):
            result = bytearray()
            while length:
                base = offset//2048*2048
                if base not in cache:
                    f.seek(2048+base)
                    cache[base] = transform(f.read(2048), params)
                start = offset-base
                n = min(length, 2048-start)
                result.extend(cache[base][start:start+n])
                length -= n
                offset += n
            return bytes(result)
        for target, lba in physical.items():
            actual = sector_transform(inner_bytes(target*2352, 2352), lba, dwords, qwords)
            assert actual == changed[lba], f'Final disc verification failed: {lba}'
    # All untouched encrypted outer blocks must remain byte-identical.
    allowed_blocks = set()
    for target in physical:
        allowed_blocks.update(range(target*2352//2048, (target*2352+2351)//2048+1))
    with out.open('rb') as a, original_container.open('rb') as b:
        assert a.read(2048) == b.read(2048)
        i = 0
        while block := a.read(2048):
            other = b.read(2048)
            assert i in allowed_blocks or block == other, f'Unexpected outer block modified: {i}'
            i += 1
    print(f'Final disc verified: {len(changed)} changed sectors', flush=True)
    if not keep_intermediate:
        inner.unlink()
    return {'changed_sectors': len(changed),
            'cd_parity_reference': parity_source,
            'relocations': [{'path': name, 'lba': lba, 'sectors': sectors,
                             'bytes': needs[name]} for name, (lba, sectors) in sorted(relocations.items())],
            'changed_resources': [{'path': n, 'original_size': entries[n]['size'], 'patched_size': len(p), 'sha256': hashlib.sha256(p).hexdigest()} for n, p in changes.items()]}


def patch_exe(include_host_strings=True):
    data = bytearray(EXE.read_bytes())
    pe = pefile.PE(str(EXE), fast_load=True)
    offset = pe.get_offset_from_rva(0xE9BD1)
    assert data[offset:offset+2] == b'\x74\x32', 'Unexpected checksum branch'
    data[offset] = 0xEB
    # Steam adds an item-description overlay with its own embedded RGBA font.
    # Its CP932 slots must match the Saturn font used for names/equipment.
    from overlay_font import font_patch
    patched, overlay = font_patch(bytes(data), (STAGE / 'resources/SYSTEM/KANJI.FON').read_bytes())
    data = bytearray(patched)
    host_rows = []
    if include_host_strings:
        host_rows = read_corpus_rows('zh-CN', {'host'})
        for row in host_rows:
            off = int(row['offset'], 16)
            source = row['source'].encode('utf-8')
            translated = row['translation'].encode('utf-8')
            capacity = row['capacity_bytes']
            assert capacity == len(source)+1
            assert data[off:off+capacity] == source+b'\0', row['id']
            assert len(translated) < capacity, row['id']
            data[off:off+capacity] = translated+bytes(capacity-len(translated))
    (STAGE / 'FarlandSaga.exe').write_bytes(data)
    original = EXE.read_bytes()
    start = overlay['file_offset']
    end = start + overlay['reserved_bytes']
    assert len(original) == len(data)
    host_allowed = {i for row in host_rows for i in range(int(row['offset'],16), int(row['offset'],16)+row['capacity_bytes'])}
    assert all(i == offset or start <= i < end or i in host_allowed for i, (a, b) in enumerate(zip(original, data)) if a != b)
    return {'va': '0x1400E9BD1', 'file_offset': offset, 'before': '7432', 'after': 'eb32', 'purpose': 'Skip destructive second decryption on modified sector checksum mismatch', 'item_description_font': overlay, 'host_strings':len(host_rows)}


def main():
    global STAGE, GAME_ROOT
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', default='alpha')
    parser.add_argument('--version', default='alpha01')
    parser.add_argument('--resources-only', action='store_true', help='Prepare resources and capacity report without packing the disc')
    parser.add_argument('--font-source', choices=['simsun', 'unifont'], default='unifont',
                        help='Glyph source; unifont ships in the repository and needs no Windows font')
    parser.add_argument('--game', help='Game installation root; copies its FarlandSaga.exe into work/analysis when missing')
    parser.add_argument('--force', action='store_true', help='Overwrite a differing work/analysis/FarlandSaga.exe')
    args = parser.parse_args()
    if args.game:
        GAME_ROOT = Path(args.game).resolve()
        if not GAME_ROOT.is_dir():
            raise SystemExit(f'游戏根目录不存在或不是目录：{GAME_ROOT}')
        source = GAME_ROOT / 'FarlandSaga.exe'
        if not source.is_file():
            raise SystemExit(f'游戏可执行文件不存在：{source}')
        EXE.parent.mkdir(parents=True, exist_ok=True)
        if EXE.is_file() and digest(EXE) != digest(source):
            if not args.force:
                raise SystemExit(f'{EXE} 与 {source} 不一致；确认游戏安装未被修改后加 --force 覆盖')
            shutil.copyfile(source, EXE)
        elif not EXE.is_file():
            shutil.copyfile(source, EXE)
    STAGE = (WORK / 'staging' / args.stage).resolve()
    assert STAGE.is_relative_to((WORK / 'staging').resolve())
    STAGE.mkdir(parents=True, exist_ok=True)
    assert digest(original_file('FarlandSaga.exe')) == digest(EXE), 'Original executable differs from analyzed original'
    changes, report = build_resources(args.font_source, enforce_capacity=not args.resources_only)
    report['version'] = 'zh-CN ' + args.version
    if args.resources_only:
        report['build_status'] = 'resources_only'
        report['installed'] = False
        report['packable'] = not report.get('capacity_overflows')
        (STAGE / 'resource_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return
    report.update(build_disc(changes))
    report['exe_patch'] = patch_exe()
    report['files'] = {name: {'original_sha256': digest(original_file(name)), 'patched_sha256': digest(STAGE / name), 'size': (STAGE / name).stat().st_size} for name in ['FarlandSaga.bin', 'FarlandSaga.exe']}
    report['verification'] = 'All changed final encrypted sectors round-trip exactly; unchanged encrypted blocks unchanged; script non-text bytes preserved; text capacities checked; CD parity reference matched; all translated event files reparsed and runtime name mirrors synchronized; embedded Steam description font decoded to the patched KANJI.FON expansion.'
    report['build_status'] = 'candidate_ready'
    report['input_snapshots'] = {}
    sources = [REPO / 'translation/corpus.json', GLYPH_SLOTS]
    for source in sources:
        if not source.exists():
            continue
        target = STAGE / 'inputs' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        report['input_snapshots'][source.name] = digest(target)
    (STAGE / 'build_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
