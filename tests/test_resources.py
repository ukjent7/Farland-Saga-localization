"""Fast isolated regressions for editable resource binding and export guards."""
import copy
import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))

import extract_text
import native_patches
import path_compat

WORK = path_compat.WORK


class FrontendTranslationBinding(unittest.TestCase):
    def setUp(self):
        (WORK / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'frontend.json'

    @staticmethod
    def translations():
        return {
            'label': {'zh-CN': '选项设置', 'ko-KR': '옵션 설정', 'en-US': 'Options'},
            'panel-title': {'zh-CN': '选项', 'ko-KR': '설정', 'en-US': 'Settings'},
        }

    def bind(self, translations, suffix):
        groups = [{'id': identifier, 'text': text}
                  for identifier, text in translations.items()]
        self.source.write_text(json.dumps({'groups': groups}, ensure_ascii=False), encoding='utf-8')
        stage = self.root / suffix
        (stage / 'inputs').mkdir(parents=True)
        recipe = {
            'id': 'frontend-fixture',
            'languages': ['zh-CN', 'ko-KR', 'en-US'],
            'container_atlases': [{
                'translation_source': str(self.source),
                'labels': [{
                    'part': 'fixture-label',
                    'translation_id': 'label',
                    'text': {
                        'zh-CN': '选项\n设置',
                        'ko-KR': '옵션\n설정',
                        'en-US': 'Options',
                    },
                }],
                'panel_translation_ids': {'title': 'panel-title'},
                'panel_text': {
                    'zh-CN': {'title': '选项'},
                    'ko-KR': {'title': '설\n정'},
                    'en-US': {'title': 'Set\ntings'},
                },
            }],
        }
        native_patches._bind_frontend_translations(recipe, SimpleNamespace(stage=stage))
        return recipe

    def test_preserves_layout_and_binds_only_changed_language_text(self):
        authority = self.translations()
        unchanged = self.bind(authority, 'unchanged')
        atlas = unchanged['container_atlases'][0]
        self.assertEqual(atlas['labels'][0]['text']['zh-CN'], '选项\n设置')
        self.assertEqual(atlas['labels'][0]['text']['ko-KR'], '옵션\n설정')
        self.assertEqual(atlas['panel_text']['ko-KR']['title'], '설\n정')
        self.assertEqual(atlas['panel_text']['en-US']['title'], 'Set\ntings')

        changed_korean = copy.deepcopy(authority)
        changed_korean['label']['ko-KR'] = '환경 설정'
        changed_korean['panel-title']['ko-KR'] = '환경 설정'
        korean_update = self.bind(changed_korean, 'korean-update')['container_atlases'][0]
        self.assertEqual(korean_update['labels'][0]['text']['ko-KR'], '환경 설정')
        self.assertEqual(korean_update['panel_text']['ko-KR']['title'], '환경 설정')
        self.assertEqual(korean_update['labels'][0]['text']['zh-CN'], '选项\n设置')

        changed_english = copy.deepcopy(authority)
        changed_english['label']['en-US'] = 'Preferences'
        changed_english['panel-title']['en-US'] = 'Preferences'
        english_update = self.bind(changed_english, 'english-update')['container_atlases'][0]
        self.assertEqual(english_update['labels'][0]['text']['en-US'], 'Preferences')
        self.assertEqual(english_update['panel_text']['en-US']['title'], 'Preferences')
        self.assertEqual(english_update['labels'][0]['text']['ko-KR'], '옵션\n설정')
        self.assertEqual(english_update['panel_text']['ko-KR']['title'], '설\n정')


class CorpusExtractionProtection(unittest.TestCase):
    def setUp(self):
        (WORK / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'translation').mkdir()
        (self.root / 'translation/corpus.json').write_text('{}', encoding='utf-8')

    def test_event_export_refuses_to_create_a_detached_copy(self):
        output = self.root / 'translation/exported'
        with patch.object(extract_text, 'REPO', self.root):
            with self.assertRaisesRegex(RuntimeError, 'authoritative translation corpus exists'):
                extract_text.export(self.root / 'not-read', output)
        self.assertFalse(output.exists())

    def test_catalog_export_refuses_to_create_a_detached_copy(self):
        tools = self.root / 'tools'
        tools.mkdir()
        script = tools / 'extract_catalog.py'
        shutil.copyfile(REPO / 'tools/extract_catalog.py', script)
        with patch.object(path_compat, 'REPO', self.root):
            with self.assertRaisesRegex(RuntimeError, 'authoritative translation corpus exists'):
                runpy.run_path(str(script))
        self.assertFalse((self.root / 'translation/catalog.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
