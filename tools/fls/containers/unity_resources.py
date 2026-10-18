"""Patch remaining Steam text fields in-place using native font fallbacks."""
import copy
import hashlib
import json
import re
import struct
from pathlib import Path
from fls.core.path_compat import WORK
from fls.core.file_hash import sha256 as digest
from candidate import stage_path
from path_compat import REPO

UNITY_SOURCE = WORK / 'assets/sources/unity'
UNITY_INPUT = WORK / 'assets/sources/unity.json'
CORPUS = REPO / 'translation/corpus.json'
BASELINE = UNITY_SOURCE
FILES = ['S-Tribute_Data/resources.assets', 'S-Tribute_Data/level0']

# Exact byte offsets of target Unity objects within pristine baseline files.
OBJECT_STARTS = {
    ('S-Tribute_Data/level0', 147): 19912,
    ('S-Tribute_Data/resources.assets', 2002): 30778152,
    ('S-Tribute_Data/resources.assets', 2127): 31960056,
    ('S-Tribute_Data/resources.assets', 2134): 31963064,
    ('S-Tribute_Data/resources.assets', 2164): 31976120,
    ('S-Tribute_Data/resources.assets', 2165): 31976696,
}


def _rows_for_language(language, rows):
    inputs = json.loads(UNITY_INPUT.read_text(encoding='utf-8'))
    if rows is None:
        corpus = json.loads(CORPUS.read_text(encoding='utf-8'))
        rows = [row for row in corpus['rows'] if row.get('kind') == 'unity']
    by_id = {row['id']: row for row in rows}
    assert len(inputs) == len(rows) == len(by_id)
    normalized = []
    for before in inputs:
        row = by_id[before['id']]
        for key, value in before.items():
            if key not in {'translation', 'kind'}:
                assert row.get(key) == value, (before['id'], key)
        text = row.get('translations', {}).get(language) or row.get('translation')
        assert isinstance(text, str) and text.strip(), before['id']
        normalized.append({**before, 'translation': text})
    return inputs, normalized


def build(stage, rows=None, language='zh-CN'):
    stage = stage_path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    inputs, rows = _rows_for_language(language, rows)
    seen = {}
    pattern = r'<sprite="TMP_Buttons" index=\d+>'
    for before, row in zip(inputs, rows):
        assert all(row[k] == v for k, v in before.items() if k != 'translation'), row['id']
        assert row['translation'] and len(row['translation'].encode('utf-8')) <= row['capacity_bytes'], row['id']
        assert re.findall(pattern, row['source']) == re.findall(pattern, row['translation']), row['id']
        if row['source'] in seen:
            assert seen[row['source']] == row['translation']
        seen[row['source']] = row['translation']

    original_hash_path = BASELINE / 'unity_original_files.json'
    original_hashes = json.loads(original_hash_path.read_text(encoding='utf-8'))

    reports = []
    for filename in FILES:
        baseline = BASELINE / filename
        expected = original_hashes.get(filename)
        if not expected or not baseline.is_file() or digest(baseline) != expected:
            raise ValueError('Frozen Unity source identity mismatch: ' + filename)

        orig_data = baseline.read_bytes()
        patched_data = bytearray(orig_data)

        file_patches = [r for r in rows if r['file'] == filename]
        modified_objects = set()

        for row in file_patches:
            key = (filename, row['path_id'])
            if key not in OBJECT_STARTS:
                raise KeyError(f'Unknown object offset for {key}')
            base_offset = OBJECT_STARTS[key]
            off = base_offset + row['object_offset']
            capacity = row['capacity_bytes']

            # Verify capacity prefix and original source string
            cap_check = struct.unpack_from('<I', patched_data, off - 4)[0]
            assert cap_check == capacity, f"Capacity header mismatch for {row['id']}"
            src_bytes = row['source'].encode('utf-8')
            assert patched_data[off:off + len(src_bytes)] == src_bytes, f"Source string mismatch for {row['id']}"

            # In-place write with space padding to preserve exact size and byte alignment
            payload = row['translation'].encode('utf-8')
            padded = payload + b' ' * (capacity - len(payload))
            patched_data[off:off + capacity] = padded
            modified_objects.add(row['path_id'])

        target = stage / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(patched_data)

        # Verify patched strings
        for row in file_patches:
            base_offset = OBJECT_STARTS[(filename, row['path_id'])]
            off = base_offset + row['object_offset']
            capacity = row['capacity_bytes']
            found = patched_data[off:off + capacity].decode('utf-8').rstrip()
            assert found == row['translation'].rstrip(), f"Patched string check failed for {row['id']}"

        reports.append({
            'file': filename,
            'original_sha256': digest(baseline),
            'patched_sha256': digest(target),
            'original_size': baseline.stat().st_size,
            'size': target.stat().st_size,
            'translated_strings': len(file_patches),
            'modified_objects': sorted(modified_objects),
            'other_objects_unchanged': True
        })

    record = {
        'status': 'passed',
        'translated_strings': len(rows),
        'unique_source_strings': len(seen),
        'font_source': 'Native TMP SDF Font Fallback (FZHTK & YDGothic)',
        'files': reports,
        'images_modified': False,
        'game_launched': False
    }
    (stage / 'unity_report.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    (stage / 'inputs').mkdir(exist_ok=True)
    (stage / 'inputs/unity.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')

    report_path = stage / 'build_report.json'
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding='utf-8'))
        report['core_translated_texts'] = report.get('core_translated_texts', report.get('translated_texts', 0))
        report['host_strings'] = report.get('exe_patch', {}).get('host_strings', 0)
        report['unity_strings'] = len(rows)
        report['translated_texts'] = report['core_translated_texts'] + report['host_strings'] + len(rows)
        report['unity_report'] = 'unity_report.json'
        inputs_dir = stage / 'inputs'
        snapshots = {p.name: digest(p) for p in sorted(inputs_dir.iterdir(), key=lambda path: path.name) if p.is_file()}
        report['input_snapshots'] = snapshots
        for item in reports:
            report['files'][item['file']] = {k: item[k] for k in ['original_sha256', 'patched_sha256', 'size']}
        # Remove sharedassets0.assets if it existed in prior report
        if 'S-Tribute_Data/sharedassets0.assets' in report.get('files', {}):
            del report['files']['S-Tribute_Data/sharedassets0.assets']
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    resource_report_path = stage / 'resource_report.json'
    if resource_report_path.is_file():
        resource_report = json.loads(resource_report_path.read_text(encoding='utf-8'))
        inputs_dir = stage / 'inputs'
        snapshots = {p.name: digest(p) for p in sorted(inputs_dir.iterdir(), key=lambda path: path.name) if p.is_file()}
        resource_report['input_snapshots'] = snapshots
        resource_report_path.write_text(json.dumps(resource_report, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps(record, ensure_ascii=False, indent=2))
    return record
