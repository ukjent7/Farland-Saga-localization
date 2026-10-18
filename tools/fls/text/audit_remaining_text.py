"""Read-only inventory of literal text and its SH2 pointer references via IDALib."""
import json
import re
import struct
from pathlib import Path
import idapro
import idautils
import idc
from path_compat import WORK

DATA = (WORK / 'extracted/steam/1ST.BIN').read_bytes()
BASE = 0x06004000

def literal_at(off):
    end = DATA.find(b'\0', off)
    if end < 0 or end-off > 350 or end-off < 2:
        return None
    try:
        text = DATA[off:end].decode('cp932')
    except UnicodeDecodeError:
        return None
    if not re.search('[ぁ-ヿ一-龥]', text):
        return None
    if any(not (0x20 <= ord(c) <= 0x7e or 0x3000 <= ord(c) <= 0x9fff
                or 0xff01 <= ord(c) <= 0xff60 or c in '\r\n\t') for c in text):
        return None
    return text, end

def main():
    rc = idapro.open_database(str(WORK/'analysis/Saturn_1ST.BIN.i64'), False)
    if rc:
        raise RuntimeError(rc)
    try:
        references = {}
        for pool in range(0, len(DATA)-3, 4):
            address = struct.unpack_from('>I', DATA, pool)[0]
            off = address-BASE
            if not 0 <= off < len(DATA) or literal_at(off) is None:
                continue
            references.setdefault(off, []).append(hex(pool))
        # Standalone literals also include arrays of pointers populated at runtime.
        candidates = set(references)
        for m in re.finditer(rb'[^\x00]+\x00', DATA):
            if m.end()-m.start() > 360:
                continue
            for trim in range(min(6, m.end()-m.start()-1)):
                off = m.start()+trim
                if literal_at(off) and (off % 4 == 0 or off in references):
                    candidates.add(off)
                    break
        rows = []
        for off in sorted(candidates):
            text, end = literal_at(off)
            refs = [{'from':hex(x.frm), 'function':idc.get_func_name(x.frm),
                     'disassembly':idc.generate_disasm_line(x.frm, 0)}
                    for x in idautils.XrefsTo(BASE+off)]
            rows.append({'file':'1ST.BIN','offset':hex(off),'source':text,
                         'capacity_bytes':end-off+1,'pool_offsets':references.get(off,[]),
                         'ida_xrefs':refs})
        (WORK/'analysis/remaining_sh2_literals.json').write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        for r in rows:
            print(r['offset'], len(r['pool_offsets']), r['source'])
    finally:
        idapro.close_database(False)

if __name__ == '__main__':
    main()
