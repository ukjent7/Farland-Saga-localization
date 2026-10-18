"""Unity 文字行表提取器测试；重放用例需要私有工作区的冻结素材。"""
import contextlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))

import extract_unity_text
import path_compat

WORK = path_compat.WORK
FROZEN = WORK / 'assets/sources/unity'
FROZEN_TABLE = (
    REPO / 'config/unity.json'
    if (REPO / 'config/unity.json').is_file()
    else WORK / 'assets/sources/unity.json'
)


def scan_rows():
    """三行假命中：本地化条目、可见文字、声音列表元数据。"""
    return [
        {'id': 'resources.assets:2002:64', 'file': 'S-Tribute_Data/resources.assets', 'path_id': 2002,
         'object_offset': 64, 'capacity_bytes': 34, 'source': 'コットン ２を開始します', 'kind': 'localization'},
        {'id': 'resources.assets:2127:92', 'file': 'S-Tribute_Data/resources.assets', 'path_id': 2127,
         'object_offset': 92, 'capacity_bytes': 90, 'source': 'ステータス表示', 'kind': 'visible_text'},
        {'id': 'resources.assets:2001:48', 'file': 'S-Tribute_Data/resources.assets', 'path_id': 2001,
         'object_offset': 48, 'capacity_bytes': 6, 'source': '無音', 'kind': 'localization'},
    ]


def corpus_document(rows):
    """构造能通过 translation_data 校验的最小语料库。"""
    return {'schema_version': 1, 'source_language': 'ja-JP', 'target_languages': ['zh-CN', 'ko-KR', 'en-US'],
            'rows': [{'id': row['id'], 'file': row['file'], 'path_id': row['path_id'],
                      'object_offset': row['object_offset'], 'capacity_bytes': row['capacity_bytes'],
                      'source': row['source'], 'kind': 'unity', 'max_utf8_bytes': row['capacity_bytes'],
                      'translations': {'zh-CN': '中文', 'ko-KR': '한국어', 'en-US': 'English'}}
                     for row in rows]}


def frozen_unity_rows():
    document = json.loads((REPO / 'translation/corpus.json').read_text(encoding='utf-8'))
    return [row for row in document['rows'] if row.get('kind') == 'unity']


def segment(text):
    """把一个字符串打包成 4 字节小端长度加 UTF-8 数据。"""
    payload = text.encode('utf-8')
    return struct.pack('<I', len(payload)) + payload


class ScriptFilters(unittest.TestCase):
    def test_kana_and_han_boundaries(self):
        self.assertTrue(extract_unity_text.kana('コットン'))
        self.assertTrue(extract_unity_text.kana('ひらがな'))
        self.assertFalse(extract_unity_text.kana('文字列'))
        self.assertFalse(extract_unity_text.kana('plain ascii'))
        self.assertTrue(extract_unity_text.han('文字列'))
        self.assertFalse(extract_unity_text.han('ひらがなのみ'))

    def test_hangul_and_non_ascii_latin(self):
        self.assertTrue(extract_unity_text.hangul('한국어'))
        self.assertFalse(extract_unity_text.hangul('日本語'))
        self.assertTrue(extract_unity_text.non_ascii_latin('café crème'))
        self.assertFalse(extract_unity_text.non_ascii_latin('漢字'))
        self.assertFalse(extract_unity_text.non_ascii_latin('あいう'))

    def test_text_candidate_conditions(self):
        self.assertTrue(extract_unity_text.is_text_candidate('コットン ２を開始します'))
        self.assertFalse(extract_unity_text.is_text_candidate('   '))
        self.assertFalse(extract_unity_text.is_text_candidate('ascii only'))
        self.assertFalse(extract_unity_text.is_text_candidate('日本語\x00'))
        self.assertFalse(extract_unity_text.is_text_candidate('日本語\n注記'))

    def test_japanese_filter_keeps_kana_and_pure_han(self):
        self.assertTrue(extract_unity_text.is_japanese_text('こんにちは'))
        self.assertTrue(extract_unity_text.is_japanese_text('漢字のみの文'))
        self.assertFalse(extract_unity_text.is_japanese_text('한국어 텍스트'))
        self.assertFalse(extract_unity_text.is_japanese_text('漢字한국어'))
        self.assertFalse(extract_unity_text.is_japanese_text('café crème'))
        # 假名优先：混有谚文但带假名的行仍然保留
        self.assertTrue(extract_unity_text.is_japanese_text('テスト한국어'))

    def test_classify_kind(self):
        self.assertEqual(extract_unity_text.classify_kind('TextMeshPro'), 'visible_text')
        self.assertEqual(extract_unity_text.classify_kind('TextMeshProUGUI'), 'visible_text')
        self.assertEqual(extract_unity_text.classify_kind('InfomationList'), 'localization')
        self.assertEqual(extract_unity_text.classify_kind('BGMList'), 'localization')


class ByteScan(unittest.TestCase):
    def test_adjacent_segments_are_both_found(self):
        raw = segment('コットン ２を開始します') + segment('ステータス表示')
        first = 'コットン ２を開始します'.encode('utf-8')
        second = 'ステータス表示'.encode('utf-8')
        self.assertEqual(extract_unity_text.scan_bytes(raw),
                         [(4, len(first), 'コットン ２を開始します'),
                          (8 + len(first), len(second), 'ステータス表示')])

    def test_rejected_segment_only_advances_one_byte(self):
        # 未保留的 ASCII 段后面紧跟日字段落；只有逐字节前进才能找到后者
        raw = segment('not japanese') + segment('決定')
        self.assertEqual([hit[2] for hit in extract_unity_text.scan_bytes(raw)], ['決定'])

    def test_control_characters_are_rejected(self):
        for text in ('日本語\x00', '日本語\n注記', '日本語\r注記'):
            self.assertEqual(extract_unity_text.scan_bytes(segment(text)), [], text)

    def test_oversized_segments_are_rejected(self):
        text = 'あ' * 1366  # 4098 字节，超过 MAX_BYTES
        self.assertGreater(len(text.encode('utf-8')), extract_unity_text.MAX_BYTES)
        self.assertEqual(extract_unity_text.scan_bytes(segment(text)), [])

    def test_other_scripts_are_rejected(self):
        for text in ('한국어 텍스트', 'café crème', '漢字한국어'):
            self.assertEqual(extract_unity_text.scan_bytes(segment(text)), [], text)


class RowShape(unittest.TestCase):
    def test_row_id_uses_basename(self):
        self.assertEqual(extract_unity_text.row_id('resources.assets', 2002, 64), 'resources.assets:2002:64')
        self.assertEqual(extract_unity_text.row_id('level0', 147, 92), 'level0:147:92')

    def test_sort_preserves_scan_order(self):
        rows = [{'file': 'S-Tribute_Data/resources.assets', 'object_offset': 268, 'path_id': 2002, 'id': 'c'},
                {'file': 'S-Tribute_Data/resources.assets', 'object_offset': 64, 'path_id': 2002, 'id': 'a'},
                {'file': 'S-Tribute_Data/resources.assets', 'object_offset': 92, 'path_id': 2134, 'id': 'e'},
                {'file': 'S-Tribute_Data/level0', 'object_offset': 92, 'path_id': 147, 'id': 'b'},
                {'file': 'S-Tribute_Data/resources.assets', 'object_offset': 92, 'path_id': 2127, 'id': 'd'}]
        # 扫描顺序由冻结原版决定，重新排序会让重建结果的哈希无故变化。
        self.assertEqual([row['id'] for row in extract_unity_text.sort_rows(rows)],
                         ['c', 'a', 'e', 'b', 'd'])
        self.assertIsNot(extract_unity_text.sort_rows(rows), rows)

    def test_output_row_field_order(self):
        row = extract_unity_text.output_row(scan_rows()[0])
        self.assertEqual(list(row), ['id', 'file', 'path_id', 'object_offset', 'capacity_bytes',
                                     'source', 'translation', 'kind'])
        self.assertEqual(row['translation'], '')
        self.assertEqual(row['kind'], 'localization')

    def test_output_row_defaults_kind(self):
        row = {'id': 'x:1:2', 'file': 'f', 'path_id': 1, 'object_offset': 2,
               'capacity_bytes': 3, 'source': 'テスト'}
        self.assertEqual(extract_unity_text.output_row(row)['kind'], 'localization')


class Alignment(unittest.TestCase):
    def test_align_splits_matched_and_unmatched(self):
        formal, review = extract_unity_text.align(scan_rows(), corpus_document(scan_rows()[:2])['rows'])
        self.assertEqual([row['id'] for row in formal],
                         ['resources.assets:2002:64', 'resources.assets:2127:92'])
        self.assertEqual([row['id'] for row in review], ['resources.assets:2001:48'])
        self.assertTrue(all(row['translation'] == '' for row in formal + review))

    def test_align_reports_missing_corpus_rows(self):
        with self.assertRaisesRegex(ValueError, 'resources.assets:2127:92'):
            extract_unity_text.align(scan_rows()[:1], corpus_document(scan_rows())['rows'])

    def test_align_accepts_empty_corpus(self):
        formal, review = extract_unity_text.align(scan_rows(), [])
        self.assertEqual(formal, [])
        self.assertEqual(len(review), 3)

    def test_compare_rows_ignores_order(self):
        rows = scan_rows()
        self.assertEqual(extract_unity_text.compare_rows(rows, list(reversed(rows))), [])

    def test_compare_rows_lists_field_drift(self):
        after = [dict(row) for row in scan_rows()]
        after[0]['source'] = '别的内容'
        problems = extract_unity_text.compare_rows(scan_rows(), after)
        self.assertEqual(len(problems), 1)
        self.assertIn('resources.assets:2002:64', problems[0])

    def test_index_rows_rejects_duplicates(self):
        rows = scan_rows() + [dict(scan_rows()[0])]
        with self.assertRaisesRegex(ValueError, '重复 id'):
            extract_unity_text.index_rows(rows)


class CommandLine(unittest.TestCase):
    def setUp(self):
        (WORK / '.tmp').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'unity'
        self.source.mkdir()
        self.output = self.root / 'unity.json'
        self.review = self.root / 'unity_unmatched.json'
        self.corpus = self.root / 'corpus.json'
        self.corpus.write_text(json.dumps(corpus_document(scan_rows()[:2]), ensure_ascii=False),
                               encoding='utf-8')

    def run_tool(self, *arguments):
        """用假扫描结果跑一遍命令行，返回 (stdout, stderr, 退出码)。"""
        with patch.object(extract_unity_text, 'scan', lambda source: scan_rows()):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    try:
                        extract_unity_text.main(['--source', str(self.source), '--output', str(self.output),
                                                 '--review', str(self.review), '--corpus', str(self.corpus),
                                                 *arguments])
                        code = 0
                    except SystemExit as error:
                        code = error.code
        return out.getvalue(), err.getvalue(), code

    def test_generate_writes_table_and_review(self):
        out, err, code = self.run_tool()
        self.assertEqual((code, err), (0, ''))
        formal = json.loads(self.output.read_text(encoding='utf-8'))
        review = json.loads(self.review.read_text(encoding='utf-8'))
        self.assertEqual([row['id'] for row in formal],
                         ['resources.assets:2002:64', 'resources.assets:2127:92'])
        self.assertEqual([row['id'] for row in review], ['resources.assets:2001:48'])
        self.assertIn('resources.assets:2001:48', out)
        self.assertIn('無音', out)

    def test_repeated_generate_skips_identical_write(self):
        self.run_tool()
        first = self.output.stat().st_mtime_ns
        out, err, code = self.run_tool()
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(self.output.stat().st_mtime_ns, first)
        self.assertIn('跳过写入', out)

    def test_check_passes_when_output_matches(self):
        self.run_tool()
        out, err, code = self.run_tool('--check')
        self.assertEqual((code, err), (0, ''))
        summary = json.loads(out)
        self.assertEqual(summary['status'], 'passed')
        self.assertTrue(summary['output_matches'])
        self.assertEqual(summary['matched_rows'], 2)
        self.assertEqual(summary['review_rows'], 1)
        self.assertEqual(summary['missing_rows'], 0)

    def test_check_reports_output_drift(self):
        self.run_tool()
        rows = json.loads(self.output.read_text(encoding='utf-8'))
        rows[0]['source'] = '被改过的原文'
        self.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        out, err, code = self.run_tool('--check')
        self.assertEqual(code, 1)
        self.assertIn('resources.assets:2002:64', err)

    def test_check_requires_existing_output(self):
        out, err, code = self.run_tool('--check')
        self.assertEqual(code, 1)
        self.assertIn('不存在', err)

    def test_missing_corpus_row_fails(self):
        document = corpus_document(scan_rows())
        document['rows'].append({'id': 'resources.assets:9999:1', 'file': 'S-Tribute_Data/resources.assets',
                                 'path_id': 9999, 'object_offset': 1, 'capacity_bytes': 4, 'source': '漂移',
                                 'kind': 'unity', 'max_utf8_bytes': 4,
                                 'translations': {'zh-CN': '漂', 'ko-KR': '표', 'en-US': 'drift'}})
        self.corpus.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        out, err, code = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn('resources.assets:9999:1', err)

    def test_corpus_without_unity_rows_is_refused(self):
        self.corpus.write_text(json.dumps({'schema_version': 1, 'source_language': 'ja-JP',
                                           'target_languages': ['zh-CN', 'ko-KR', 'en-US'], 'rows': []},
                                          ensure_ascii=False), encoding='utf-8')
        out, err, code = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn('kind == "unity"', err)

    def test_output_outside_workspace_is_refused(self):
        outside = path_compat.WORKSPACE / 'extract_unity_text_probe.json'
        out, err, code = self.run_tool('--output', str(outside))
        self.assertEqual(code, 1)
        self.assertIn('私有工作区', err)
        self.assertFalse(outside.exists())

    def test_scan_requires_the_frozen_directory(self):
        with self.assertRaisesRegex(ValueError, '不存在'):
            extract_unity_text.scan(self.root / 'missing')

    def test_scan_requires_the_three_files(self):
        with self.assertRaisesRegex(ValueError, 'resources.assets'):
            extract_unity_text.scan(self.source)


@unittest.skipUnless(FROZEN.is_dir(), '私有工作区没有冻结 Unity 素材，跳过重放')
class FrozenReplay(unittest.TestCase):
    """对私有工作区的冻结素材重放扫描，核对与历史 unity.json 一致。"""

    @classmethod
    def setUpClass(cls):
        cls.scanned = extract_unity_text.scan(FROZEN)
        cls.formal, cls.review = extract_unity_text.align(cls.scanned, frozen_unity_rows())
        cls.frozen = json.loads(FROZEN_TABLE.read_text(encoding='utf-8'))

    def test_scan_reproduces_the_frozen_table(self):
        self.assertEqual(len(self.formal), 67)
        self.assertEqual(extract_unity_text.compare_rows(self.frozen, self.formal), [])

    def test_visible_text_rows_come_from_textmesh_components(self):
        visible = [row for row in self.formal if row['kind'] == 'visible_text']
        self.assertEqual(len(visible), 5)
        self.assertEqual({row['path_id'] for row in visible}, {2127, 2134, 2164, 2165, 147})

    def test_review_holds_only_sound_list_metadata(self):
        self.assertTrue(self.review)
        self.assertEqual({row['path_id'] for row in self.review}, {2001, 2003})
        for row in self.review:
            self.assertTrue(extract_unity_text.is_japanese_text(row['source']))

    def test_check_command_accepts_the_frozen_table(self):
        with patch.object(extract_unity_text, 'scan', lambda source: self.scanned):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                extract_unity_text.main(['--check'])
        summary = json.loads(out.getvalue())
        self.assertEqual(summary['status'], 'passed')
        self.assertEqual(summary['matched_rows'], 67)
        self.assertEqual(summary['review_rows'], len(self.review))

    def test_generate_to_a_scratch_path_matches_the_frozen_table(self):
        (WORK / '.tmp').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORK / '.tmp') as scratch:
            output = Path(scratch) / 'unity.json'
            review = Path(scratch) / 'unity_unmatched.json'
            with patch.object(extract_unity_text, 'scan', lambda source: self.scanned):
                with contextlib.redirect_stdout(io.StringIO()):
                    extract_unity_text.main(['--output', str(output), '--review', str(review)])
            self.assertEqual(extract_unity_text.compare_rows(
                self.frozen, json.loads(output.read_text(encoding='utf-8'))), [])
            self.assertEqual(len(json.loads(review.read_text(encoding='utf-8'))), len(self.review))

    def test_command_line_check_against_the_frozen_workspace(self):
        environment = dict(os.environ, FARLAND_WORK=str(WORK))
        result = subprocess.run([sys.executable, str(REPO / 'tools/extract_unity_text.py'), '--check'],
                                capture_output=True, text=True, env=environment, cwd=REPO)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'passed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
