# Recipe fonts

The recipe names fonts by role. `recipe/fonts.json` resolves each role to a
file committed under `recipe/fonts/`, so a clean checkout and CI rasterise the
same glyphs as a local build.

| Role | File | License | Used for |
| --- | --- | --- | --- |
| `NotoSansSC` | `NotoSansSC-Regular-subset.otf` | OFL-1.1 | Simplified Chinese |
| `NotoSansSCAtlas` | `NotoSansSC-Atlas-subset.otf` | OFL-1.1 | Simplified Chinese in `Texture.tex` |
| `Galmuri11` | `Galmuri11-subset.ttf` | OFL-1.1 | Korean (11px boxes, small story pages) |
| `Galmuri14` | `Galmuri14-subset.ttf` | OFL-1.1 | Korean (14px boxes, story text) |
| `Galmuri11Atlas` | `Galmuri11-Atlas-subset.ttf` | OFL-1.1 | Korean in `Texture.tex` |
| `LiberationSans` | `LiberationSans-Regular-subset.ttf` | OFL-1.1 | English |
| `Unifont` | `unifont-subset.hex.gz` | OFL-1.1 | Bitmap fallback for story pages a TTF grid cannot fit |

The `.otf`/`.ttf` files are subsets containing the characters used by the
committed recipes, including the story fallback glyphs.
`unifont-subset.hex.gz` is a filtered Unifont hex file used by the bitmap
renderer. License texts are next to the fonts.

Pixel-grid fonts (`Galmuri*`) only render at their native size (Galmuri11 at
11px, Galmuri14 at 14px); the compiler never resamples them above the grid.
Vector fonts (`NotoSansSC`, `LiberationSans`) rasterise through a 4x
supersampled coverage pass before the 1-bit threshold, which keeps dense CJK
strokes separated at the engine's small UI sizes.

The `*Atlas` roles cover the larger, multi-size text in `Texture.tex` and are
kept separate so extending atlas coverage cannot change the pixels of the
already-verified UI and story resources.

When recipe text adds a new character, regenerate the affected subset. The
compiler fails on a missing glyph rather than silently drawing `.notdef`.
