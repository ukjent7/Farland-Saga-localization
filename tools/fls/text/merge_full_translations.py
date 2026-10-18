"""Validate the shared multilingual corpus and merge ID-keyed drafts safely."""
import argparse
import json
import re
from pathlib import Path

from fls.builders.common import catalog_capacity, catalog_text
import language_encoding
from path_compat import inside_workspace
from translation_data import CORPUS, LANGUAGES, read_corpus, write_corpus
LANGUAGE_KEYS = {"zh-CN": ("translation", "zh-CN", "zh"),
                 "ko-KR": ("translation", "ko-KR", "ko"),
                 "en-US": ("translation", "en-US", "en")}
ALL_TRANSLATION_KEYS = {"translation", "zh-CN", "zh", "ko-KR", "ko", "en-US", "en"}
CORE_KINDS = {"event", "catalog", "description", "host", "unity", "condition"}


def translation_text(row, language):
    return row["translations"][language]


def _tokens(text, language):
    return language_encoding.tokens(text, {"zh-CN": "zh", "ko-KR": "ko", "en-US": "en"}[language])


def _native_catalog_text(row, language, text):
    code = {"zh-CN": "zh", "ko-KR": "ko", "en-US": "en"}[language]
    return catalog_text(row, text, code)


def validate_text(row, language, text):
    assert isinstance(text, str) and text.strip(), (row["id"], language, "empty")
    assert not any(ord(ch) < 32 for ch in text), (row["id"], language, "control")
    if language == "en-US":
        assert all(ord(ch) < 127 for ch in text), (row["id"], "non-ASCII")
    elif language == "ko-KR":
        assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text), (row["id"], "Japanese/CJK in Korean")

    kind = row.get("kind")
    if kind == "event":
        capacity = row.get("original_units")
        used = len(_tokens(text, language))
        assert capacity is not None and used <= int(capacity), (row["id"], language, "glyphs", used, capacity)
    elif kind in {"catalog", "description"}:
        encoded_text = _native_catalog_text(row, language, text)
        used = 2 * len(_tokens(encoded_text, language))
        capacity = catalog_capacity(row)
        assert used < capacity, (row["id"], language, "bytes", used, capacity)
    elif kind == "host":
        encoded_bytes = len(text.encode("utf-8"))
        if row.get("max_utf8_bytes") is not None:
            capacity = int(row["max_utf8_bytes"])
            assert encoded_bytes <= capacity, (row["id"], language, "utf8 bytes", encoded_bytes, capacity)
        else:
            capacity = int(row.get("capacity_bytes", 0))
            assert encoded_bytes < capacity, (row["id"], language, "utf8 bytes", encoded_bytes, capacity)
    elif kind == "unity":
        capacity = int(row.get("max_utf8_bytes", row.get("capacity_bytes", 0)))
        assert len(text.encode("utf-8")) <= capacity, (row["id"], language, "utf8 bytes", len(text.encode("utf-8")), capacity)
        pattern = r'<sprite="TMP_Buttons" index=\d+>'
        assert re.findall(pattern, row["source"]) == re.findall(pattern, text), (row["id"], "markup")
    elif kind == "condition":
        used = 2 * len(_tokens(text, language))
        assert used <= int(row["capacity_bytes"]), (row["id"], language, "fixed bytes", used, row["capacity_bytes"])
    return True


def validate(row, after=None, language=None):
    """Validate one canonical row; the old pair form remains for archived tools."""
    if language is not None:
        if isinstance(after, str):
            text = after
        elif isinstance(after, dict):
            assert row["id"] == after["id"]
            if isinstance(after.get("translations"), dict):
                text = translation_text(after, language)
            else:
                text = after["translation"]
        elif isinstance(row.get("translations"), dict):
            text = translation_text(row, language)
        elif isinstance(row.get("translation"), str):
            text = row["translation"]
        else:
            raise ValueError(f"No {language} translation for {row.get('id')}")
        return validate_text(row, language, text)

    # Compatibility for the old candidate-selection helper. It is read-only and
    # still supplies the frozen input row plus a ko/en pair.
    assert after is not None and row["id"] == after["id"]
    for code, language_name in (("ko", "ko-KR"), ("en", "en-US")):
        text = after[code]
        max_chars = row.get(code + "_max_chars")
        assert isinstance(text, str) and text.strip(), (row["id"], code, "empty")
        assert not any(ord(ch) < 32 for ch in text), (row["id"], code, "control")
        if code == "en":
            assert all(ord(ch) < 127 for ch in text), (row["id"], "non-ASCII")
        else:
            assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text), (row["id"], "Japanese/CJK in Korean")
        if "max_utf8_bytes" in row:
            assert len(text.encode("utf-8")) <= row["max_utf8_bytes"], (row["id"], code, "bytes")
        else:
            assert len(text) <= max_chars, (row["id"], code, len(text), max_chars)
        if row["kind"] == "unity":
            pattern = r'<sprite="TMP_Buttons" index=\d+>'
            assert re.findall(pattern, row["source"]) == re.findall(pattern, text), (row["id"], "markup")
    return True


def validate_corpus(document=None):
    document = document or read_corpus()
    rows = document["rows"]
    for row in rows:
        if row.get("kind") not in CORE_KINDS:
            continue
        for language in LANGUAGES:
            validate_text(row, language, translation_text(row, language))
    return {"rows": len(rows), "core_rows": sum(row.get("kind") in CORE_KINDS for row in rows),
            "languages": list(LANGUAGES), "status": "valid"}


def _text_for(entry, language):
    for key in LANGUAGE_KEYS[language]:
        if isinstance(entry.get(key), str):
            return entry[key]
    raise ValueError(f"Draft row has no {language} text: {entry.get('id')}")


def merge_draft(path, language="zh-CN", kinds=None, overwrite=False, corpus_path=CORPUS):
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    draft_path = Path(path).resolve()
    if not inside_workspace(draft_path):
        raise ValueError("Draft must be stored under the repository or workspace")
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    entries = draft.get("translations")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Draft must contain a nonempty translations array")

    corpus = read_corpus(corpus_path)
    index = {row["id"]: row for row in corpus["rows"]}
    allowed = set(kinds) if kinds else None
    updates = {}
    review_keys = {"status", "notes", "note", "review_note", "reason", "reviewer", "review_status"}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("Each translation needs a string ID")
        identifier = entry["id"]
        if identifier not in index:
            raise ValueError(f"Unknown ID in draft: {identifier}")
        if identifier in updates:
            raise ValueError(f"Duplicate ID in draft: {identifier}")
        row = index[identifier]
        if allowed is not None and row.get("kind") not in allowed:
            raise ValueError(f"ID is outside requested table: {identifier}")
        for other_language in LANGUAGES:
            if other_language == language:
                continue
            for key in LANGUAGE_KEYS[other_language][1:]:
                if key in entry and entry[key] != row["translations"][other_language]:
                    raise ValueError(f"Draft changes another language: {identifier}:{other_language}")
        text = _text_for(entry, language)
        validate_text(row, language, text)
        current = row["translations"][language]
        if current and current != text and not overwrite:
            raise ValueError(f"Existing {language} translation would be replaced; use --overwrite: {identifier}")
        metadata = {}
        for key, value in entry.items():
            if key == "id" or key in ALL_TRANSLATION_KEYS:
                continue
            if key in review_keys or key.startswith("review_"):
                metadata[key] = value
            elif key in row and row[key] == value:
                continue
            else:
                raise ValueError(f"Draft changes source metadata: {identifier}:{key}")
        updates[identifier] = (text, metadata)

    for identifier, (text, metadata) in updates.items():
        row = index[identifier]
        row["translations"][language] = text
        if metadata:
            row.setdefault("reviews", {}).setdefault(language, {}).update(metadata)
    write_corpus(corpus_path, corpus)
    return {"language": language, "merged": len(updates), "overwritten": bool(overwrite),
            "translator": draft.get("translator", "AI agent")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=LANGUAGES, default="zh-CN")
    parser.add_argument("--table", choices=sorted(CORE_KINDS), action="append",
                        help="Restrict validation/merge to one or more corpus kinds")
    parser.add_argument("--draft", help="AI JSON output under work with translations keyed by stable ID")
    parser.add_argument("--overwrite", action="store_true", help="Allow intentional replacement of existing translations")
    args = parser.parse_args()
    if args.overwrite and not args.draft:
        parser.error("--overwrite requires --draft")
    if args.draft:
        result = merge_draft(args.draft, args.language, args.table, args.overwrite)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.table:
        selected = [row for row in read_corpus()["rows"] if row.get("kind") in set(args.table)]
        document = {"rows": selected}
    else:
        document = read_corpus()
    result = validate_corpus(document)
    if args.table:
        result["tables"] = args.table
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
