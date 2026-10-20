# Patch Configuration Format

Configurations are read by `localize.py patch`: the language, field positions, and glyph data all live in JSON, and the executing code keeps only the generic rules per resource type. Add configuration for a new problem first; existing artifacts use `--base`, and the output goes to a new `--output`.

The top-level schema is 1, `id` is the configuration name, `languages` lists the supported languages, and `report_key` names the field in the candidate report. Ordinary text is configured in the `fields` array: `file`, `offset`, `capacity_bytes`, `mode`, `source`, and `text`.

| Field | Meaning |
| --- | --- |
| `file` / `offset` | Native resource name and fixed offset; hex offsets may be strings |
| `capacity_bytes` | The full field capacity, including the trailing zero when `mode=nul` |
| `mode=fixed` | A fixed byte count with no terminator; shortfalls use the original full-width blank cell |
| `mode=nul` | Zero-fill after the text; space must be left for the terminator |
| `source` | The original CP932 text, which must match the corresponding `extracted/steam` field exactly |
| `before` | The expected translation per language from the base candidate; when omitted, the original is expected |
| `text` | The target text per language; re-applying the same configuration is allowed |
| `new_text=true` | Increments the recognized entry count only when the original is replaced for the first time |

The built-in condition-label configuration references `translation/corpus.json` via `translation_corpus`, checking field IDs against the original locations one by one and reading the text from that row's `translations`; the legacy `translation_file` is kept only for explicit historical configurations. `width_checks` keeps the reference option and checks cell width; `mirror_checks` scans the original resources and requires the copy location to match the manifest. `glyphs` specifies one 32-byte glyph replacement through `character`, `source_hex`, and `bitmap_hex`.

The target still uses the original 16×16 cells. English fits at most two ASCII characters per cell; Korean and Chinese fit one character per cell. Exceeding capacity, a mismatched base field, an unsupported language, or an unsupported glyph stops the build immediately; nothing is auto-truncated and no program pointers are relocated.

A configuration never installs anything on its own. The unified workflow and commands are in the [tools notes](../docs/tools.md).

`original_ranges` restores original values that an older patch overwrote in fixed records. Each group declares `file/source_sha256/offset/stride/count/field_offset/size/before_hex`; a record is accepted only when it holds the declared old bytes or the original bytes, after checking the frozen asset identity and boundaries. Fields may be declared in only some languages' `text`; the other languages are skipped.

`item-prices` restores the two-byte original prices for all 200 records in `ITEM.DAT` and the `MAIN.DAT` copies, and verifies prices against the remaining attributes. A name's real capacity is 18 bytes including the terminator; older drafts' 20-byte capacity is kept only as a historical input, and the shared builder uses the real bound. Eight over-long English names are abbreviated to 16 ASCII characters or fewer via `before`/`text` in the configuration, and the full rebuild uses the same configuration, so neither the old drafts nor the existing review records override it.

`container_atlases` handles the SSF-encrypted `Texture/Parts` PNG atlases. `source` points at the frozen original inside the private workspace (for example `assets/sources/Texture.tex`) and `original_sha256` pins its identity; `image_size`, `part_count`, and each label's `part`/`box` pin the atlas layout. Text goes in `text`, and each language may set its own `font_size`. `panel_parts` / `panel_text` handle the same structure for option hint panels. A font size that is too large or a missing glyph stops the build; icons, pixels outside the text areas, Parts/Anime metadata, and the encrypted container header are preserved, and the decrypted read-back after encoding is verified pixel by pixel.

`frontend-menu` covers the eight front-end menu entries and the View button hints in seven language slots, and adds a sixth install file `Texture.tex` to the candidate. The installer folds it into the original backup, pre-flight, rollback, and restore automatically; generating a candidate alone never modifies the game directory. Later generic patches and full translation rebuilds preserve this atlas.

The current accumulated configuration is `frontend-submenus`, which keeps the main menu and covers sub-options, confirm dialogs, operation notes and warnings, loading text, and the no-save prompt, reaching 371 text blocks and 7 View panels in total. The translation group IDs and the original language slots are recorded in `translation/frontend_submenus.json`; the built-in configuration's `translation_source` and the labels' `translation_id` point at that unified topical table, and panels use `panel_translation_ids` to point at the same table. When the current translation and the configuration layout match once whitespace is stripped, the original font size and line breaks are reused; after a text edit the new text and new line breaks from the topical table are used, checked by the existing rendering and capacity rules, and nothing is auto-truncated at build time. Explicit custom JSON configurations keep using their own `text`, for replaying frozen versions or validating layout.

An atlas label may declare `region`, background RGBA `background`, alignment `align` (left/center/right), text RGBA `fill`, outline `stroke`, allowed `max_lines`, and clockwise `rotation` (0/90/180/270) relative to it. The region must lie inside the frozen sprite; only that region is cleared, protecting neighbouring content such as loading animations and badge borders. Line breaks are valid only within the allowed line count; other control characters and blank lines are rejected. `preview_sections` names the IDs and sprite lists for grouped previews.

Atlases are always generated from the frozen original, so a new sub-option configuration must include the existing main menu and panels to avoid regressing them. The current accumulated configuration may be applied repeatedly; the earlier `frontend-menu` covers the main menu only and is retained for replaying the old 04 candidate, not for generating the current sub-menu candidate.

`runtime_word_writes` handles the title text the emulator writes into native memory at runtime. `original_sha256` pins the original executable; each label declares `source`, three-language `text`, `capacity_cells`, and per-character `word_writes`. Each write record fixes the 4-byte immediate `offset`, the memory write address `memory_address`, and the complete 18-byte original instruction `instruction_hex`. Only the text immediate may change; the instruction opcode, memory address, call target, and all other bytes must match. Exceeding the cell capacity or a mismatched base instruction refuses the build.

`save-folder` changes six writes of "フォルダ" to "文件夹／폴더／Folder", reusing the native glyph mapping and filling the original blank cells. All numbering, surrounding whitespace, and the save/load code stay as they are, the candidate remains six files, and the full three-language rebuild replays these text constants per the preserved report.
