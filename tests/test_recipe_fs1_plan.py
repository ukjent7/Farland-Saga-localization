"""Unit tests for the FS1 install-plan compiler (tools/compile_recipe_fs1.py).

All fixtures are synthetic and written to temporary directories; the tests
never read the committed payloads, the game, or the private workspace.
"""
import gzip
import hashlib
import importlib.util
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'compile_recipe_fs1', REPO / 'tools' / 'compile_recipe_fs1.py')
compiler = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault('compile_recipe_fs1', compiler)
_SPEC.loader.exec_module(compiler)

BLOCK = 4096


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pattern(seed: int, size: int) -> bytes:
    out = bytearray(size)
    value = (seed * 7 + 13) & 0xFF
    for index in range(size):
        value = (value * 31 + index + 1) & 0xFF
        out[index] = value
    return bytes(out)


def diff_records(original: bytes, patched: bytes) -> bytes:
    """One diff-object record stream: ascending 4 KiB changed blocks."""
    stream = bytearray()
    for base in range(0, len(patched), BLOCK):
        end = min(base + BLOCK, len(patched))
        orig_end = min(end, len(original))
        if base >= len(original) or original[base:orig_end] != patched[base:end]:
            stream += struct.pack('<QI', base, end - base)
            stream += patched[base:end]
    return bytes(stream)


def build_object(original: bytes, patched: bytes) -> tuple[str, bytes]:
    """Return (object name, gz bytes) for one target file's diff object."""
    patched_hash = sha256_hex(patched)
    header = json.dumps({
        'format': 1,
        'sha256': patched_hash,
        'size': len(patched),
        'base': {'path': 'backups/x/file', 'sha256': sha256_hex(original),
                 'size': len(original)},
    }).encode('utf-8')
    gz = gzip.compress(header + b'\n' + diff_records(original, patched), mtime=0)
    return f'objects/{patched_hash}.gz', gz


def sample_files() -> list[dict]:
    original = pattern(1, 6 * BLOCK + 123)
    patched = bytearray(original)
    patched[BLOCK:2 * BLOCK] = pattern(9, BLOCK)
    shrunk_original = pattern(5, 4 * BLOCK)
    shrunk = pattern(6, 2 * BLOCK + 70)
    exe_original = pattern(3, 3 * BLOCK)
    exe_patched = bytearray(exe_original)
    exe_patched[64:96] = pattern(8, 32)
    return [
        {'name': 'FarlandSaga.bin', 'original': original, 'patched': bytes(patched)},
        {'name': 'FarlandSaga.exe', 'original': exe_original, 'patched': bytes(exe_patched)},
        {'name': 'Texture.tex', 'original': shrunk_original, 'patched': shrunk},
    ]


def write_language_zip(directory: Path, language: str, version: str,
                       files: list[dict], mutate_entry=None) -> Path:
    objects = {}
    manifest_files = []
    for item in files:
        name, gz = build_object(item['original'], item['patched'])
        objects[name] = gz
        entry = {
            'path': item['name'],
            'original_sha256': sha256_hex(item['original']),
            'original_size': len(item['original']),
            'patched_sha256': sha256_hex(item['patched']),
            'patched_size': len(item['patched']),
            'object': name,
        }
        if mutate_entry is not None:
            entry = mutate_entry(language, entry)
        manifest_files.append(entry)
    manifest = {
        'format': 1, 'kind': 'farland-saga-patch', 'version': version,
        'language': language, 'game': 'FARLAND SAGA I Saturn Tribute',
        'created_utc': '2026-11-12T00:00:00Z', 'files': manifest_files,
    }
    path = directory / f'farland-patch-{version}.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        for member, data in objects.items():
            archive.writestr(member, data)
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))
    return path


def write_payload_set(directory: Path, files=None, versions=None,
                      languages=('zh-CN', 'ko-KR', 'en-US'), skip=None,
                      mutate_entry=None, stray=False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else sample_files()
    versions = versions or {language: f'{language}-plan01' for language in languages}
    for language in languages:
        if skip and language in skip:
            continue
        write_language_zip(directory, language, versions[language], files,
                           mutate_entry=mutate_entry)
    if stray:
        (directory / 'stray.txt').write_text('stray', encoding='utf-8')
    return directory


def write_identities(directory: Path, files=None,
                     versions=None,
                     languages=('zh-CN', 'ko-KR', 'en-US')) -> Path:
    """Write identities.json matching the fixture's manifest identities."""
    directory.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else sample_files()
    versions = versions or {language: f'{language}-plan01' for language in languages}
    doc = {'schema': 1, 'kind': 'farland-saga-identity', 'languages': {}}
    for language in languages:
        doc['languages'][language] = {
            'version': versions[language],
            'files': [{
                'path': item['name'],
                'original_sha256': sha256_hex(item['original']),
                'original_size': len(item['original']),
                'patched_sha256': sha256_hex(item['patched']),
                'patched_size': len(item['patched']),
            } for item in files],
        }
    (directory / 'identities.json').write_text(
        json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return directory


def write_declarations(directory: Path, files=None,
                       languages=('zh-CN', 'ko-KR', 'en-US')) -> Path:
    """Write minimal exe/Unity declarations matching the fixture's exe change.

    The fixture patches exe bytes [0x40, 0x60); the declaration expresses the
    same change as one patch operation, so the compiled plan entry carries
    exactly one op and no record stream.
    """
    directory.mkdir(parents=True, exist_ok=True)
    files = files if files is not None else sample_files()
    write_identities(directory, files, languages=languages)
    exe = next(item for item in files if item['name'] == 'FarlandSaga.exe')
    exe_decl = {
        'schema': 1, 'file': 'FarlandSaga.exe',
        'original_sha256': sha256_hex(exe['original']), 'size': len(exe['original']),
        'font': {'start': '0x1000', 'limit': '0x2000', 'cells': 128},
        'languages': {
            language: {
                'patched_sha256': sha256_hex(exe['patched']),
                'size': len(exe['patched']),
                'glyph_slots': [],
                'patches': [{'name': 'test-patch', 'offset': '0x40',
                             'hex': exe['patched'][0x40:0x60].hex()}],
            } for language in languages
        },
    }
    unity_decl = {'schema': 1, 'languages': {
        language: {} for language in languages
    }}
    (directory / 'exe.json').write_text(json.dumps(exe_decl, ensure_ascii=False, indent=1), encoding='utf-8')
    (directory / 'unity.json').write_text(json.dumps(unity_decl, ensure_ascii=False, indent=1), encoding='utf-8')
    return directory


def compile_to(payload_dir: Path, output: Path, lock: Path, write_lock=False,
               declarations_dir=None):
    return compiler.compile_plan(payload_dir, output, lock, write_lock, None,
                                 declarations_dir)


class CompilePlanTests(unittest.TestCase):
    def test_compile_round_trip_and_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload')
            decls = write_declarations(Path(temp) / 'decls')
            output = Path(temp) / 'plan.bin'
            lock = Path(temp) / 'plan.lock.json'
            decls = write_declarations(Path(temp) / 'decls')
            compile_to(payloads, output, lock, write_lock=True, declarations_dir=decls)
            plan = output.read_bytes()

            # 容器结构：magic + u32 头部长度 + JSON 头 + gzip 数据段。
            self.assertTrue(plan.startswith(compiler.PLAN_MAGIC))
            header_length = struct.unpack_from('<I', plan, len(compiler.PLAN_MAGIC))[0]
            start = len(compiler.PLAN_MAGIC) + 4
            header = json.loads(plan[start:start + header_length])
            self.assertEqual(header['format'], 2)
            self.assertEqual(header['game'], 'saga1')
            self.assertEqual(header['languages'], ['zh-CN', 'ko-KR', 'en-US'])
            self.assertEqual(header['files']['FarlandSaga.bin']['kind'], 'disc')
            # 没有 texture.json 声明时，Texture.tex 仍是记录流形态。
            self.assertEqual(header['files']['Texture.tex']['kind'], 'binary')

            blob = gzip.decompress(plan[start + header_length:])
            self.assertEqual(hashlib.sha256(blob).hexdigest(), header['binary_sha256'])

            # 每个语言的条目引用完整的数据段范围，版本来自 manifest；第一个
            # 语言的记录流回放出的就是补丁后内容。
            files = sample_files()
            offset = 0
            for language in header['languages']:
                section = header['languages_data'][language]
                self.assertEqual(section['version'], f'{language}-plan01')
                self.assertEqual(set(section['files']), {item['name'] for item in files})
                for item in files:
                    entry = section['files'][item['name']]
                    self.assertEqual(entry['patched_size'], len(item['patched']))
                    self.assertEqual(entry['patched_sha256'], sha256_hex(item['patched']))
                    if item['name'] == 'FarlandSaga.exe':
                        # 声明覆盖的文件没有记录流，其变化以一条 patch 操作表达。
                        self.assertEqual(entry['records_length'], 0)
                        self.assertEqual(len(entry['ops']), 1)
                        op = entry['ops'][0]
                        self.assertEqual(op['type'], 'patch')
                        self.assertEqual(op['offset'], 0x40)
                        self.assertEqual(op['data_length'], 0x20)
                        self.assertEqual(blob[op['data_offset']:op['data_offset'] + op['data_length']],
                                         item['patched'][0x40:0x60])
                        offset += op['data_length']
                        continue
                    self.assertEqual(entry['records_offset'], offset)
                    stream = blob[offset:offset + entry['records_length']]
                    self.assertEqual(stream, diff_records(item['original'], item['patched']))
                    offset += entry['records_length']
            self.assertEqual(offset, len(blob))

            # lock 校验幂等；身份清单变化后应当拒绝。
            compile_to(payloads, output, lock, declarations_dir=decls)
            write_identities(decls, versions={'zh-CN': 'zh-CN-plan09',
                                              'ko-KR': 'ko-KR-plan01',
                                              'en-US': 'en-US-plan01'})
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, output, lock, declarations_dir=decls)
            self.assertIn('plan does not match', str(caught.exception))

    def test_write_plan_lock_accepts_new_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload')
            decls = write_declarations(Path(temp) / 'decls')
            output = Path(temp) / 'plan.bin'
            lock = Path(temp) / 'plan.lock.json'
            compile_to(payloads, output, lock, write_lock=True, declarations_dir=decls)
            (payloads / 'farland-patch-zh-CN-plan01.zip').unlink()
            write_language_zip(payloads, 'zh-CN', 'zh-CN-plan02', sample_files())
            write_identities(decls, versions={'zh-CN': 'zh-CN-plan02',
                                              'ko-KR': 'ko-KR-plan01',
                                              'en-US': 'en-US-plan01'})
            compile_to(payloads, output, lock, write_lock=True, declarations_dir=decls)
            locked = json.loads(lock.read_text(encoding='utf-8'))
            self.assertEqual(locked['versions']['zh-CN'], 'zh-CN-plan02')

    def test_missing_language_rejected(self):
        """A language ZIP missing while its identity section exists fails hard:
        the disc's pruned record stream has no other source."""
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload', skip=('ko-KR',))
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(Path(temp) / 'decls'))
            self.assertIn('盘片条目需要载荷 ZIP', str(caught.exception))

    def test_missing_identity_language_rejected(self):
        """identities.json must cover every language before any compile."""
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload')
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(
                               Path(temp) / 'decls', languages=('zh-CN', 'en-US')))
            self.assertIn('languages 必须恰好覆盖', str(caught.exception))

    def test_compile_without_zips_fails_on_records(self):
        """Without the legacy ZIPs the compile fails instead of dropping the
        disc's pruned record stream — the plan stays provably complete."""
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload')
            decls = write_declarations(Path(temp) / 'decls')
            output = Path(temp) / 'plan.bin'
            lock = Path(temp) / 'plan.lock.json'
            compile_to(payloads, output, lock, write_lock=True, declarations_dir=decls)
            for zip_path in sorted(payloads.glob('*.zip')):
                zip_path.unlink()
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, output, lock, declarations_dir=decls)
            self.assertIn('盘片条目需要载荷 ZIP', str(caught.exception))

    def test_unknown_language_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(
                Path(temp) / 'payload', languages=('zh-CN', 'ko-KR', 'en-US', 'fr-FR'))
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(Path(temp) / 'decls'))
            self.assertIn('未知的语言载荷', str(caught.exception))

    def test_original_identity_mismatch_rejected(self):
        def mutate_ko_texture(language, entry):
            if language == 'ko-KR' and entry['path'] == 'Texture.tex':
                entry['original_sha256'] = sha256_hex(pattern(77, 32))
            return entry

        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload',
                                         mutate_entry=mutate_ko_texture)
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(Path(temp) / 'decls'))
            self.assertIn('载荷清单与身份清单', str(caught.exception))

    def test_zip_identity_conflict_rejected(self):
        """The ZIP manifest may not drift from the committed identity manifest."""
        def mutate_ko_texture(language, entry):
            if language == 'ko-KR' and entry['path'] == 'Texture.tex':
                entry['patched_sha256'] = sha256_hex(pattern(78, 32))
            return entry

        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload',
                                         mutate_entry=mutate_ko_texture)
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(Path(temp) / 'decls'))
            self.assertIn('载荷清单与身份清单', str(caught.exception))

    def test_stray_file_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload', stray=True)
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(Path(temp) / 'decls'))
            self.assertIn('未知文件', str(caught.exception))

    def test_truncated_record_stream_rejected(self):
        """记录流截断在编译期拒绝，而不是在玩家机器上。"""
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload')
            zip_path = payloads / 'farland-patch-zh-CN-plan01.zip'
            files = sample_files()
            disc = files[0]
            object_name, gz = build_object(disc['original'], disc['patched'])
            with zipfile.ZipFile(zip_path) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                members = {member.filename: archive.read(member.filename)
                           for member in archive.infolist()}
            # 把光盘差异对象的数据段截掉尾部三个字节，再以同名写回；
            # manifest 的对象名与身份字段都不变，只有数据段被截断。
            plain = gzip.decompress(gz)
            truncated = gzip.compress(plain[:len(plain) - 3], mtime=0)
            members[object_name] = truncated
            rewritten = zip_path.with_suffix('.rewritten.zip')
            with zipfile.ZipFile(rewritten, 'w') as target:
                for member, data in members.items():
                    if member == 'manifest.json':
                        data = json.dumps(manifest).encode('utf-8')
                    target.writestr(member, data)
            zip_path.unlink()
            rewritten.rename(zip_path)
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(Path(temp) / 'decls'))
            self.assertIn('截断', str(caught.exception))

    def test_missing_lock_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            payloads = write_payload_set(Path(temp) / 'payload')
            with self.assertRaises(SystemExit) as caught:
                compile_to(payloads, Path(temp) / 'plan.bin', Path(temp) / 'lock.json',
                           declarations_dir=write_declarations(Path(temp) / 'decls'))
            self.assertIn('missing plan lock', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
