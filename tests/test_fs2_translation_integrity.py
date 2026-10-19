"""Test translation integrity and completeness for Farland Saga II."""
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class TestFS2TranslationIntegrity(unittest.TestCase):
    def test_corpus_completeness(self):
        corpus_path = REPO / 'translation/fs2/corpus.json'
        self.assertTrue(corpus_path.exists())
        corpus = json.loads(corpus_path.read_text(encoding='utf-8'))
        self.assertEqual(corpus['count'], 2660)
        self.assertEqual(len(corpus['rows']), 2660)

        blank_ids = {2177, 3021, 5171}
        for lang in ('zh-CN', 'ko-KR', 'en-US'):
            translated = 0
            for r in corpus['rows']:
                t = r['translations'].get(lang, '').strip()
                if t:
                    translated += 1
                else:
                    self.assertTrue(r['id'] in blank_ids or r.get('blank'))
            self.assertEqual(translated, 2657, f"{lang} expected 2657 translated, got {translated}")

    def test_glossary(self):
        glossary_path = REPO / 'translation/fs2/glossary.json'
        self.assertTrue(glossary_path.exists())
        glossary = json.loads(glossary_path.read_text(encoding='utf-8'))
        self.assertIn('characters', glossary)
        self.assertIn('locations', glossary)
        self.assertIn('terms', glossary)
        self.assertIn('カリン', glossary['characters'])
        self.assertEqual(glossary['characters']['カリン']['zh-CN'], '卡琳')
        self.assertEqual(glossary['characters']['カリン']['ko-KR'], '카린')
        self.assertEqual(glossary['characters']['カリン']['en-US'], 'Karin')


if __name__ == '__main__':
    unittest.main()
