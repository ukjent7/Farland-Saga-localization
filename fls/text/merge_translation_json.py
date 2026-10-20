"""Merge an ID-keyed draft into the shared multilingual corpus."""
import argparse
import json

from fls.text.merge_full_translations import merge_draft
from fls.text.translation_data import LANGUAGES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=LANGUAGES, default="zh-CN")
    parser.add_argument("--table", choices=["events", "catalog"])
    parser.add_argument("--draft", required=True, help="AI JSON output under work with translations by ID")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing translations explicitly")
    args = parser.parse_args()
    kinds = {"events": {"event"}, "catalog": {"catalog"}}
    result = merge_draft(args.draft, args.language,
                         kinds[args.table] if args.table else None, args.overwrite)
    if args.table:
        result["table"] = args.table
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
