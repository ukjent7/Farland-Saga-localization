"""Read original Unifont HEX cells, preserving their 16-row layout."""
import gzip
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FONT_ROOT = REPO / 'fonts/unifont-18.0.01'
FONT_HEX_GZ = FONT_ROOT / 'unifont-18.0.01.hex.gz'
FONT_HEX = FONT_ROOT / 'unifont-18.0.01.hex'


class Unifont:
    def __init__(self):
        self.glyphs = {}
        if FONT_HEX_GZ.exists():
            with gzip.open(FONT_HEX_GZ, 'rt', encoding='ascii') as handle:
                text = handle.read()
        elif FONT_HEX.exists():
            text = FONT_HEX.read_text(encoding='ascii')
        else:
            raise FileNotFoundError(f"Unifont hex file not found in {FONT_ROOT}")

        self.source_sha256 = hashlib.sha256(text.encode('ascii')).hexdigest()
        for line in text.splitlines():
            if not line.strip():
                continue
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
            rows = [sum(3 << (2 * (7 - x)) for x in range(8) if value & (128 >> x))
                    for value in raw]
            return b''.join(value.to_bytes(2, 'big') for value in rows)
        return b''.join((value << 4).to_bytes(2, 'big') for value in raw)

    def export(self, target_directory):
        target = Path(target_directory)
        target.mkdir(parents=True, exist_ok=True)
        subset_lines = []
        for code in sorted(self.used):
            subset_lines.append(f'{code:04X}:{self.glyphs[code].hex().upper()}\n')
        subset_text = ''.join(subset_lines)
        (target / 'unifont_subset.hex').write_text(subset_text, encoding='ascii')
        record = {
            'font': 'GNU Unifont',
            'version': '18.0.01',
            'source_sha256': self.source_sha256,
            'subset_characters': len(self.used),
            'subset_sha256': hashlib.sha256(subset_text.encode('ascii')).hexdigest(),
        }
        (target / 'font_source.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        return record
