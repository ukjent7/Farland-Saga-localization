"""Shared token and glyph-slot helpers for native Japanese script resources."""


def tokens(text, language):
    """Return the native 16x16 glyph tokens used by one localized string."""
    if language in {'en', 'en-US'}:
        if not all(32 <= ord(ch) <= 126 for ch in text):
            raise ValueError(f'English needs printable ASCII: {text!r}')
        return [text[i:i+2] for i in range(0, len(text), 2)]
    if any(ord(ch) < 32 for ch in text):
        raise ValueError('Control characters are not supported')
    return list(text)


def protected_pairs():
    """Return unused, valid CP932 pairs suitable for stable translated glyphs."""
    from fls.core.env import SOURCE as source, font_index
    from fls.text.extract_text import event_header

    valid = []
    for hi in list(range(0x81, 0xA0)) + list(range(0xE0, 0xEB)):
        for lo in range(0x40, 0xFD):
            if lo == 0x7F:
                continue
            pair = bytes((hi, lo))
            if font_index(pair) >= 7806:
                continue
            try:
                pair.decode('cp932')
            except UnicodeDecodeError:
                continue
            valid.append(pair)

    used = set()
    for path in source.rglob('*.EVT'):
        data = path.read_bytes()
        header = event_header(data)
        used.update(data[p:p+2] for p in range(header['table_offset'] + 2,
                                                 header['script_start'], 2))
    for name in ['1ST.BIN', 'SYSTEM/ITEM.DAT', 'SYSTEM/MAGIC.DAT',
                 'SYSTEM/UNIT1.DAT', 'SYSTEM/UNIT2.DAT', 'SYSTEM/MENUINFO.DAT']:
        data = (source / name).read_bytes()
        used.update(data[p:p+2] for p in range(len(data)-1))
    return [pair for pair in reversed(valid)
            if pair not in used and font_index(pair) >= 4000]
