# FS2 install-time recipe

This directory is the human-readable source for the FARLAND SAGA II localisation.
The patcher no longer needs a prebuilt ZIP of patched game resources: it reads
the user's pristine `FarlandSaga2.bin`, applies the recipe, verifies the result,
and writes the same sectors the payload used to carry.

Nothing here contains game bytes. The only inputs are text, label boxes, colour
indices, font roles and expected SHA-256 hashes.

## Layout

- `manifest.json` — recipe version, languages, disc constants, resource index
  (archive offset, size, original hash) and the recipe file for each resource.
- `resources/043F.json` — save/load screen labels: strings, boxes, styles,
  font roles, size limits and alignment.
- `expected/<language>.json` — the required patched SHA-256 of every resource.
  A recipe edit that changes pixels must update these hashes; the installer
  refuses to write a resource whose generated hash does not match.
- `fonts.json` — public font roles. It is empty in Phase 0 because that phase
  reproduces the existing private pipeline byte for byte.
- `fonts.local.json` — optional, ignored by git. Absolute paths for the fonts
  used by the private Phase 0 build. Phase 1 replaces these with OFL fonts
  committed under `recipe/fonts/`.

## Compiling the plan

```sh
./uv.sh run --locked python tools/compile_recipe.py
```

The compiler rasterises the text with Pillow, emits one mask per label, and
writes `patcher/plan/plan.bin` plus a JSON build report. `go build -tags saga2`
embeds the plan. The build is deterministic: the same recipe and fonts produce
the same plan hash.

A clean public checkout has no private fonts and no `plan.bin`; the patcher
still compiles and falls back to the payload ZIP. A private build with the
fonts produces the recipe-enabled patcher.

## Applying a recipe

The installer verifies the original disc image, reads resource `043F` from the
backup, decodes its tile atlas, erases the label rows with their row background,
blits the masks, and runs the same first-fit tile allocation as the private
Python pipeline. It then checks the expected hash and turns the bytes into
2048-byte sectors at `archive_lba + archive_offset / 2048`.

Phase 0 keeps the payload as the source for every resource the recipe does not
cover and for the executable and `Texture.tex`. It also enforces byte parity:
each recipe sector must equal the payload sector at the same LBA, so the merged
image keeps the payload's declared hash. This is the gate that proves the
install-time generator matches the released candidate.

## Phase 0 scope

Only `043F` is generated. The three languages are byte-identical to candidate16:

| Language | 043F SHA-256 |
| --- | --- |
| `zh-CN` | `66b2f35db889dfe9af7f46da8bd58f72c11f80c39f1da6d93237371cfd6413a7` |
| `ko-KR` | `1655744b1450010a658aaf65635e2f741d84b82c407791e9abcb74a89e55dd66` |
| `en-US` | `74aaebd37f2326b7b379e285e630f59071e222c6e5bad522bd771088d86f3d00` |

Phases 1–3 move the remaining tile atlases, text banks, descriptions, story
pages and `Texture.tex` into recipes. Phase 4 removes the FS2 payload ZIPs and
compiles the plan in CI from the committed OFL fonts.

## Changing text

1. Edit the string or box in `resources/043F.json`.
2. Compile. The compiler fails fast on a missing font, a box outside the atlas,
   or text that does not fit its box.
3. Run the private `fs2/work/tools/recipe_e2e.py` gate. It installs all three
   languages into a throwaway game, compares the decoded `043F` with the
   payload's bytes, and exercises switching, restore and rejected-plan paths.
4. Update `expected/<language>.json` only when the new pixels are the intended
   result, and record the new hashes in the change description.
