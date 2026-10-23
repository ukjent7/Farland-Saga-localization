# Recipe fonts

The recipe names fonts by role, never by a machine-specific path. A public
build resolves those names through `fonts.json`; a private build may add a
`fonts.local.json` beside this file with absolute paths, and that file is
ignored by git.

Public entries point at files committed under `recipe/fonts/`:

```json
{
  "fonts": {
    "Galmuri11": {"file": "fonts/Galmuri11.ttf", "license": "OFL-1.1"}
  }
}
```

Phase 0 is a byte-parity pilot against the fonts used by the existing private
pipeline, so its font map lives in `fonts.local.json` only. Phase 1 switches
the public recipe to OFL fonts and commits the subset files and licenses.
