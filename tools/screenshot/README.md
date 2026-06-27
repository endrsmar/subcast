# Web UI screenshot harness

Renders the real `src/subcast/web/index.html` in headless Chrome and
captures a chosen **scene** — a known UI state populated with representative
sample data — so you can screenshot any part of the player without a running
backend or an active casting session.

## Setup (one-time)

```bash
cd tools/screenshot
npm install            # installs the Playwright npm package (API only)
```

It uses your **system Chrome** by default, so you do *not* need
`npx playwright install`. If Chrome lives somewhere unusual:

```bash
CHROME_PATH=/path/to/chrome node shoot.mjs --scene player
```

## Usage

```bash
# from anywhere in the repo
node tools/screenshot/shoot.mjs --scene player --out /tmp/player.png

node tools/screenshot/shoot.mjs --list          # list scenes
node tools/screenshot/shoot.mjs --scene full --width 760   # responsive shell
node tools/screenshot/shoot.mjs --scene library --out lib.png
```

### Scenes

| scene      | captures                                  |
|------------|-------------------------------------------|
| `setup`    | the setup card (source / tracks / device) |
| `player`   | the playback control, mid-playback        |
| `settings` | the Settings modal                        |
| `library`  | the library sidebar (with sample items)   |
| `full`     | the whole shell (use `--width` for responsive) |

### Flags

| flag | meaning |
|------|---------|
| `--scene <name>` | scene to render (default `setup`) |
| `--out <path>`   | output PNG (default `<scene>.png`) |
| `--width <px>`   | viewport width (default per-scene) |
| `--selector <css>` | capture a specific element instead of the scene default |
| `--full-page`    | capture the whole page |
| `--scale <n>`    | deviceScaleFactor (default 2) |
| `--index <path>` | override the index.html location |

## Adding / tweaking states

Scenes live in `shoot.mjs` in the `SCENES` map. Each `apply` function runs in
the page and may call the page's own helpers (`setNowArt`, `renderLibrary`,
`fillLangSelect`, …) to drive the UI. Add a new entry to capture a new state
(e.g. a paused player, an error hint, the file-browser modal).
