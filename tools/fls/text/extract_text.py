"""Export indexed event strings, preserving original offsets and raw codes."""
import argparse
import json
from pathlib import Path
import struct
import sys
from path_compat import REPO, inside_workspace
from translation_data import write_document

def event_header(data):
    count = int.from_bytes(data[:2], "big")
    if not 0 < count < 100:
        raise ValueError("Unexpected event count")
    table_offset = 2 + count * 6
    glyph_count = int.from_bytes(data[table_offset:table_offset + 2], "big")
    script_start = table_offset + 2 + glyph_count * 2
    glyphs = [data[table_offset + 2 + 2*i:table_offset + 4 + 2*i].decode("cp932")
              for i in range(glyph_count)]
    events = [{"id": int.from_bytes(data[2 + i*6:4 + i*6], "big"),
               "script_offset": int.from_bytes(data[4 + i*6:8 + i*6], "big")} for i in range(count)]
    return {"events": events, "glyphs": glyphs, "script_start": script_start, "table_offset": table_offset}

def event_strings(data, header):
    glyphs = header["glyphs"]
    pos = header["script_start"]
    strings = []
    while pos < len(data):
        op = data[pos]
        if op == 0x25:  # 32-bit integer literal
            pos += 5
        elif op == 0x40:  # function number
            pos += 2
        elif op == 0x27:  # byte string (movement etc.)
            end = data.find(b"\0", pos + 1)
            if end < 0:
                raise ValueError(f"Unterminated byte literal at {pos:X}")
            pos = end + 1
        elif op == 0x22:
            start = pos
            pos += 1
            codes = []
            while pos + 2 <= len(data):
                code = int.from_bytes(data[pos:pos + 2], "big")
                pos += 2
                if code == 0:
                    break
                codes.append(code)
            else:
                raise ValueError(f"Unterminated text at {start:X}")
            text = "".join(glyphs[c-1] if 1 <= c <= len(glyphs) else f"<0x{c:04X}>" for c in codes)
            strings.append({"offset": start, "end": pos, "codes": codes, "text": text})
        else:
            pos += 1
    return strings

def export(source, output):
    corpus = REPO / "translation/corpus.json"
    if corpus.exists():
        raise RuntimeError(f"Refusing extraction because the authoritative translation corpus exists: {corpus}")
    source = Path(source).resolve()
    output = Path(output).resolve()
    if not inside_workspace(output):
        raise ValueError("Output must stay under the repository or workspace")
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    files = []
    for path in sorted(source.rglob("*.EVT")):
        data = path.read_bytes()
        header = event_header(data)
        strings = event_strings(data, header)
        relative = path.relative_to(source).as_posix()
        files.append({"path": relative, **header, "string_count": len(strings)})
        for i, entry in enumerate(strings, 1):
            rows.append({"id": f"{relative}:{i:04d}", "file": relative, "offset": f"0x{entry['offset']:X}",
                         "original_units": len(entry["codes"]), "source": entry["text"],
                         "translation": "", "status": "untranslated", "notes": "",
                         "raw_codes": " ".join(f"{c:04X}" for c in entry["codes"])})
    rows_path = output / "events.json"
    if rows_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing translation document: {rows_path}")
    write_document(rows_path, {"schema_version": 1, "source_language": "ja",
                               "target_language": "zh-CN", "rows": rows})
    (output / "event_tables.json").write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"event_files": len(files), "strings": len(rows), "characters": sum(int(r["original_units"]) for r in rows),
               "unknown_code_strings": sum("<0x" in r["source"] for r in rows), "source": str(source)}
    (output / "extraction_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return rows

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args()
    export(args.source, args.output)
