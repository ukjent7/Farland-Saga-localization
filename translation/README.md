# Three-Language Translation Tables

The current translations are kept in this directory by topic, sharing stable IDs across the Japanese source and the Chinese, Korean, and English translations. Older extracted tables, AI batches, cross-reviews, and frozen copies are folded into historical bundles; restoring an old record never automatically overwrites the current translations.

| Current source | Contents |
| --- | --- |
| `corpus.json` | 2,777 original ordinary-text rows plus 9 status labels, 2,786 stable IDs in total |
| `glossary.json` | 90 terms, categories, and review notes; 17 Korean and English terms are already aligned to the Japanese |
| `titles.json` | Three-language text and layout for 50 image level titles |
| `ui_labels.json` | Terrain, equipment, interface, and attribute image labels |
| `frontend_submenus.json` | Three-language topical text for the front-end menu and hint panels |

An ordinary text row's `translations` holds `zh-CN`, `ko-KR`, and `en-US`; `reviews` holds each language's and the shared review records. The source text, file, offset, capacity, glyph slots, and Unity object location are for building and verification and must not be edited as if they were translations. A missing term translation is left empty; never auto-fill it from an older Chinese snapshot or from the source.

```json
{
  "id": "EVE/EVE01_1.EVT:0001",
  "translations": {
    "zh-CN": "久等了！",
    "ko-KR": "늦었지!",
    "en-US": "Sorry!"
  },
  "reviews": {
    "zh-CN": {"status": "draft", "notes": "review notes"}
  }
}
```

Only the editable fields are shown above; real rows also carry the full source text and locating metadata. Each image topical follows its own encoder's row structure; there is no need to force a change to the stable image layout format.

Both the Chinese virtual `events/catalog` view and the Korean and English build views read from `corpus.json`; no duplicate editable copies are maintained. Of the older alignment inputs, 1,714 Chinese entries differ from the current Chinese; during migration the current Chinese is backfilled per ID, while Korean and English keep the already-reviewed complete translations. The original rows and historical notes live in the maintenance snapshot and the historical translation bundles.

When continuing a translation, put the temporary ID-keyed draft at `translation/draft.json` in the repository and merge it with `bash flow.sh merge --language ko-KR --draft translation/draft.json`; add `--overwrite` when you explicitly need to replace an existing translation. A parameterless `merge` only validates the current tables. The detailed merge, build, and capacity rules are in [tools notes](../docs/tools.md).

The frozen inputs for images, atlases, glyph slots, and Unity live in the private workspace's `assets/sources/`; a candidate's `inputs/` holds that run's complete input snapshot. Historical batches are restored to their original paths by the private workspace's `flow.sh storage` command, and once restored are for reading or explicit merging only — never re-run the original-text extraction to overwrite the translations.
