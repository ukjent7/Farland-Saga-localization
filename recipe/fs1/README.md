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
- `FarlandSaga.bin` (the disc image, ~3.4 MB of text-bank diff records) and
  `Texture.tex` (a size-changing SSF atlas re-encode) are still **diff
  record streams** taken from the committed language payloads. Compiling
  these from recipes is later work.

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
  table pairs), `SYSTEM/KANJI.FON` glyph cells (Unifont renderings), and the
  fixed-capacity byte patches for the menu/item/system data files — together
  with the structural facts (per-file LBA and size, and the container-offset
  ranges of the sectors the operations regenerate). The installer rebuilds
  these files from the user's pristine disc, so the repository carries no
  game-derived bytes for them. Title sprites, theme files, directory-sector
  bytes and a few parity-only rewrites still travel as diff records until
  their recipes exist; `tools/derive_fs1_disc.py verify` is the golden gate
  proving that pristine + declarations + the pruned records reproduce every
  language's shipped container byte for byte.
- `payload/farland-patch-<version>.zip` — the committed per-language diff
  payloads. Each ZIP carries a `manifest.json` and one diff object per target
  file, exactly as `fls.storage.package_release` wrote them. The compiler
  uses them for the disc and texture record streams and as the authority on
  every file's original and patched identity; the exe/Unity diff objects
  inside them are no longer read.
- `plan.lock.json` — the uncompressed plan blob's length and SHA-256. The
  compiler refuses to emit a plan with different bytes, so a payload or
  declaration change cannot reach a release by accident; rerun
  `tools/compile_recipe_fs1.py --write-plan-lock` after reviewing one.

## Compiling the plan

```sh
bash uv.sh run --locked python tools/compile_recipe_fs1.py
```

The compiler renders every declared glyph token through Unifont (failing on
render errors), sorts and validates the declared patches, packs the committed
language ZIPs for the record-stream files, cross-checks the three languages'
original identities, and reconciles the declarations against the ZIP
manifests: a declared file must agree on `patched_sha256` and `size`, and the
manifest stays the authority for original identities and patched sizes.
The result is `patcher/plan/saga1/plan.bin`, which the default build embeds
via `go:embed`. The plan starts with `FS1PLAN1\n`, a little-endian u32 header
length, a compact JSON header, and a gzip blob. The header declares file
identities and versions; each per-language file entry carries either a blob
range of diff records or a list of operations (`patch` writes declared bytes
at an offset, `font` overlays rendered glyph cells onto the exe's
LZ-compressed overlay font region) — never both. The gzip wrapper depends on
the host zlib, so the lock covers the uncompressed blob. CI compiles the plan
before every test and release build, and the compiler never reads the game.

`--declarations <dir>` points at a different `exe.json`/`unity.json` pair;
the tests use it to compile synthetic declarations.

## Maintainer tooling

The declarations are derived from pristine originals, a maintainer step that
needs the game:

```sh
bash uv.sh run --locked python tools/derive_fs1_ops.py extract --backup <pristine backup dir>
bash uv.sh run --locked python tools/derive_fs1_ops.py verify  --backup <pristine backup dir>
```

`extract` re-derives `exe.json`/`unity.json` from the originals; `verify`
rebuilds every language's exe and Unity files from the declarations alone and
compares the hashes against them. The disc counterpart is
`tools/derive_fs1_disc.py`: `extract --backup <dir> --patched-bin <container>`
re-derives `disc.json` from the pristine and patched discs, and
`verify --backup <dir>` rebuilds every language's shipped container from the
pristine disc, the declarations and the pruned ZIP records, requiring the
container SHA-256 to match the payload manifests. End users and CI never run
these: compiling the plan needs only the committed files.

## Install-time replay

`patcher/plan_saga1.go` parses and fully validates the plan before the game
directory is touched, then rebuilds each file from the pristine original
backup. Record-stream files replay the diff records exactly as the ZIP
payload engine does (`payload.go`). Operation files verify the original's
size and SHA-256 first, then apply the operations in order — copying the
declared bytes into place and re-encoding the font region through the LZ
format — and finally verify the rebuilt length and SHA-256 against the plan.
A tampered original is refused before any write.

## Later phases

The remaining record streams are the disc image and the texture. The
follow-up work mirrors the sequel's recipe migration: compile the disc text
banks from `translation/corpus.json` and express the texture as a texture
recipe, so the committed payloads can eventually be dropped.
`plan.lock.json` keeps every intermediate step pinned and reviewable.
