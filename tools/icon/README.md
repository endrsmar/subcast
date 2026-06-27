# Icon generator

Generates the subcast app icon with Google's **Nano Banana Pro**
(`gemini-3-pro-image`) via the Gemini image-generation REST API. Stdlib only —
no SDK, no extra dependencies.

## Setup

Get an API key at <https://aistudio.google.com/apikey>, then export it. The tool
reads the key from an env var (never from the command line):

```bash
export GEMINI_API_KEY="..."     # or GOOGLE_API_KEY
```

## Usage

```bash
# default: renders the subcast icon to tools/icon/out/subcast-icon.png
python tools/icon/generate_icon.py

# pick output, resolution, aspect ratio
python tools/icon/generate_icon.py --out my-icon.png --size 4K --aspect 1:1

# arbitrary prompt
python tools/icon/generate_icon.py --prompt "a minimal play button" --aspect 16:9

# edit/restyle an existing image instead of generating from scratch
python tools/icon/generate_icon.py --ref current-logo.png --prompt "make it flat and purple"
```

| flag | meaning |
|------|---------|
| `--prompt <text>` | text prompt (default: the subcast icon prompt baked into the script) |
| `--out <path>` | output path; extension is adjusted to the returned format |
| `--model <id>` | model id (default `gemini-3-pro-image`) |
| `--aspect <r>` | aspect ratio, e.g. `1:1`, `16:9`, `4:3` (default `1:1`) |
| `--size <1K\|2K\|4K>` | resolution (default `2K`) |
| `--ref <path>` | reference image to edit/restyle |

Exit codes: `0` ok · `1` usage/config (e.g. missing key) · `2` API/network · `3` no image returned.

Generated images land in `tools/icon/out/`, which is git-ignored.

## Stripping the background (`strip_bg.py`)

Nano Banana Pro can't emit transparency, so generate the glyph as a white shape
on a **flat solid-black** field, then key the field out with `strip_bg.py`
(wraps ImageMagick `convert`; no Python image libs needed):

```bash
python tools/icon/strip_bg.py tools/icon/out/subcast-glyph.jpg \
  -o tools/icon/out/subcast-glyph.png --size 512 --pad 0.1
```

| flag | meaning |
|------|---------|
| `--size <px>` | output square size (default 512) |
| `--pad <frac>` | padding as a fraction of size (default 0.12) |
| `--bg <colour>` | flat background colour to key out (default `black`) |
| `--fuzz <pct>` | colour tolerance when keying (default 30) |
| `--fill <colour>` | recolour the glyph (alpha preserved), or `keep` (default) |

## Full pipeline → web UI

The web UI logo and favicon are **inlined as base64** in
`src/subcast/web/index.html` (no static route needed). The tile colour is pure
CSS — a Plum Noir gradient `#18181b → #6d28d9`. To regenerate:

```bash
# 1. generate the glyph (white on flat black)
python tools/icon/generate_icon.py --prompt "<glyph prompt>" --size 1K \
  --out tools/icon/out/subcast-glyph.png
# 2. strip the background -> transparent master
python tools/icon/strip_bg.py tools/icon/out/subcast-glyph.jpg \
  -o tools/icon/out/subcast-glyph.png --size 512 --pad 0.1
# 3. build inline assets: a 192px glyph + a 128px favicon on the Plum Noir tile
#    (see the convert recipes used to produce tools/icon/assets/*), then base64
#    them into the `<link rel=icon>` and header `.logo img` in index.html.
```

Tracked source art lives in `tools/icon/assets/` (`subcast-glyph.png` master,
`glyph-192.png`, `favicon-128.png`).
