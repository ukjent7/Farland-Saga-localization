"""Package candidate into distributable sparse patch payload zip archives.

Format 1 farland-saga-patch:
manifest.json + objects/<sha256>.gz (gzip stream: json header line + binary <QI> records).
Contains ONLY the modified 4KB blocks; 100% free of proprietary game assets.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import struct
import zipfile

REPO = Path(__file__).resolve().parents[1]
WORK = Path(__file__).resolve().parents[2]

BLOCK = 4096
RECORD = struct.Struct('<QI')
READ_SIZE = 1024 * 1024
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def create_patch_payload(candidate_dir, output_zip, game_root=None):
    candidate_dir = Path(candidate_dir)
    output_zip = Path(output_zip)
    if game_root is None:
        game_root = WORK.parent
    else:
        game_root = Path(game_root)

    source_manifest = json.loads((REPO / 'config/source_manifest.json').read_text(encoding='utf-8'))
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

    # Target file is FarlandSaga2.bin
    orig_file = game_root / 'FarlandSaga2.bin'
    cand_file = candidate_dir / 'FarlandSaga2.bin'

    if not orig_file.exists():
        raise FileNotFoundError(f"Original FarlandSaga2.bin not found at {orig_file}")
    if not cand_file.exists():
        raise FileNotFoundError(f"Candidate FarlandSaga2.bin not found at {cand_file}")

    orig_sha256 = baseline['FarlandSaga2.bin']['sha256']
    orig_size = baseline['FarlandSaga2.bin']['bytes']

    cand_size = cand_file.stat().st_size
    cand_sha256 = disc_report['candidate_sha256']

    header = {
        'format': 1,
        'sha256': cand_sha256,
        'size': cand_size,
        'base': {
            'path': 'FarlandSaga2.bin',
            'sha256': orig_sha256,
            'size': orig_size,
        }
    }

    object_rel = f'objects/{cand_sha256}.gz'
    temp_gz = output_zip.parent / f'.temp_{cand_sha256}.gz'
    temp_gz.parent.mkdir(parents=True, exist_ok=True)

    diff_blocks = 0
    diff_bytes = 0

    print(f"Diffing candidate {candidate_dir.name} against baseline...")
    with open(orig_file, 'rb') as f_orig, open(cand_file, 'rb') as f_cand, gzip.open(temp_gz, 'wb', compresslevel=9) as dst:
        dst.write(json.dumps(header).encode('utf-8') + b'\n')
        offset = 0
        while True:
            c_chunk = f_cand.read(READ_SIZE)
            if not c_chunk:
                break
            o_chunk = f_orig.read(len(c_chunk))
            if c_chunk != o_chunk:
                for start in range(0, len(c_chunk), BLOCK):
                    sub_c = c_chunk[start:start + BLOCK]
                    sub_o = o_chunk[start:start + BLOCK]
                    if sub_c != sub_o:
                        dst.write(RECORD.pack(offset + start, len(sub_c)))
                        dst.write(sub_c)
                        diff_blocks += 1
                        diff_bytes += len(sub_c)
            offset += len(c_chunk)

    print(f"Diff complete: {diff_blocks} blocks ({diff_bytes // 1024} KiB). Compressed object: {temp_gz.stat().st_size // 1024} KiB.")

    manifest_obj = {
        'format': 1,
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
                'object': object_rel,
            }
        ]
    }

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        manifest_data = json.dumps(manifest_obj, ensure_ascii=False, indent=2).encode('utf-8')
        zi_manifest = zipfile.ZipInfo('manifest.json', date_time=ZIP_TIMESTAMP)
        zi_manifest.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi_manifest, manifest_data)

        gz_data = temp_gz.read_bytes()
        zi_obj = zipfile.ZipInfo(object_rel, date_time=ZIP_TIMESTAMP)
        # The object is already gzip compressed, store without recompressing
        zi_obj.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi_obj, gz_data)

    temp_gz.unlink(missing_ok=True)
    print(f"Created payload zip: {output_zip} ({output_zip.stat().st_size // 1024} KiB)")


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
