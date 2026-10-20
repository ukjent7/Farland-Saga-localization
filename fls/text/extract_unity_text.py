"""从冻结的 Unity 原版素材重新提取 Steam 外壳（Unity 侧）的文字行表。

社区成员用自己的游戏冻结出 ``assets/sources/unity`` 后，用本工具重建
``unity.json``：全程只读游戏文件，不启动游戏。扫描规则与历史分析产物一致——
在 MonoBehaviour 原始数据上滑动扫描定长 UTF-8 段，命中行按 id 与
``translation/corpus.json`` 里 ``kind == "unity"`` 的行对齐；未被收录的命中
（声音列表元数据等）写入单独的审阅文件，等待人工确认后决定是否补入语料库。

行内容里的 ``kind`` 按 MonoBehaviour 的脚本类名分类：TextMesh 系列组件直接
渲染可见文字，记 ``visible_text``；InfomationList 等脚本数据表记
``localization``。冻结表里 62 行来自 InfomationList、5 行来自 TextMeshPro，
本工具按同一规则复现该分布。
"""
import argparse
import json
import struct
import sys
import tempfile
from pathlib import Path

import UnityPy

from fls.core.path_compat import REPO, WORK, inside_workspace
from fls.text.translation_data import read_corpus_rows

UNITY_SOURCE = WORK / 'assets/sources/unity'
UNITY_INPUT = (
    REPO / 'config/unity.json'
    if (REPO / 'config/unity.json').is_file()
    else WORK / 'assets/sources/unity.json'
)
CORPUS = REPO / 'translation/corpus.json'
REVIEW_NAME = 'unity_unmatched.json'
FILES = ['S-Tribute_Data/resources.assets', 'S-Tribute_Data/sharedassets0.assets', 'S-Tribute_Data/level0']
MIN_BYTES = 2
MAX_BYTES = 4096
DEFAULT_KIND = 'localization'
# TextMesh 组件自己渲染文字；其余脚本（InfomationList、BGMList 等）是数据表。
TEXT_SCRIPTS = frozenset({'TextMeshPro', 'TextMeshProUGUI'})


def kana(text):
    """含平假名或片假名（U+3040..U+30FF）。"""
    return any('\u3040' <= character <= '\u30ff' for character in text)


def han(text):
    """含 CJK 统一表意文字（U+4E00..U+9FFF）。"""
    return any('\u4e00' <= character <= '\u9fff' for character in text)


def hangul(text):
    """含谚文音节（U+AC00..U+D7AF）。"""
    return any('\uac00' <= character <= '\ud7af' for character in text)


def non_ascii_latin(text):
    """含非 ASCII 的拉丁系字母（变音字母、希腊字母等），用于排除其他语言槽。"""
    return any(ord(character) > 127 and character.isalpha() and ord(character) < 0x3000
               for character in text)


def is_text_candidate(text):
    """基本命中条件：去空白后非空、含非 ASCII、无 NUL、无换行。"""
    stripped = text.strip()
    return (bool(stripped) and any(ord(character) > 127 for character in stripped)
            and '\x00' not in text and '\n' not in text and '\r' not in text)


def is_japanese_text(text):
    """文字系统过滤：含假名保留；纯汉字且无谚文、无非 ASCII 拉丁字母时保留。"""
    if kana(text):
        return True
    return han(text) and not hangul(text) and not non_ascii_latin(text)


def classify_kind(script_name):
    """TextMesh 组件记可见文字，其余脚本数据表记本地化条目。"""
    return 'visible_text' if script_name in TEXT_SCRIPTS else DEFAULT_KIND


def row_id(basename, path_id, offset):
    """行 id 用文件 basename，与冻结 unity.json 的历史形状一致。"""
    return f'{basename}:{path_id}:{offset}'


def output_row(row, translation=''):
    """按冻结 unity.json 的字段顺序重建行，保证输出字节级可复现。"""
    return {'id': row['id'], 'file': row['file'], 'path_id': row['path_id'],
            'object_offset': row['object_offset'], 'capacity_bytes': row['capacity_bytes'],
            'source': row['source'], 'translation': translation,
            'kind': row.get('kind') or DEFAULT_KIND}


def sort_rows(rows):
    """保持扫描顺序，使重建结果与历史冻结表字节级一致。

    扫描顺序即 FILES 的顺序加 UnityPy 的对象顺序，由冻结原版本身决定；
    历史 unity.json 正是这个顺序（resources.assets 按 path_id 与偏移，level0 在后），
    重新排序会让哈希无故变化，进而打断候选 inputs 的快照对应关系。
    """
    return list(rows)


def index_rows(rows):
    """按 id 建索引；缺 id 或重复 id 都说明行表已损坏。"""
    indexed = {}
    for row in rows:
        identifier = row.get('id') if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier:
            raise ValueError('行缺少字符串 id：' + repr(row)[:80])
        if identifier in indexed:
            raise ValueError('行表存在重复 id：' + identifier)
        indexed[identifier] = row
    return indexed


def scan_bytes(raw):
    """在单个对象原始数据上滑动扫描定长 UTF-8 文字段。

    位置 i 处读 4 字节小端长度 n；命中并保留时跳过整段，其余任何一步不满足
    都只前进 1 字节——漏跳一代会丢掉紧邻的文字段。
    """
    hits = []
    position = 0
    size = len(raw)
    while position + 4 <= size:
        capacity = struct.unpack_from('<I', raw, position)[0]
        if MIN_BYTES <= capacity <= MAX_BYTES and position + 4 + capacity <= size:
            segment = raw[position + 4:position + 4 + capacity]
            try:
                text = segment.decode('utf-8')
            except UnicodeDecodeError:
                position += 1
                continue
            if is_text_candidate(text) and is_japanese_text(text):
                hits.append((position + 4, capacity, text))
                position += 4 + capacity
                continue
        position += 1
    return hits


def object_kind(obj):
    """按 MonoBehaviour 的脚本类名分类；解析失败按本地化条目处理并告警。"""
    try:
        head = obj.parse_monobehaviour_head()
        script = head.m_Script.deref().parse_as_dict()
        name = script.get('m_ClassName')
    except Exception:
        name = None
    if not name:
        print(f'警告：无法解析对象 {obj.path_id} 的脚本类型，按 {DEFAULT_KIND} 处理', file=sys.stderr)
        return DEFAULT_KIND
    return classify_kind(name)


def scan(source=UNITY_SOURCE):
    """扫描冻结 Unity 原版目录，返回命中行列表（含 kind）。"""
    source = Path(source)
    if not source.is_dir():
        raise ValueError(f'冻结 Unity 原版目录不存在：{source}')
    rows = []
    for relative in FILES:
        path = source / relative
        if not path.is_file():
            raise ValueError(f'冻结 Unity 原版文件缺失：{path}')
        for path_id, obj in dict(UnityPy.load(str(path)).file.objects).items():
            if obj.type is None or obj.type.name != 'MonoBehaviour':
                continue
            raw = obj.get_raw_data()
            if raw is None:
                continue
            hits = scan_bytes(raw)
            if not hits:
                continue
            kind = object_kind(obj)
            for offset, capacity, text in hits:
                rows.append({'id': row_id(Path(relative).name, path_id, offset), 'file': relative,
                             'path_id': path_id, 'object_offset': offset,
                             'capacity_bytes': capacity, 'source': text, 'kind': kind})
    return sort_rows(rows)


def align(rows, corpus_rows):
    """按 id 对齐扫描行与语料库行，返回 (正式行, 待确认行)。

    语料库里有而扫描未命中的行说明冻结原版与语料库已经漂移，直接报错。
    """
    corpus_ids = {row['id'] for row in corpus_rows}
    formal, review = [], []
    for row in rows:
        (formal if row['id'] in corpus_ids else review).append(output_row(row))
    missing = sorted(corpus_ids - {row['id'] for row in rows})
    if missing:
        raise ValueError(f'语料库有 {len(missing)} 行未被冻结原版命中，可能已漂移：' + '、'.join(missing))
    return sort_rows(formal), sort_rows(review)


def compare_rows(before, after):
    """按 id 逐字段比较两批行，返回差异描述列表（对行序不敏感）。"""
    problems = []
    before_by_id = index_rows(before)
    after_by_id = index_rows(after)
    for identifier in sorted(set(before_by_id) | set(after_by_id)):
        left = before_by_id.get(identifier)
        right = after_by_id.get(identifier)
        if left is None:
            problems.append(f'输出缺失 {identifier}')
            continue
        if right is None:
            problems.append(f'输出多余 {identifier}')
            continue
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                problems.append(f'{identifier} 字段 {key}：{left.get(key)!r} != {right.get(key)!r}')
    return problems


def checked_path(value, label):
    """解析并校验路径：只允许私有工作区、公开仓库与系统临时目录。

    临时目录是唯一例外，便于校验时把产物写到 /tmp 而不触碰冻结文件。
    """
    path = Path(value).expanduser().resolve()
    scratch = Path(tempfile.gettempdir()).resolve()
    if not inside_workspace(path) and not path.is_relative_to(scratch):
        raise ValueError(f'{label} 必须位于私有工作区、公开仓库或临时目录内：{path}')
    return path


def write_rows(path, rows):
    """内容相同则跳过写入，避免无谓改动；返回是否实际写入。"""
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    if path.is_file() and path.read_text(encoding='utf-8') == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding='utf-8')
    return True


def check_output(output, formal, review_rows, scanned, corpus_rows, source, corpus):
    """核对现有输出与冻结原版、语料库三者一致，打印 JSON 摘要。"""
    if not output.is_file():
        raise ValueError(f'待校验的输出文件不存在：{output}')
    existing = json.loads(output.read_text(encoding='utf-8'))
    problems = compare_rows(existing, formal)
    if problems:
        shown = '；'.join(problems[:10]) + (f' 等共 {len(problems)} 处' if len(problems) > 10 else '')
        raise ValueError('输出与冻结原版/语料库不一致：' + shown)
    summary = {'status': 'passed', 'source': str(source), 'output': str(output), 'corpus': str(corpus),
               'scanned_rows': len(scanned), 'corpus_unity_rows': len(corpus_rows),
               'matched_rows': len(formal), 'review_rows': len(review_rows), 'missing_rows': 0,
               'output_rows': len(existing), 'output_matches': True}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def generate_output(output, review, formal, review_rows):
    """写入正式行表与审阅文件，并打印待人工确认的条目。"""
    reports = []
    for path, rows in ((output, formal), (review, review_rows)):
        if write_rows(path, rows):
            reports.append(f'已写入 {path}（{len(rows)} 行）')
        else:
            reports.append(f'内容相同，跳过写入 {path}')
    print(f'扫描完成：语料库收录 {len(formal)} 行，待人工确认 {len(review_rows)} 行。')
    if review_rows:
        print('以下命中未被语料库收录，需人工确认（声音列表元数据、其他语言槽等）：')
        for row in review_rows:
            print(f"  {row['id']}  {row['source'][:40]}")
    for line in reports:
        print(line)


def _utf8_stdout():
    """命令行输出中文；测试里重定向的 stdout 没有 reconfigure。"""
    reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding='utf-8')
    except (ValueError, OSError):
        pass


def main(argv=None):
    """命令行入口：成功返回 None，失败打印中文错误并以退出码 1 结束。"""
    _utf8_stdout()
    parser = argparse.ArgumentParser(description='从冻结的 Unity 原版素材重新提取文字行表')
    parser.add_argument('--source', default=str(UNITY_SOURCE), help='冻结 Unity 原版目录')
    parser.add_argument('--output', default=str(UNITY_INPUT), help='输出的 unity.json')
    parser.add_argument('--review', help='未被语料库收录的命中行输出路径，默认与输出同目录')
    parser.add_argument('--check', action='store_true', help='只校验现有输出，不写文件')
    parser.add_argument('--corpus', default=str(CORPUS), help='统一译稿语料库')
    args = parser.parse_args(argv)
    try:
        source = checked_path(args.source, '--source')
        output = checked_path(args.output, '--output')
        review = checked_path(args.review or output.parent / REVIEW_NAME, '--review')
        corpus = checked_path(args.corpus, '--corpus')
        scanned = scan(source)
        corpus_rows = read_corpus_rows(kinds={'unity'}, path=corpus)
        if not corpus_rows:
            raise ValueError(f'语料库中没有 kind == "unity" 的行：{corpus}')
        formal, review_rows = align(scanned, corpus_rows)
        if args.check:
            check_output(output, formal, review_rows, scanned, corpus_rows, source, corpus)
        else:
            generate_output(output, review, formal, review_rows)
    except (ValueError, OSError) as error:
        print(f'错误：{error}', file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == '__main__':
    main()
