"""Export menu literals and fixed-record item, spell and unit names.

This is the first-pass catalogue extractor for a blank workspace. It refuses to
run once translation/corpus.json exists, because the corpus is authoritative and
must never be regenerated from the original text.
"""
import argparse
import json

from fls.core.path_compat import REPO, WORK
from fls.text.translation_data import write_document

MENU_START = 0x1232C
MENU_END = 0x12488
FIXED_RECORDS = (
    ("SYSTEM/ITEM.DAT", 36, 18, 200, "item"),
    ("SYSTEM/MAGIC.DAT", 32, 22, 90, "magic"),
    # The named unit prefix is regular; the following data is left for analysis.
    ("SYSTEM/UNIT1.DAT", 72, 18, 35, "unit"),
)


def add(rows, translations, file, offset, raw, capacity, group, record):
    text = raw.split(b"\0")[0].decode("cp932")
    if not text.strip():
        return
    translated = translations.get(text, "")
    rows.append({"id": f"{file}:0x{offset:X}", "file": file, "offset": f"0x{offset:X}",
                 "group": group, "record": record, "capacity_bytes": capacity,
                 "source": text, "translation": translated,
                 "status": "draft" if translated else "untranslated",
                 "notes": "人名、地名、专有名词暂定；尚未在游戏内校对。" if translated else ""})


def export():
    corpus = REPO / "translation/corpus.json"
    if corpus.exists():
        raise RuntimeError(
            f"Refusing catalog extraction because the authoritative translation corpus exists: {corpus}")
    source = WORK / "extracted/steam"
    translations = json.loads((REPO / "translation/ui_names.zh-CN.json").read_text("utf-8"))
    rows = []

    data = (source / "1ST.BIN").read_bytes()
    position = MENU_START
    while position < MENU_END:
        end = data.index(0, position)
        following = end + 1
        while following < MENU_END and not data[following]:
            following += 1
        add(rows, translations, "1ST.BIN", position, data[position:end], following - position, "menu", "")
        position = following

    for file, stride, capacity, count, group in FIXED_RECORDS:
        data = (source / file).read_bytes()
        for record in range(count):
            position = record * stride
            add(rows, translations, file, position, data[position:position + capacity],
                capacity, group, record)

    output = REPO / "translation/catalog.json"
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing translation document: {output}")
    write_document(output, {"schema_version": 1, "source_language": "ja",
                            "target_language": "zh-CN", "rows": rows})
    summary = {"rows": len(rows), "drafts": sum(bool(row["translation"]) for row in rows),
               "groups": {group: sum(row["group"] == group for row in rows)
                          for group in ["menu", "item", "magic", "unit"]}}
    (REPO / "translation/catalog_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    export()


if __name__ == "__main__":
    main()
