# Recipe fonts

The recipe names fonts by role. `recipe/fonts.json` resolves each role to a
file committed under `recipe/fonts/`, so a clean checkout and CI rasterise the
same glyphs as a local build.

| Role | File | License | Used for |
| --- | --- | --- | --- |
| `NotoSansSC` | `NotoSansSC-Regular-subset.otf` | OFL-1.1 | Simplified Chinese |
| `Galmuri11` | `Galmuri11-subset.ttf` | OFL-1.1 | Korean |
| `LiberationSans` | `LiberationSans-Regular-subset.ttf` | OFL-1.1 | English |
| `Unifont` | `unifont-subset.hex.gz` | OFL-1.1 | Title menu, place names and story text at 16px |

The `.otf`/`.ttf` files are subsets containing the characters used by the
committed recipes, including the story fallback glyphs.
`unifont-subset.hex.gz` is a filtered Unifont hex file used by the bitmap
renderer. License texts are next to the fonts.

When recipe text adds a new character, regenerate the affected subset. The
compiler fails on a missing glyph rather than silently drawing `.notdef`.
