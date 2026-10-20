"""Test dialogue layout engine across Chinese, Korean, and English for Farland Saga II."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fls.containers.unifont_bitmap import Unifont
from fls.containers.multilingual_layout import render_dialogue


class TestFS2MultilingualLayout(unittest.TestCase):
    def test_multilingual_rendering(self):
        font = Unifont()
        samples = [
            ('zh-CN', '你好，阿特拉克塔！\n这是一段测试对白。'),
            ('ko-KR', '안녕하세요, 아틀락타!\n이것은 테스트 대사입니다.'),
            ('en-US', 'Hello, Attracta!\nThis is a dialogue layout test.'),
        ]
        for lang, text in samples:
            img, metrics = render_dialogue(font, text, lang=lang)
            self.assertEqual(img.size, (196, 48))
            self.assertTrue(1 <= metrics['lines'] <= 3)
            self.assertIn(metrics['font_pixels'], (12, 14, 16))


if __name__ == '__main__':
    unittest.main()
