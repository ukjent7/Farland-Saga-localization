# FS2 install-time recipe

This directory is the human-readable source for the FARLAND SAGA II localisation.
The patcher no longer needs a prebuilt ZIP of patched game resources: it reads
the user's pristine `FarlandSaga2.bin`, applies the recipe, verifies the result,
and writes the changed sectors itself.

Nothing here contains game bytes. The only inputs are text, label boxes, colour
indices, font roles, operation lists and expected SHA-256 hashes.

## Layout

- `manifest.json` — recipe version, languages, disc constants, resource index
  (archive offset, size, original hash) and the recipe file for each resource.
- `resources/<id>.json` — one recipe per generated resource. A tile atlas lists
  erase regions, label boxes and styles; a 1bpp text bank lists slot writes.
- `fonts.json` — public font roles used by every resource:
  `NotoSansSC` (zh-CN), `Galmuri11` (ko-KR), `LiberationSans` (en-US) and
  `Unifont` for the title-menu and place-name bitmap glyphs.
- `render` on a label selects its mask renderer. `pillow` rasterises the
  language font and `unifont` uses Unifont's bitmap glyphs. The value is either
  one name or a per-language map such as `{"en-US": "unifont"}`. The English
  battle menus use proportional Unifont at 8px: Liberation Sans breaks up at
  that size after the 1-bit threshold, while Unifont stays crisp and fits the
  atlas tile budget.
- `fonts/` — the committed OFL font subsets and their licence texts. The
  subsets contain only the characters these recipes draw.
- `expected/<language>.json` — the required patched SHA-256 of every resource.
  A recipe edit that changes pixels must update these hashes; the installer
  refuses to write a resource whose generated hash does not match.

## Compiling the plan

```sh
./uv.sh run --locked python tools/compile_recipe.py
```

The compiler rasterises the text with Pillow, packs 1bpp slots, emits one mask
per label, and writes `patcher/plan/plan.bin` plus a JSON build report.
`go build -tags saga2` embeds the plan. The build is deterministic: the same
recipe and fonts produce the same plan hash.

`patcher/plan/plan.bin` is ignored by git. A clean checkout can compile it from
the committed fonts; without a plan the patcher falls back to the payload ZIP.
Some resources still have no recipe in Phase 1 and always come from that ZIP.

## Applying a recipe

The installer verifies the original disc image, reads each resource from the
backup, and dispatches on its format:

- `tile_atlas_8bpp`: decode the map and tiles into a pixel plane, apply every
  erase region, blit the label masks, then repack the whole atlas. One tile is
  claimed per distinct 8x8 cell image; the repack fails if the atlas needs more
  tiles than it has.
- `mono_bitmap_1bpp`: copy the plan's packed slot bytes into the original
  resource at their declared offsets.

The generated resource must match its expected hash and length. The installer
then replaces the payload's sectors for that resource and computes the final
image hash from the bytes it actually wrote; the install record stores that
hash, so `status`, `verify` and `restore` keep working.

Phase 1 keeps the payload as the source for every resource the recipe does not
cover and for the executable and `Texture.tex`. Recipe sectors take precedence
over the payload at the same LBA.

## Phase 1 scope

Generated resources:

| Group | Resources |
| --- | --- |
| Mission banners | `02B9`–`02C8` |
| Shared UI atlas | `02C9` |
| Job prompt | `0439` |
| Save/load screen | `043F` |
| Title panels | `0444`, `0445` |
| 1bpp text bank | `02CB` |
| Chapter cards | `040D`–`041E` |
| Save-scene titles | `041F` |

The public fonts change the pixels relative to the candidate16 ZIP, so Phase 1
no longer requires byte parity with that payload. `expected/` records the new
reference pixels for all three languages.

Phases 2–3 move descriptions, story pages and `Texture.tex` into recipes.
Phase 4 removes the FS2 payload ZIPs and compiles the plan in CI.

## Changing text

1. Edit the string or box in the matching `resources/<id>.json`.
2. Compile. The compiler fails fast on a missing font, a box outside the atlas,
   a 1bpp slot that cannot fit, or text that does not fit its box.
3. Run the private `fs2/work/tools/recipe_e2e.py` gate. It installs all three
   languages into a throwaway game, decodes all 41 resources from the installed
   image, compares them with `expected/`, proves that only recipe sectors
   changed, and exercises switching, restore and rejected-plan paths.
4. Update `expected/<language>.json` only when the new pixels are the intended
   result, and record the new hashes in the change description.
