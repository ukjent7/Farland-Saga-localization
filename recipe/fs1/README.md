# FS1 install plan

This directory holds the FARLAND SAGA I install-plan sources. The release form
matches the sequel's: the patcher embeds a compiled plan instead of payload
ZIPs, verifies the user's pristine files, and rebuilds the patched files
locally at install time.

The plan is a mix of two representations, and the split follows
reproducibility:

- `FarlandSaga.exe` and the two Unity files (`S-Tribute_Data/level0`,
  `S-Tribute_Data/resources.assets`) are **declarative operations**: font
  glyphs re-rendered from Unifont and small public patches. The repository
  carries no game-derived bytes for them.
- `FarlandSaga.bin` (the disc image) is **fully declarative**: event
  scripts, the disc font bank, the fixed-capacity data files, the title
  sprites, the translated menu graphics and the ISO directory updates are
  all install-time operations over the user's pristine disc. The plan
  carries no game-derived bytes for it.
- `Texture.tex` is **declarative** (`texture.json`, schema 2): the plan
  carries only the structural facts (part boxes, declared label regions and
  their constant backgrounds, panel rules) plus, per language, a sparse ink
  layer — the localized text pixels and their antialiasing. The installer
  decodes the user's pristine atlas, clears the declared regions, rebuilds
  the panel gradient from a row of that same pristine image, overlays the
  ink, re-encodes the PNG and re-encrypts the SSF container. Game artwork
  (portraits, sprites, UI frames) never leaves the user's own files.

## Layout

- `exe.json` — the executable's declarations: the overlay-font region
  (`start`/`limit`/`cells`), and per language the `glyph_slots` (slot → token
  rendered through Unifont) and `patches` (named offset + hex writes: the 24
  runtime word writes, the 1-byte jump, and the gamepad `.rdata` strings),
  plus each language's expected `patched_sha256`/`size`.
- `unity.json` — the Unity files' declarations: per language, per file, the
  named UTF-8 text patches and the expected `patched_sha256`/`size`.
- `disc.json` — the disc's declarations: per language, the event-script
  rebuilds (glyph-pair sequences per translated line plus each file's added
  table pairs), `SYSTEM/KANJI.FON` glyph cells (Unifont renderings), the
  fixed-capacity byte patches for the menu/item/system data files, the
  same-size graphics patches (the 50 `TITLE/TITLE*.SPR` title sprites and,
  for Korean and English, the translated `MENU.CEL`/`MENU_S.SPR`/
  `N_MENU.DAT`/`RESET.DAT` menu graphics), and the ISO directory-record
  updates (`dir_records` maps each grown or relocated event to its record's
  sector and offset; the `dirop` rewrites the record's extent/size fields in
  both endians). Together with the structural facts (per-file LBA and size,
  and the container-offset ranges of the sectors the operations regenerate)
  the installer rebuilds the whole disc from the user's pristine image, so
  the repository carries no game-derived disc bytes.
  `tools/derive_fs1_disc.py verify` is the golden gate proving that pristine
  + declarations alone reproduce every language's shipped container byte for
  byte.
- `texture.json` — the texture atlas declaration (schema 2): the image
  size, part count, the 371 declared label boxes with their backgrounds,
  the seven option-panel rules, and per language a sparse `mask`/`ink` PNG
  pair (text pixels only) plus the expected `patched_sha256`/`patched_size`
  of the rebuilt container. Regenerate it with
  `tools/derive_fs1_texture.py` from a pristine backup, the patched
  containers (private workspace) and the historical label declaration.
- `identities.json` — the identity manifest: per language, the candidate
  version and every target file's `original_sha256`/`original_size`/
  `patched_sha256`/`patched_size`. This is the authority on every file's
  original and patched identity; the compiler refuses a plan whose entries
  disagree with it. It was extracted from the historical payload manifests
  (`package-release` output).
- `plan.lock.json` — the uncompressed plan blob's length and SHA-256. The
  compiler refuses to emit a plan with different bytes, so a declaration
  change cannot reach a release by accident; rerun
  `tools/compile_recipe_fs1.py --write-plan-lock` after reviewing one.

## Compiling the plan

```sh
bash uv.sh run --locked python tools/compile_recipe_fs1.py
```

The compiler renders every declared glyph token through Unifont (failing on
render errors), sorts and validates the declared patches, takes the file
identities and versions from `identities.json`, and reconciles the
declarations against it: a declared file must agree on `patched_sha256` and
`size`.
The result is `patcher/plan/saga1/plan.bin`, which the default build embeds
via `go:embed`. The plan starts with `FS1PLAN1\n`, a little-endian u32 header
length, a compact JSON header, and a gzip blob. The header declares file
identities and versions; each per-language file entry carries either a blob
range of diff records or a list of operations (`patch` writes declared bytes
at an offset, `font` overlays rendered glyph cells onto the exe's
LZ-compressed overlay font region) — never both. The gzip wrapper depends on
the host zlib, so the lock covers the uncompressed blob. CI compiles the plan
before every test and release build, and the compiler never reads the game.

`--declarations <dir>` points at a different declarations directory; the
tests use it to compile synthetic declarations.

## Maintainer tooling

The declarations are derived from pristine originals, a maintainer step that
needs the game:

```sh
bash uv.sh run --locked python tools/derive_fs1_ops.py extract --backup <pristine backup dir> --payloads <historical payload dir>
bash uv.sh run --locked python tools/derive_fs1_ops.py verify  --backup <pristine backup dir>
```

`extract` re-derives `exe.json`/`unity.json` from the originals (the
historical payload ZIPs live in the private workspace; the repository no
longer carries them); `verify` rebuilds every language's exe and Unity files
from the declarations alone and compares the hashes against them. The disc
counterpart is `tools/derive_fs1_disc.py`: `extract --backup <dir>
--patched-bin <container> --language <code>` re-derives one language's disc
declarations from the pristine and that language's patched disc, `merge`
combines the three partial documents into `disc.json`, and
`verify --backup <dir>` rebuilds every language's shipped container from the
pristine disc and the declarations alone, requiring the container SHA-256 to
match `identities.json`. End users and CI never run these: compiling the
plan needs only the committed files.

## Install-time replay

`patcher/plan_saga1.go` parses and fully validates the plan before the game
directory is touched, then rebuilds each file from the pristine original
backup. Record-stream files replay the diff records exactly as the ZIP
payload engine does (`payload.go`). Operation files verify the original's
size and SHA-256 first, then apply the operations in order — copying the
declared bytes into place and re-encoding the font region through the LZ
format — and finally verify the rebuilt length and SHA-256 against the plan.
A tampered original is refused before any write.

The disc image is rebuilt the same way the golden gate proves it: the
installer seeds the output with a pristine copy and applies the disc
operations in place, so the SSF block transforms always see a
single-encryption-session container. Every changed byte lies inside a
declared sector (the extractor refuses undeclared changes), so the compiler
drops every record intersecting the declared ranges and the disc entry
carries operations only. Records would still be replayed raw after the
operations if any existed, mirroring `tools/derive_fs1_disc.py verify`.

`Texture.tex` is rebuilt from the plan's ink layers: the installer decodes
the user's pristine container with the exe-derived SSF parameters, applies
the declared region clears and panel rules, overlays the localized text
ink, re-encodes the PNG (updating the header's end/size fields, keeping the
4-byte alignment and the original Parts metadata), re-encrypts the whole
stream, and verifies the container hash against the plan.

## Later phases

No record streams remain: every target file is plan-built, and the
repository carries no game-derived bytes at all — the historical payload
ZIPs are gone, with `identities.json` as the identity authority.
`plan.lock.json` keeps every step pinned and reviewable.
