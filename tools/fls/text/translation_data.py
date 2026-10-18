"""Read and write the shared, ID-stable multilingual translation corpus."""
import copy
import json
import os
from pathlib import Path
import tempfile

from path_compat import REPO

CORPUS = REPO / "translation/corpus.json"
LANGUAGES = ("zh-CN", "ko-KR", "en-US")
LEGACY_TABLES = {"events.json": {"event"}, "catalog.json": {"catalog"}}


def _validate_corpus(document, path=CORPUS):
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"Unsupported translation corpus: {path}")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Translation corpus has no rows array: {path}")
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError(f"Every corpus row must have a string ID: {path}")
        translations = row.get("translations")
        if not isinstance(translations, dict):
            raise ValueError(f"Corpus row has no translations object: {row['id']}")
        for language in LANGUAGES:
            if not isinstance(translations.get(language), str):
                raise ValueError(f"Corpus row needs a {language} string: {row['id']}")
        ids.append(row["id"])
    if len(set(ids)) != len(ids):
        raise ValueError(f"Duplicate translation IDs: {path}")
    return document


def read_corpus(path=CORPUS):
    path = Path(path)
    return _validate_corpus(json.loads(path.read_text(encoding="utf-8")), path)


def language_row(row, language):
    """Return a compatibility row with the selected translation in `translation`."""
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    result = {key: copy.deepcopy(value) for key, value in row.items()
              if key not in {"translations", "reviews"}}
    result["translation"] = row["translations"][language]
    review = row.get("reviews", {}).get(language, {})
    if language == "zh-CN":
        for key in ("status", "notes"):
            if key in review:
                result[key] = copy.deepcopy(review[key])
    else:
        for key in ("note", "review_note"):
            if key in review:
                result[key] = copy.deepcopy(review[key])
    return result


def read_corpus_rows(language=None, kinds=None, path=CORPUS):
    rows = read_corpus(path)["rows"]
    selected = set(kinds) if kinds is not None else None
    result = [row for row in rows if selected is None or row.get("kind") in selected]
    if language is None:
        return copy.deepcopy(result)
    return [language_row(row, language) for row in result]


def _legacy_kind(path):
    path = Path(path).resolve()
    for name, kinds in LEGACY_TABLES.items():
        if path == (REPO / "translation" / name).resolve():
            return kinds
    return None


def _atomic_json_write(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".tmp-", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_document(path):
    """Read the shared corpus or a virtual Chinese event/catalog document."""
    path = Path(path)
    kinds = _legacy_kind(path)
    if kinds is not None:
        rows = read_corpus_rows("zh-CN", kinds)
        return {"schema_version": 1, "source_language": "ja", "target_language": "zh-CN",
                "rows": rows}
    if path.resolve() == CORPUS.resolve():
        return read_corpus(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"Unsupported translation document: {path}")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Translation document has no rows array: {path}")
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or any(not isinstance(identifier, str) for identifier in ids):
        raise ValueError(f"Every translation row must have a string ID: {path}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"Duplicate translation IDs: {path}")
    return document


def read_rows(path):
    return read_document(path)["rows"]


def write_corpus(path, document):
    path = Path(path)
    _validate_corpus(document, path)
    _atomic_json_write(path, document)


def _write_legacy_view(path, document):
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"Unsupported translation document: {path}")
    kinds = _legacy_kind(path)
    if kinds is None:
        raise ValueError(f"Not a virtual corpus table: {path}")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Translation document has no rows array: {path}")
    corpus = read_corpus()
    index = {row["id"]: row for row in corpus["rows"]}
    expected = {row["id"] for row in corpus["rows"] if row.get("kind") in kinds}
    seen = set()
    for incoming in rows:
        if not isinstance(incoming, dict) or not isinstance(incoming.get("id"), str):
            raise ValueError(f"Every translation row must have a string ID: {path}")
        identifier = incoming["id"]
        if identifier not in expected:
            raise ValueError(f"Unknown ID in virtual corpus table: {identifier}")
        if identifier in seen:
            raise ValueError(f"Duplicate ID in virtual corpus table: {identifier}")
        seen.add(identifier)
        target = index[identifier]
        if not isinstance(incoming.get("translation"), str):
            raise ValueError(f"Translation must be a string: {identifier}")
        # Legacy callers may update prose and review fields, but cannot change
        # IDs, original text, offsets, byte capacities, or other source metadata.
        for key, value in incoming.items():
            if key in {"translation", "status", "notes", "note", "review_note"}:
                continue
            if key not in target or target[key] != value:
                raise ValueError(f"Source metadata cannot be changed through a table view: {identifier}:{key}")
        target["translations"]["zh-CN"] = incoming["translation"]
        review = target.setdefault("reviews", {}).setdefault("zh-CN", {})
        for key in ("status", "notes"):
            if key in incoming:
                review[key] = incoming[key]
    if seen != expected:
        missing = sorted(expected - seen)
        raise ValueError(f"Virtual corpus table is incomplete: {path} ({len(missing)} rows missing)")
    write_corpus(CORPUS, corpus)


def write_document(path, document):
    path = Path(path)
    if _legacy_kind(path) is not None:
        _write_legacy_view(path, document)
        return
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"Unsupported translation document: {path}")
    read_ids = [row.get("id") for row in document.get("rows", []) if isinstance(row, dict)]
    if len(read_ids) != len(document.get("rows", [])) or len(set(read_ids)) != len(read_ids):
        raise ValueError(f"Translation rows need unique IDs: {path}")
    _atomic_json_write(path, document)
