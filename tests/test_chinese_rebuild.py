"""Ensure fresh Chinese text spans fully replace stale candidate bytes."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fls.builders.build_full_languages import _copy_fresh_span, _encode_zh_condition
from fls.storage.candidate import _copy_report, verify_builder_seed
import fls.core.path_compat as path_compat

WORK = path_compat.WORK


class ChineseRebuild(unittest.TestCase):
    def test_partial_candidate_accepts_legacy_ready_ancestor(self):
        # 全新工作区没有 analysis/，先建出来供 TemporaryDirectory 使用。
        (WORK / 'analysis').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORK / 'analysis') as temporary:
            root = Path(temporary)
            ready = root / 'legacy-alpha06'
            partial = root / 'partial-resources'
            ready_report = {
                'verification': 'verified legacy ready candidate',
                'files': {'FarlandSaga.bin': {'patched_sha256': 'bin-sha', 'size': 4096}},
            }
            partial_report = {
                'build_status': 'resources_only',
                'previous_candidate': ready.name,
                'source_candidate': ready.relative_to(WORK).as_posix(),
                'files': {},
                'omitted_files': ['FarlandSaga.bin'],
            }
            reports = {ready.resolve(): ready_report, partial.resolve(): partial_report}

            with patch('fls.storage.candidate.read_report', side_effect=lambda path: reports[Path(path).resolve()]), \
                 patch('fls.storage.candidate.verify_candidate', side_effect=lambda path: reports[Path(path).resolve()]), \
                 patch('fls.storage.candidate._verify_resource_only_files'), \
                 patch('fls.storage.candidate._verify_resource_only_resources'):
                report, ancestor = verify_builder_seed(partial)

            self.assertIs(report, partial_report)
            self.assertIs(ancestor, ready_report)
            self.assertNotIn('build_status', ready_report)
            self.assertIn('FarlandSaga.bin', ready_report['files'])

    def test_derived_report_does_not_mutate_prior_or_ready_ancestor_files(self):
        ready = {
            'build_status': 'candidate_ready',
            'files': {
                'FarlandSaga.bin': {'patched_sha256': 'bin-ready', 'size': 4096},
                'FarlandSaga.exe': {'patched_sha256': 'exe-ready', 'size': 2048},
            },
        }
        prior, ready_ancestor = ready, ready
        derived = _copy_report(prior)
        derived['files'].pop('FarlandSaga.bin')
        derived['files']['FarlandSaga.exe']['patched_sha256'] = 'exe-resource-only'

        self.assertIn('FarlandSaga.bin', prior['files'])
        self.assertEqual(prior['files']['FarlandSaga.bin']['patched_sha256'], 'bin-ready')
        self.assertEqual(prior['files']['FarlandSaga.exe']['patched_sha256'], 'exe-ready')
        self.assertIs(ready_ancestor, prior)
        self.assertEqual(ready_ancestor['files'], prior['files'])

    def test_shortened_text_clears_old_suffix(self):
        candidate = {'SYSTEM/ITEM.DAT': b'prefixOLDTEXT!tail'}
        fresh = {'SYSTEM/ITEM.DAT': b'prefixNew\0\0\0\0\0tail'}
        _copy_fresh_span(candidate, fresh, 'SYSTEM/ITEM.DAT', 6, 8)
        self.assertEqual(candidate['SYSTEM/ITEM.DAT'], b'prefixNew\0\0\0\0\0tail')
        self.assertEqual(candidate['SYSTEM/ITEM.DAT'][6:14], b'New\0\0\0\0\0')

    def test_seed_ui_ranges_outside_declared_text_interval_stay_untouched(self):
        candidate = {'SYSTEM/MAIN.DAT': b'A' * 4 + b'UI-TILE' + b'B' * 8}
        fresh = {'SYSTEM/MAIN.DAT': b'1' * 4 + b'BAD-TILE' + b'2' * 8}
        _copy_fresh_span(candidate, fresh, 'SYSTEM/MAIN.DAT', 0, 4)
        self.assertEqual(candidate['SYSTEM/MAIN.DAT'], b'1111UI-TILEBBBBBBBB')

    def test_chinese_condition_uses_shared_glyph_map_not_cp932_encoding(self):
        row = {'id': '1ST.BIN:0x75388', 'mode': 'fixed', 'capacity_bytes': 4,
               'translation': '冻结'}
        mapping = {'冻': b'\xe0\x40', '结': b'\xe0\x41', '石': b'\xe0\x42', '　': b'\x81\x40'}
        self.assertEqual(_encode_zh_condition(row, mapping), b'\xe0\x40\xe0\x41')
        row['translation'] = '冻'
        self.assertEqual(_encode_zh_condition(row, mapping), b'\xe0\x40\x81\x40')
        row['translation'] = '冻结石'
        with self.assertRaisesRegex(ValueError, 'overflow'):
            _encode_zh_condition(row, mapping)


if __name__ == '__main__':
    unittest.main(verbosity=2)
