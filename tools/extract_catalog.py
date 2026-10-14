"""Export menu literals and fixed-record item, spell and unit names."""
import json
from pathlib import Path
import sys
from path_compat import REPO, WORK
from translation_data import write_document

sys.stdout.reconfigure(encoding="utf-8")
corpus = REPO / "translation/corpus.json"
if corpus.exists():
    raise RuntimeError(f"Refusing catalog extraction because the authoritative translation corpus exists: {corpus}")
source = WORK / "extracted/steam"
translations = json.loads((REPO / "translation/ui_names.zh-CN.json").read_text("utf-8"))
rows = []

def add(file, offset, raw, capacity, group, record):
    text = raw.split(b"\0")[0].decode("cp932")
    if not text.strip():
        return
    translated = translations.get(text, "")
    rows.append({"id": f"{file}:0x{offset:X}", "file": file, "offset": f"0x{offset:X}",
                 "group": group, "record": record, "capacity_bytes": capacity,
                 "source": text, "translation": translated,
                 "status": "draft" if translated else "untranslated",
                 "notes": "人名、地名、专有名词暂定；尚未在游戏内校对。" if translated else ""})

data = (source / "1ST.BIN").read_bytes()
pos = 0x1232C
while pos < 0x12488:
    end = data.index(0, pos)
    following = end + 1
    while following < 0x12488 and not data[following]:
        following += 1
    add("1ST.BIN", pos, data[pos:end], following-pos, "menu", "")
    pos = following

for file, stride, capacity, count, group in [
    ("SYSTEM/ITEM.DAT", 36, 18, 200, "item"),
    ("SYSTEM/MAGIC.DAT", 32, 22, 90, "magic"),
    # The named unit prefix is regular; the following data is left for analysis.
    ("SYSTEM/UNIT1.DAT", 72, 18, 35, "unit"),
]:
    data = (source / file).read_bytes()
    for record in range(count):
        pos = record * stride
        add(file, pos, data[pos:pos+capacity], capacity, group, record)

output = REPO / "translation/catalog.json"
if output.exists():
    raise FileExistsError(f"Refusing to overwrite existing translation document: {output}")
write_document(output, {"schema_version": 1, "source_language": "ja",
                        "target_language": "zh-CN", "rows": rows})
summary = {"rows": len(rows), "drafts": sum(bool(r["translation"]) for r in rows),
           "groups": {group: sum(r["group"] == group for r in rows) for group in ["menu", "item", "magic", "unit"]}}
(REPO / "translation/catalog_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False))
