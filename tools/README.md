# Tools and the Unified Workflow

The default entry point is `bash flow.sh` in the repository root. It locates the project from its own path and runs the in-repo `tools/localize.py` through `uv.sh run --locked`. Reverse-engineering output goes to the private workspace `work/` next to the repository (`FARLAND_WORK` can change it). Commands, paths, and environments are WSL/Linux throughout. All three languages share the processors and translation tables; historical scripts are kept only for recovery and traceability.

## Status and Environment

```bash
bash flow.sh status
bash flow.sh doctor
bash flow.sh --help
bash uv.sh sync --locked
```

`status` only reads the actual install and candidate metadata; `doctor` checks the locked dependencies, candidate availability, the current install, and the original backup hashes. The install record is authoritative from `install_report.json` in the private workspace, and the current candidate from `config/storage_policy.json` in the private workspace.

Python 3.14 is required. The private workspace's `pyproject.toml`, `uv.lock`, and `.venv` form the runtime environment (including the proprietary IDALib wheel). The public repository ships its own `pyproject.toml` containing only public dependencies, with IDALib listed as the optional `ida` extra; both manifests are version-aligned. `uv.sh` prefers the private workspace project when it exists and falls back to the repository's own manifest for a standalone checkout. The environment and caches are `work/.venv`, `.uv-cache`, `.pycache`, and `.tmp`. Run an individual tool with `bash uv.sh run --locked python tools/<tool>.py`. Maintain dependencies with `uv.sh add/lock/sync --locked`; global pip is not used.

IDALib comes from `/home/kk/ida-pro-9.4`; the wheel and its source hash live in the private workspace's `vendor/idalib/`. Static disassembly uses that IDALib; Capstone must not be introduced without the user's permission. The old PowerShell wrappers and Win32 memory probes survive only in the historical archive.

## Editing and Reviewing Translations

The [translation notes](../translation/README.md) list the current topical tables. Ordinary text is edited only in `translation/corpus.json`, merging the three languages' translations by ID; older batches and candidate `inputs/` are preserved as frozen records. The original-text extractor is only for new blank workspaces — never use its output to overwrite existing translations.

Merge batches live inside `work/`, for example:

```json
{
  "translator": "reviewer",
  "translations": [
    {"id": "EVE/EVE01_1.EVT:0001", "translation": "久等了！", "status": "reviewed", "notes": "review notes"}
  ]
}
```

```bash
bash flow.sh merge --language zh-CN --draft translation/draft.json --overwrite
bash flow.sh merge --language ko-KR --draft translation/draft.json --overwrite
bash flow.sh merge --language en-US --draft translation/draft.json --overwrite
bash flow.sh merge
bash flow.sh review
```

`--overwrite` explicitly permits replacing an existing translation; without it, a merge refuses to overwrite differing text. Merging checks unknown and duplicate IDs, capacity, control characters, and language encoding, and updates only the named language's translations and review notes. The parameterless `merge` validates the current unified tables and does not write back from historical batches. The unified review page is [review.html](../review.html), with Japanese alongside all three languages.

Image titles, UI labels, and the front-end menu each use a shallow three-language topical table. The atlas configuration declares bounds, font, layout, and protected regions; after editing text you must build and check the capacity report.

## Bootstrapping the Workspace from a Game Install

Holding one Steam-purchased game install is enough to build the private workspace from scratch. The whole chain only reads the game directory: it never launches the game and never modifies the install. `--game` defaults to the install root next to the private workspace; when `FARLAND_WORK` points elsewhere, `--game` must be passed explicitly.

```bash
python tools/localize.py unpack --game <game root>
python tools/localize.py assets freeze --game <game root>
python tools/localize.py extract-unity
python tools/localize.py build-first --output zh-CN-seed01 --game <game root>
python tools/localize.py verify --stage zh-CN-seed01
```

`unpack` extracts the four SSF containers (`FarlandSaga.cue`, `FarlandSaga_Steam.ini`, `FarlandSaga.bin`, `Texture.tex`) into `decoded/`, restores the data track to `FarlandSaga_track1.bin`, extracts `extracted/steam/` per ISO9660, and places the analysis executable at `analysis/FarlandSaga.exe`. Re-runs are idempotent; a target exe that differs from the game's requires `--force`.

`assets freeze` freezes the originals whose identity the build must verify: `Texture.tex` is copied verbatim as an SSF container (atlas decoding needs the 2048-byte header, and the patch recipes pin the container hash), the five Unity originals are copied to `assets/sources/unity/S-Tribute_Data/`, `S-Tribute_Data/Managed/*.dll` is packed into a byte-reproducible `unity-managed.zip` (fixed timestamp and deflate level), and a `unity_original_files.json` manifest is written.

Both `unpack` and `assets freeze` carry an original guard: if the game directory has a patch installed they refuse to run, pointing you to `restore-original`, to a clean install via `--game`, or to an explicit `--force`. Unpacking a patched install yields patched resources, and the candidate would be built on the wrong foundation.

`extract-unity` rebuilds `config/unity.json` from the frozen Unity originals. The scan rule slides a window over raw MonoBehaviour data reading fixed-length UTF-8 segments, keeping Japanese and Traditional Chinese slots by script, excluding Hangul, other Latin slots, sound-list metadata, and editor notes containing newlines. Hits are aligned by id with the `kind == "unity"` rows of `translation/corpus.json`; anything not covered is written to `unity_unmatched.json` for manual confirmation. Rows present in the corpus but not hit by the scan raise an error, meaning the frozen originals and the translations have drifted. `--check` only validates and writes nothing. Row order follows scan order, and the rebuild matches the historical frozen table byte for byte.

`build-first` builds the first candidate directly from `extracted/steam`, depending on no existing candidate, so it can run before `build full --base`. It builds only the Saturn disc side; to get a full candidate including the Steam shell (Unity text), continue with `build full` on top of the first candidate. The image-encoding CD parity self-check prefers the private workspace's raw original disc image `reference_original.bin` and falls back to the repository's known-answer vectors when it is missing (the Steam edition ships without an original pressing's EDC/ECC, verified by measurement); the report's `cd_parity_reference` records which was actually used.

## Building and Patching

The base candidate is chosen by the current storage policy, and the output must be a new directory. The following commands only prepare resources; they do not produce an installable artifact:

```bash
bash flow.sh build full --base zh-CN-unifont07 --output zh-text-review --language zh-CN --resources-only
bash flow.sh build full --base ko-KR-unifont-full07 --output ko-text-review --language ko-KR --resources-only
bash flow.sh build full --base en-US-unifont-full07 --output en-text-review --language en-US --resources-only
```

Drop `--resources-only` to build a full candidate. A successful full `build/patch` updates the current candidate and tidies the historical files; a resource build does not update the candidate and cannot be installed. A full text rebuild preserves the base candidate's tiles, letter spacing, status labels, runtime save titles, and front-end atlas fixes.

The current Chinese draft's `EVE/EVE09_1.EVT` glyph table and script exceed the original image's 8192-byte allocation; the exact length is in the build output's `event_capacity.json`. Resource mode keeps the exact translation and marks it as unpackable in the capacity report; a full build rejects the overflow outright. The original image does not have enough unused all-zero sectors to relocate safely, and the audit is recorded in the private workspace's `work/maintenance/iso-free-extents.json`. This does not affect the installed 07 builds. When continuing to edit that event, review the glyph table's total capacity alongside the capacity report rather than judging by per-line length alone.

To re-run this capacity audit, use `tools/iso_extents.py` for a read-only scan of the data track; it reproduces every key figure in the audit record:

```bash
bash uv.sh run --locked python tools/iso_extents.py work/decoded/FarlandSaga_track1.bin
bash uv.sh run --locked python tools/iso_extents.py work/decoded/FarlandSaga_track1.bin --expected-sha256 <track hash> --min-run 5
```

An empty `free_zero_mode1.runs` means there are no contiguous free zero sectors that can be relocated safely. `plan_relocations` only permits relocation when the directory records agree with each other and match the frozen asset manifest; an incomplete scan is rejected outright.

Image topicals are built separately through the same entry point, reusing the base candidate's other resources:

```bash
bash flow.sh build titles --base zh-CN-unifont07 --output zh-title-review --language zh-CN --resources-only
bash flow.sh build ui --base ko-KR-unifont-full07 --output ko-ui-review --language ko-KR --resources-only
bash flow.sh build equipment --base en-US-unifont-full07 --output en-equipment-review --language en-US --resources-only
```

Prefer [config/patches/](../config/patches/README.md) for new fixes, executed by the shared processors. Add a processor only for a genuinely new encoding format, rather than copying scripts per version.

```bash
bash flow.sh patch --base en-US-unifont-full07 --output en-menu-review --language en-US --recipe frontend-submenus
bash flow.sh patch --base zh-CN-unifont07 --output zh-price-review --language zh-CN --recipe item-prices
bash flow.sh verify --stage en-menu-review
```

Atlases are generated from the frozen original at `assets/sources/Texture.tex`, which lives in the private workspace; candidates keep an input snapshot and a read-back report. The accumulated menu coverage is currently 371 text blocks and 7 hint panels. The save-title patch protects instructions, numbering, and the save/load logic; shop names write only the first 18 bytes and protect prices and attributes.

## Regression and Tidy-up

```bash
bash flow.sh test --cli-only
bash flow.sh test
bash flow.sh test --pack
bash flow.sh assets verify
bash flow.sh storage verify
bash flow.sh clean
bash flow.sh storage compact
```

Use `test --cli-only` for entry-point changes, `test` for shared resource changes, and add `--pack` when final image encoding is involved. Tests write only temporary files inside `work/` and may expand historical candidates as needed; re-archive them with the storage tools afterwards.

`clean --apply` removes only independently verifiable, rebuildable packaged intermediate images; `storage compact --apply` archives large historical files; `assets cleanup --apply` deduplicates previews after verification. The inventory, tidying, and restoration of historical small-file bundles are handled by the private workspace's `work/storage/` via `flow.sh storage`. Never manually delete backups, objects, or version manifests in the recovery chain.

## Install and Restore

Use this section only on an explicit install or restore instruction from the user. Exit the game from Windows first, then pre-flight; the pre-flight modifies neither the game nor the install record.

```bash
bash flow.sh install --language ko-KR --check-only
bash flow.sh install --language ko-KR --game-is-closed
bash flow.sh restore-original --game-is-closed
```

`--stage` and `--language` are mutually exclusive. An actual write requires an explicit `--game-is-closed`; the installer verifies the candidate, backs up the originals, and supports rollback on failure. WSL cannot inspect native Windows game processes. The workflow never launches the game automatically, and a resource build never installs.

## Release Packaging

```bash
# 1. Package the candidate into a diff payload (once per language)
bash flow.sh package-release --stage zh-CN-unifont08
bash flow.sh package-release --stage ko-KR-unifont-full07
bash flow.sh package-release --stage en-US-unifont-full07

# 2. Embed all patches and cross-compile the single-file patcher
bash flow.sh build-patcher
```

`package-release` first verifies identity file by file against the candidate report, then writes each file's 4 KiB diff blocks against the original backup into a payload ZIP (default `work/release/farland-patch-<version>.zip`); diff objects enter the cold store and are shared with the historical archive. `--output` changes the output directory. With `--patcher` it also produces the end-user bundle `FarlandSagaPatcher-<version>-win64.zip` (patcher plus GUI-only usage notes), ready to attach to a GitHub release. Repeated packaging of the same candidate is byte-identical. Windows is the only target: the game is a Windows-only Steam build, so no Linux bundle is produced.

`build-patcher` is the final form for end users: it copies each language's payload ZIP into `patcher/payload/`, embeds them via the patcher's `go:embed`, and cross-compiles the Windows build, producing a single executable (about 75 MB, of which about 71 MB is patch data). It embeds the zh-CN, ko-KR, and en-US candidates by default; `--stage` can be repeated to choose which to embed, and `--output` changes the output location. Missing payloads are packaged automatically first. `patcher/payload/*.zip` is a private-workspace artifact and is already gitignored. The patcher's own build, commands, and security boundary are in [../patcher/README.md](../patcher/README.md).

## Shared Modules

`localize.py` dispatches commands; `translation_data.py` provides the three-language data view and validation; `candidate.py` manages candidate copying, resource verification, glyph-table sync, and packaging; `native_patches.py` applies declarative patches; `container_atlases.py` handles the encrypted atlases; `build_alpha.py` and the image modules handle native text and Saturn image encoding; `unity_resources.py` handles Unity text; `bootstrap.py` unpacks and freezes the original assets from a purchased game install; `extract_unity_text.py` rebuilds the `unity.json` text-line table from the frozen Unity originals (`--check` only validates); `cd_parity.py` provides the known-answer vectors and pressing reference for CD sector verification; `iso_extents.py` read-only scans the ISO9660 data track for free zero-sector extents and plans safe relocations; `version_storage.py` manages large- and small-file archiving and restoration; `package_release.py` packages candidates into end-user diff payloads; `build_patcher.py` embeds patches and cross-compiles the single-file patcher; `workspace_assets.py` maintains shared previews and historical aliases; `path_compat.py` provides the REPO/WORK dual root and path compatibility.

Each resource processor keeps its own responsibility. Versioned one-off builds and legacy translation-task tools are archived and are not entry points for new work.
