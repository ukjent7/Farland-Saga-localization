"""Package candidate into distributable Format 3 sector patch payload zip archives.

Format 3 farland-saga-patch:
manifest.json + sectors.bin.gz (gzip stream: sequence of <lba uint32, len uint32, payload 2048 bytes>).
Contains ONLY the modified plain sector payloads (100% free of proprietary game code/assets).
The standalone patcher reconstructs the Steam container cipher and EDC/ECC in place at runtime,
achieving ~4MB per language (~13.5MB total executable) instead of ~96MB uncompressible ciphertext diffs.
"""
import argparse
import gzip
import io
import json
from pathlib import Path
import struct
import zipfile

import os

REPO = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get('FARLAND_FS2_WORK', '/mnt/e/Steam/steamapps/common/FARLAND SAGA II Saturn Tribute/work'))

ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def create_patch_payload(candidate_dir, output_zip, game_root=None):
    candidate_dir = Path(candidate_dir)
    output_zip = Path(output_zip)
    if game_root is None:
        game_root = WORK.parent
    else:
        game_root = Path(game_root)

    source_manifest = json.loads((REPO / 'config/fs2/source_manifest.json').read_text(encoding='utf-8'))
    baseline = source_manifest['files']

    disc_report = json.loads((candidate_dir / 'disc_report.json').read_text(encoding='utf-8'))
    story_report = None
    story_input_name = disc_report.get('story_input', '')
    if story_input_name:
        story_report_path = candidate_dir / 'input_records' / story_input_name / 'story_report.json'
        if story_report_path.exists():
            story_report = json.loads(story_report_path.read_text(encoding='utf-8'))

    lang = story_report.get('language', 'zh-CN') if story_report else 'zh-CN'
    if 'ko-KR' in candidate_dir.name:
        lang = 'ko-KR'
    elif 'en-US' in candidate_dir.name:
        lang = 'en-US'

    orig_sha256 = baseline['FarlandSaga2.bin']['sha256']
    orig_size = baseline['FarlandSaga2.bin']['bytes']
    cand_size = disc_report['bytes']
    cand_sha256 = disc_report['candidate_sha256']

    # Collect all changed allocations
    archive_manifest = json.loads((WORK / 'extracted/resources/_manifest.json').read_text(encoding='utf-8'))
    resource_index = {r['id']: r for r in archive_manifest['resources']}

    res_input_name = disc_report.get('resource_input', '')
    blocks = []
    if res_input_name:
        res_dir = WORK / 'staging' / res_input_name / 'resources'
        if not res_dir.exists():
            res_dir = candidate_dir / 'input_records' / res_input_name / 'resources'
        for p in sorted(res_dir.glob('*.bin')):
            blocks.append({'path': p, 'archive_offset': resource_index[p.stem]['archive_offset']})

    if story_input_name:
        story_dir = WORK / 'staging' / story_input_name
        if not (story_dir / 'story_report.json').exists():
            story_dir = candidate_dir / 'input_records' / story_input_name
        s_rep = json.loads((story_dir / 'story_report.json').read_text(encoding='utf-8'))
        for r in s_rep['patches']:
            blocks.append({'path': story_dir / r['block'], 'archive_offset': r['archive_offset']})

    # Write sectors.bin.gz
    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode='wb', compresslevel=9) as gz:
        sector_count = 0
        for b in blocks:
            data = b['path'].read_bytes()
            offset = b['archive_offset']
            for pos in range(0, len(data), 2048):
                lba = 178 + (offset + pos) // 2048
                chunk = data[pos:pos+2048]
                gz.write(struct.pack('<II', lba, len(chunk)))
                gz.write(chunk)
                sector_count += 1

    gz_bytes = gz_buf.getvalue()

    manifest_obj = {
        'format': 3,
        'kind': 'farland-saga-patch',
        'game': 'FARLAND SAGA II Saturn Tribute',
        'version': candidate_dir.name,
        'language': lang,
        'files': [
            {
                'path': 'FarlandSaga2.bin',
                'original_sha256': orig_sha256,
                'original_size': orig_size,
                'patched_sha256': cand_sha256,
                'patched_size': cand_size,
                'sectors_object': 'sectors.bin.gz',
                'sector_count': sector_count,
            }
        ]
    }

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        manifest_data = json.dumps(manifest_obj, ensure_ascii=False, indent=2).encode('utf-8')
        zi_manifest = zipfile.ZipInfo('manifest.json', date_time=ZIP_TIMESTAMP)
        zi_manifest.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi_manifest, manifest_data)

        zi_sectors = zipfile.ZipInfo('sectors.bin.gz', date_time=ZIP_TIMESTAMP)
        zi_sectors.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi_sectors, gz_bytes)

    print(f"Created Format 3 payload: {output_zip} ({output_zip.stat().st_size // 1024} KiB, {sector_count} sectors)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', help='Candidate directory path')
    parser.add_argument('--output', help='Output zip path')
    parser.add_argument('--lang', help='Language code (zh-CN, ko-KR, en-US)')
    args = parser.parse_args()

    if args.candidate and args.output:
        create_patch_payload(args.candidate, args.output)
    else:
        # Build all three
        targets = [
            ('zh-CN-candidate01', 'zh-CN', REPO / 'patcher2/payload/farland-patch-zh-CN.zip'),
            ('ko-KR-candidate02', 'ko-KR', REPO / 'patcher2/payload/farland-patch-ko-KR.zip'),
            ('en-US-candidate02', 'en-US', REPO / 'patcher2/payload/farland-patch-en-US.zip'),
        ]
        for stage_name, lang, out_zip in targets:
            cand_path = WORK / 'staging' / stage_name
            if cand_path.exists():
                create_patch_payload(cand_path, out_zip)
            else:
                print(f"Skipping {stage_name}: candidate does not exist at {cand_path}")


if __name__ == '__main__':
    main()
