"""Read original Unifont HEX cells, preserving their 16-row layout."""
import gzip
import hashlib
import json
from pathlib import Path

from fls.core.path_compat import REPO

FONT_ROOT = REPO/'fonts/unifont-18.0.01'
# 只保留 gzip 形式。未压缩的 .hex 与它内容逐字节一致（sha256 均可在 source.json
# 的 hex_sha256 对上），让仓库白背 3.7 MB；官方 tar.gz 同样只作溯源，不读。
FONT_HEX_GZ = FONT_ROOT/'unifont-18.0.01.hex.gz'


class Unifont:
    def __init__(self):
        self.glyphs = {}
        with gzip.open(FONT_HEX_GZ, 'rt', encoding='ascii') as handle:
            text = handle.read()
        # 哈希仍按解压后的原文计算，与 source.json 的 hex_sha256 同值。
        self.source_sha256 = hashlib.sha256(text.encode('ascii')).hexdigest()
        for line in text.splitlines():
            code, encoded = line.split(':')
            raw = bytes.fromhex(encoded)
            assert len(raw) in (16, 32)
            self.glyphs[int(code, 16)] = raw
        self.used = set()

    def glyph(self, character):
        code = ord(character)
        if code not in self.glyphs:
            raise ValueError(f'Unifont lacks U+{code:04X} {character!r}')
        self.used.add(code)
        return self.glyphs[code]

    def full_cell(self, character):
        raw = self.glyph(character)
        if len(raw) == 32:
            return raw
        if character == '—':
            # East Asian dialogue uses an em dash spanning a full cell.
            # Preserve its original row and extend each pixel horizontally.
            rows = [sum(3 << (2*(7-x)) for x in range(8) if value & (128 >> x))
                    for value in raw]
            return b''.join(value.to_bytes(2, 'big') for value in rows)
        # Centre narrow punctuation/Latin in the original full-width cell.
        return b''.join((value << 4).to_bytes(2, 'big') for value in raw)

    def ascii_pair(self, text):
        assert 1 <= len(text) <= 2
        rows = [0]*16
        for column, ch in enumerate(text):
            raw = self.glyph(ch)
            assert len(raw) == 16, f'Expected an 8-pixel ASCII glyph: {ch!r}'
            for y, value in enumerate(raw):
                rows[y] |= value << (8 if column == 0 else 0)
        return b''.join(value.to_bytes(2, 'big') for value in rows)

    def export(self, stage):
        subset = ''.join(f'{code:04X}:{self.glyphs[code].hex().upper()}\n'
                         for code in sorted(self.used))
        (stage/'unifont_subset.hex').write_text(subset, encoding='ascii')
        metadata = {
            'font': 'GNU Unifont', 'version': '18.0.01',
            'source_sha256': self.source_sha256,
            'used_codepoints': len(self.used), 'missing_codepoints': [],
            'preserved_cell_layout': True, 'derivative_name': 'Farland Translation Bitmap Subset',
            'license': 'SIL Open Font License 1.1',
            'layout_overrides': {'U+2014': 'Expand narrow em dash to a full 16-pixel East Asian cell; preserve row'},
        }
        (stage/'font_source.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        return metadata
