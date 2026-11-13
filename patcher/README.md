# Patcher

One Go module, two builds: **FARLAND SAGA I** (the default build) and **FARLAND SAGA II** (`-tags saga2`). Both are standalone executables distributed to end users: they verify the identity of the game files, back them up, replay diffs, replace them atomically, and can restore the original at any time. They need no Python and never touch the private workspace.

The install engine, restore path, verification, backup policy, and interface are shared. The two games differ only in product names, file names, payload format, and the executable addresses of their encryption tables; those differences live in `game_saga1.go` / `game_saga2.go` (plus `payload_saga1.go`/`payload_saga2.go`, `cdrom_saga2.go`, `embedded_saga1.go`/`embedded_saga2.go`), selected by the build tag. The engine files carry no per-game branches.

The Windows build ships a GUI that works on double-click; every platform keeps the command line and the interactive wizard.

The release form is a **single executable**. Both games embed a compiled
install plan via `go:embed`: the first game carries
`plan/saga1/plan.bin`, compiled by `tools/compile_recipe_fs1.py` from the
public declarations in `recipe/fs1/` (exe glyph slots and patches, Unity text
patches, disc event/font/graphics/directory operations, texture atlas) plus
the identity manifest `recipe/fs1/identities.json` (every file's
original/patched identity); the sequel carries
`plan/saga2/plan.bin`, compiled from the public `recipe/` and its OFL fonts.
The installer reads the user's pristine files, verifies their identity, and
replays the plan locally. Neither build needs a separate archive or a
directory picker.

```bash
FarlandSagaPatcher.exe                  # GUI: pick a language, then install or restore
FarlandSagaPatcher.exe wizard           # Console wizard: install or restore
FarlandSagaPatcher.exe install --yes    # Non-interactive install
FarlandSagaPatcher.exe restore --yes    # Restore the original
FarlandSagaPatcher.exe status           # Show the current status
FarlandSagaPatcher.exe verify           # Verify and return an exit code
```

`FarlandSaga2Patcher.exe` accepts exactly the same commands for the sequel.

## Building

Go 1.25 or newer is required. The repository pulls in no third-party modules; `go.mod` holds only the standard library, and the GUI calls Win32 APIs directly, so no cgo is needed.

```bash
cd patcher
go vet ./... && go test ./...                       # FARLAND SAGA I (default build)
go vet -tags saga2 ./... && go test -tags saga2 ./...  # FARLAND SAGA II

# Local build (current platform — Linux is only for go vet / go test / round trips)
go build -o farland-saga-patcher .

# Cross-compile the release targets (without embedded patches)
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-s -w -H windowsgui' -o FarlandSagaPatcher-windows-amd64.exe .
GOOS=windows GOARCH=amd64 go build -tags saga2 -trimpath -ldflags='-s -w -H windowsgui' -o FarlandSaga2Patcher-windows-amd64.exe .
```

Windows is the only release target: the game is a Windows-only Steam build, so a Linux binary has no player to serve. Linux is still where `go vet`, `go test`, and the synthetic-game round trip run, which is why `console_other.go` and `gui_other.go` exist — but nothing ships a Linux executable.

The commands above produce a first-game program without patches; its language buttons show "(not provided)". For the single-file version aimed at users, use the unified entry point's `build-patcher`:

```bash
bash flow.sh build-patcher                                  # Compiles the plan and cross-compiles
bash flow.sh build-patcher --output release/patcher.exe     # Custom output location
```

This command runs `tools/compile_recipe_fs1.py` (which renders the exe/Unity
declarations in `recipe/fs1/` into operations and compiles
`plan/saga1/plan.bin` from them plus `identities.json`, verified against
`recipe/fs1/plan.lock.json`) and then cross-compiles. The
compiler reads only committed files, so the
release build needs no game files.

The sequel's plan comes from the public recipe:

```bash
./uv.sh run --locked python tools/compile_recipe.py
cd patcher
go build -tags saga2 -o FarlandSaga2Patcher .
```

`tools/compile_recipe.py` reads only `recipe/` and the committed OFL font subsets, so a clean checkout rebuilds the same plan byte for byte. That plan contains label layers, story-page images, texture layers, the executable patch, and original-file hashes; it contains no game bytes. Both `patcher/plan/saga1/plan.bin` and `patcher/plan/saga2/plan.bin` are ignored by git and produced by CI before every test and release build.

The Windows artifact adds `-H windowsgui`: it declares the GUI subsystem so a double-click runs without a console window, while standard output remains available when subcommands are called from a terminal. `.github/workflows/release.yml` performs the same steps for both build tags on a pushed tag or manual confirmation and uploads the two executables plus `SHA256SUMS` as release assets. The patch data is compiled in, so CI never touches the private workspace.

## GUI

Running the Windows build with no arguments (or `FarlandSagaPatcher.exe gui`) opens the main window. The interface is two screens, each holding only what is currently needed:

| Screen | Contents |
| --- | --- |
| Screen 1: pick a language | Title, three language buttons (**简体中文 / 한국어 / English**), with a badge on the button of an already-installed language |
| Screen 2: install and restore | The title becomes the chosen language, one status line, **Install** and **Restore** buttons, and a **Back** button |

When no game directory is detected on screen 1, an extra line appears and all language buttons are disabled. Screen 2's status line reads "not installed yet" or "some language installed", and shows current progress while busy; the progress bar appears only during writes and collapses when idle, so screen 2 is normally just two buttons and a back button.

To use it, put the program in the game root (alongside `FarlandSaga.exe`) and double-click. The game directory is inferred from the program's own directory; when it is not in the right place all buttons are disabled and the missing files are named. To switch languages, press Back to screen 1 and pick again — the program handles the switch, always replaying diffs against the original backup, never layering one patch on another.

### Interface Language

All interface text goes through the three tables in `i18n.go` (Simplified Chinese, 한국어, English); nothing is hard-coded in the control code. There are only two rules:

- **Screen 1's three buttons show each language's name in that language itself** (`nativeLanguageName`). The user has not picked a language yet at this step, so a button must speak for itself — a Korean cannot read the Chinese word for Korean. The installed badge follows it, in screen 1's current language.
- **After a language is chosen, references to other languages use the current interface language to name them** (`uiStrings.name`). So the status line in a Korean UI reads "한국어 설치됨" and in an English UI reads "Installed: Korean".

Before screen 1's language is chosen, which text is used is decided by the operating system's UI language (`GetUserDefaultUILanguage`): Chinese systems use the Chinese table, Korean systems the Korean table, everything else falls back to English. After the choice the whole table switches at once — page title, status line, buttons, prompts, confirm boxes, and message boxes change together; pressing Back to screen 1 keeps the last chosen language instead of reverting to the system language, so switching back and forth does not flicker. The window title bar is the game's own title in all three languages (the `appTitle` constant, `FARLAND SAGA I Saturn Tribute` or `FARLAND SAGA II Saturn Tribute`) — the program is a three-language patcher and belongs to none of them.

The Korean table additionally specifies the `Malgun Gothic` font (`fontFace`) so it does not render as boxes on a Chinese Windows without the Korean language pack; the other two tables use the system dialog font. Switching language rebuilds the font and re-applies it to every control.

New fields must be added to all three tables; `TestUIStringsComplete` checks every field for empty values by reflection, so a missing translation fails in the test rather than surfacing to the user as a string of English. A language that cannot be obtained falls back to the English table.

The command-line wizard (`wizard`) and console output remain in Chinese and are not localized — they target scripts and troubleshooting, not the double-click path.

Install and restore run on a worker thread so the UI never blocks; progress is coalesced at one refresh per 100 ms, so a 4 KiB diff block each does not disturb the message loop. Busy state is managed with an incrementing token: each operation takes a token, returns only its own at the end, and results invalidated by a later operation still return theirs, so the UI can never get stuck in a busy state because of one discarded check. Closing the window during a write is intercepted with a prompt to wait, and a 3-minute watchdog is the final fallback for an abnormal busy state.

Interface events (results posted back from the worker thread, patch loading at startup) are delivered through a buffered channel. After queuing a closure the worker thread also `PostMessage`s a custom message to wake the UI thread, so `GetMessage` is guaranteed to pick it up; `WM_TIMER` is demoted to the watchdog's metronome and no longer carries events to the interface. Earlier, `WM_TIMER` was the only wake source — a system-synthesized low-priority message that may arrive late under queue pressure, or never, leaving queued closures unexecuted forever: the UI stays at its initial state with all buttons disabled, which looks exactly like a hang.

More important than the timer: `runGUI` calls `runtime.LockOSThread()` on entry. Win32's window, timers, and posted messages all hang off the message queue of the OS thread that created the window, while Go's scheduler can move a goroutine to a different thread between system calls. Once it moves, the window belongs to the original thread but the message loop runs on the new one, so `SetTimer` and `PostMessage` messages land in the original thread's queue and the new thread's `GetMessage` waits forever for something that never comes — a total UI freeze. This kind of freeze writes no logs and uses zero CPU; the only way to find it is to dump the goroutine stacks and see the thread blocked in `GetMessage`.

To diagnose whether the UI is genuinely frozen, set the environment variable `FARLAND_PATCHER_TRACE` before running: the program writes the key startup steps and throttled message types to `farland-patcher-trace.log` in the temporary directory, and writes nothing during normal use. Note that `Get-Process.Responding` and `SendMessageTimeout` are unreliable for processes started through WSL interop and will steadily report "not responding"; trust the trace log and the message stream instead.

The interface depends only on the standard library and `user32` / `comctl32` / `gdi32` / `kernel32`, with struct layouts checked field by field against the Win32 SDK and compile-time size assertions at the bottom of the file. Non-Windows platforms have no GUI, and the `gui` subcommand gives a clear message and falls back to the wizard.

## Command Line

| Command | Purpose |
| --- | --- |
| `install` | Verify identity, back up, write patch files, write the install record; automatic rollback on failure |
| `restore` | Overwrite patched files from the original backup; no rollback directory is left behind — reinstalling once undoes it |
| `status` | Show the status of every target file plus the install record |
| `verify` | Verify strictly and return an exit code, for scripted use |
| `gui` | Open the GUI (Windows only) |
| `wizard` | Console wizard, available on every platform |

Common options: `--game <dir>` (or the `FARLAND_SAGA_GAME` environment variable), `--plan <file>` (recipe plan; defaults to the embedded plan), `--lang <code>` (required when an external plan contains more than one language), `--engine recipe|payload`, `--payload <file>` (first game, or an external bundle for troubleshooting), `--yes` (skip interactive confirmation), `--json` (emit JSON), `-h`.

The command line targets scripts and troubleshooting: it accepts any plan, patch bundle, and game directory, free of the GUI's "the program's directory is the game directory" constraint.

## Patch Payload Format

Neither game's release build carries a ZIP. The sequel's `plan/saga2/plan.bin` format is described in [../recipe/README.md](../recipe/README.md); its installer replays the disc image from the pristine base and generates `FarlandSaga2.exe` and `Texture.tex` from the recipe. The first game's `plan/saga1/plan.bin` starts with `FS1PLAN1\n`, a
little-endian u32 header length, a compact JSON header, and a gzip blob. The
header declares the game (`saga1`), the language list with each candidate
version, every target file's kind (`disc` or `binary`) and original identity,
and each language's per-file patched identity plus either a blob range or an
operation list. Each file entry carries exactly one of the two
representations: a **record stream** (blob range of 4 KiB diff blocks, used
for `FarlandSaga.bin` and `Texture.tex`) or a list of **declarative
operations** (used for `FarlandSaga.exe` and the Unity files). A `patch` op
copies declared bytes to a file offset; a `font` op re-encodes the exe's
LZ-compressed overlay font region with rendered Unifont glyph cells. Ops
carry no game-derived bytes: the glyphs are rendered from Unifont at compile
time and the patch bytes are public translation text. The installer fully
validates the plan — blob hash, path safety, hashes, op bounds and
non-overlap, and a complete walk of every record stream — before the game
directory is touched, then rebuilds each file from the original backup:
record files replay their records, operation files verify the original's
identity, apply the ops in order, and verify the rebuilt hash. A tampered
original is refused before any write. `recipe/fs1/README.md` documents the
plan's sources and lock. The ZIP format below remains in use for external troubleshooting bundles via `--payload`:

```
manifest.json            Manifest: version, language, and each target file's original/patched identity
objects/<sha256>.gz      Diff object; the filename is the patched file's SHA-256
```

`manifest.json`:

```json
{
  "format": 1,
  "kind": "farland-saga-patch",
  "game": "FARLAND SAGA I Saturn Tribute",
  "version": "zh-CN-unifont08",
  "language": "zh-CN",
  "files": [
    {
      "path": "FarlandSaga.bin",
      "original_sha256": "…64 lowercase hex…",
      "original_size": 301359116,
      "patched_sha256": "…",
      "patched_size": 301359116,
      "object": "objects/<patched_sha256>.gz"
    }
  ]
}
```

A diff object is a gzip stream: the first line is a JSON header (`format`, `sha256`, `size`, and an optional `base`), followed by consecutive 12-byte records (little-endian `uint64` offset plus `uint32` length) and their data blocks. Replay supplies the changed blocks in record order and reads the gaps from the original file; the rebuilt result's length and SHA-256 must match the object's name. The object header's `base.path` is a private-workspace path recorded for reference only; the actual base is given by the manifest's original hash.

Path safety: paths in the manifest and install record accept only POSIX relative paths, rejecting absolute paths, drive letters, backslashes, and parent traversal; ZIP members' CRC32 is verified block by block by `archive/zip` as it reads.

## Install Record and Backup

After a successful install the game directory holds only two things:

| Name | Contents |
| --- | --- |
| `FarlandSagaPatcher-install.json` | Install record: version, language, file list, backup directory, status |
| `FarlandSagaPatcher-backup/` | The original files — **exactly one for the whole lifecycle** |

The backup directory name is fixed and never timestamped. Its usability is decided by the manifest's original hash: if it is complete it is reused directly, without copying a single byte; if the game files happen to still be original it is rebuilt in place; if the record names a legacy timestamped directory it is renamed in place. All three languages' patches derive from the same original, so `original_sha256` agrees and switching languages needs no new backup — the game directory does not grow with install count.

After a successful install, legacy patchers' `FarlandSagaPatcher-backup-<timestamp>/`, `FarlandSagaPatcher-restore-rollback-<timestamp>/`, and crash remnants `FarlandSagaPatcher-rollback/` are swept, and the space reclaimed is reported on the command line. During a language switch `FarlandSagaPatcher-rollback/` temporarily holds the files that are not currently original and is deleted as soon as the install ends.

When the backup's original files are incomplete and the game files are already patched, the install is refused with a pointer to Steam's "Properties → Installed Files → Verify integrity of game files" to recover the original — a patched file cannot be reversed into an original, and this is the only reliable route. `status` reports backup completeness up front, so you do not have to discover it by failing a restore.

Directory names in the install record are always single segments relative to the game directory, so the whole game directory can be moved and still restored.

## Security Boundary

- Only files listed in the manifest are modified; a write is refused when a file is neither the original nor a patch installed by this program.
- Diff objects are always replayed against the **original**, with the base taken from the original backup rather than the current file in the game directory, so switching languages never layers diffs on another patch.
- Patched files are written to a temporary file, hash-verified, then renamed atomically; if any step fails, the files already replaced are rolled back from the backup just made.
- Before writing, the target files are opened writable as a test; on Windows a running game locks the image files, and the user is told to exit the game first.
- On restore, if a file has been modified since installation, the overwrite is refused and the user is told to back it up themselves first.

Known limits: Linux has no mandatory file locking, so process-occupancy checks only work on Windows; console output switches to the UTF-8 code page on Windows, and Chinese may render as boxes when the terminal font lacks the glyphs (functionality is unaffected).

## Tests

`patcher_test.go` covers diff replay round trips and every corruption rejection (record out of order, out of range, truncated, hash mismatch, base mismatch, illegal header), path validation for the manifest and install record, install/restore/switch/idempotency/rollback, and rejection when the backup directory is tampered with. Backup policy has dedicated cases: a language switch creates no new directory, a restore leaves no directory, legacy timestamped directories are migrated and swept, a missing backup is rebuilt from the original, and a lost backup on a patched install refuses to install while leaving no residue. The tests write objects in the same format from the Go side and need no private workspace.

The per-game test fixtures live beside the build tags: `game_saga1_test.go` names the FARLAND SAGA I originals (the archived backup plus `work/analysis/FarlandSaga.exe`), `game_saga2_test.go` names the sequel's install directory and `FarlandSaga2.exe`. `TestEmbeddedRealPayloadReplay` therefore reconstructs every embedded payload under both build tags whenever those files are present, and skips when they are not. Under `-tags saga2` it replays the embedded recipe plan and compares the generated bytes with the hashes and sizes the generator reports, including the disc image and the re-encoded texture. `cdrom_saga2_test.go` holds the sector-transform and LZ/permutation-table tests that only apply to the sequel format.

Embedded patch loading lives in the platform-neutral `embedded.go`, so it can be exercised on Linux: `TestLoadEmbedded` verifies that all three language bundles or plan languages parse, that the order is fixed as Chinese-Korean-English, and that each declares every target file with valid hashes; when no payload or plan is embedded (a fresh clone before compilation) the test skips. Interface-shared helpers (`blockedFiles`, `summarizeStates`, `samePath`) and entry-point dispatch also have unit tests. The Win32 UI code itself only compiles on Windows and cannot run under Linux `go test`; its correctness rests on `GOOS=windows go vet`, the compile-time struct size assertions, and install/restore round trips against the real payload on Windows.
