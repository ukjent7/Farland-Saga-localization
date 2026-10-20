"""Fast fixtures for safe, language-isolated corpus merging and native limits."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import fls.text.merge_full_translations as merger
import fls.core.path_compat as path_compat
from fls.text.translation_data import read_corpus

WORK = path_compat.WORK


class MergeSafety(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=WORK / '.tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus_path = self.root / 'corpus.json'
        self.draft_path = self.root / 'draft.json'
        self.row = {
            'id': 'EXE:fixture:1', 'kind': 'host', 'file': 'FarlandSaga.exe',
            'offset': '0x100', 'capacity_bytes': 7, 'max_utf8_bytes': 6,
            'source': '原文',
            'translations': {'zh-CN': '旧中', 'ko-KR': '기존', 'en-US': 'old'},
            'reviews': {'zh-CN': {'status': 'reviewed', 'notes': 'keep'},
                        'ko-KR': {'review_note': 'ko keep'},
                        'en-US': {'review_note': 'en keep'}},
        }
        self.write_corpus([self.row])

    def write_corpus(self, rows):
        self.corpus_path.write_text(json.dumps({
            'schema_version': 1,
            'source_language': 'ja',
            'target_languages': ['zh-CN', 'ko-KR', 'en-US'],
            'rows': rows,
        }, ensure_ascii=False), encoding='utf-8')

    def write_draft(self, rows):
        self.draft_path.write_text(json.dumps({'translations': rows}, ensure_ascii=False), encoding='utf-8')

    def test_same_id_merge_changes_one_language_and_keeps_reviews(self):
        self.write_draft([{'id': self.row['id'], 'en-US': '123456', 'review_note': 'new en review'}])
        result = merger.merge_draft(self.draft_path, 'en-US', overwrite=True, corpus_path=self.corpus_path)
        self.assertEqual(result['merged'], 1)
        row = read_corpus(self.corpus_path)['rows'][0]
        self.assertEqual(row['id'], self.row['id'])
        self.assertEqual(row['translations'], {'zh-CN': '旧中', 'ko-KR': '기존', 'en-US': '123456'})
        self.assertEqual(row['reviews']['zh-CN'], self.row['reviews']['zh-CN'])
        self.assertEqual(row['reviews']['ko-KR'], self.row['reviews']['ko-KR'])
        self.assertEqual(row['reviews']['en-US']['review_note'], 'new en review')

    def test_host_max_utf8_bytes_boundary_is_inclusive(self):
        self.write_draft([{'id': self.row['id'], 'en-US': '123456'}])
        merger.merge_draft(self.draft_path, 'en-US', overwrite=True, corpus_path=self.corpus_path)
        row = read_corpus(self.corpus_path)['rows'][0]
        with self.assertRaises(AssertionError):
            merger.validate_text(row, 'en-US', '1234567')

    def test_unknown_id_and_sibling_language_changes_are_rejected(self):
        self.write_draft([{'id': 'EXE:fixture:missing', 'en-US': 'new'}])
        with self.assertRaisesRegex(ValueError, 'Unknown ID'):
            merger.merge_draft(self.draft_path, 'en-US', corpus_path=self.corpus_path)
        self.write_draft([{'id': self.row['id'], 'en-US': 'new', 'ko-KR': '변경'}])
        with self.assertRaisesRegex(ValueError, 'another language'):
            merger.merge_draft(self.draft_path, 'en-US', corpus_path=self.corpus_path)

    def test_existing_translation_requires_explicit_overwrite(self):
        self.write_draft([{'id': self.row['id'], 'en-US': 'new'}])
        with self.assertRaisesRegex(ValueError, 'would be replaced'):
            merger.merge_draft(self.draft_path, 'en-US', corpus_path=self.corpus_path)
        merger.merge_draft(self.draft_path, 'en-US', overwrite=True, corpus_path=self.corpus_path)

    def test_item_english_capacity_uses_encoded_bytes_and_reviewed_abbreviation(self):
        row = next(row for row in read_corpus()['rows'] if row['id'] == 'SYSTEM/ITEM.DAT:0x924')
        self.assertTrue(merger.validate_text(row, 'en-US', 'Azure Dragon Staff'))
        self.assertTrue(merger.validate_text(row, 'en-US', '1234567890123456'))
        for value in ('12345678901234567', '123456789012345678'):
            with self.assertRaises(AssertionError):
                merger.validate_text(row, 'en-US', value)

    def test_validate_accepts_canonical_and_language_view_rows_without_after(self):
        canonical = read_corpus(self.corpus_path)['rows'][0]
        self.assertTrue(merger.validate(canonical, language='en-US'))
        view = {key: value for key, value in canonical.items() if key != 'translations'}
        view['translation'] = canonical['translations']['zh-CN']
        self.assertTrue(merger.validate(view, language='zh-CN'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
