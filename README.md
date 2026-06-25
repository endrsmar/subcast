# vidstreamer

Cast local or web video to a **Chromecast Ultra** from Ubuntu, with subtitles —
either a sidecar `.srt` or subtitles embedded in the container (e.g. `.mkv`).
Incompatible containers are remuxed and incompatible codecs transcoded on the fly,
so casting "just works" without pre-converting files.

See `SPEC.md` for the full design and `VALIDATION.md` for the acceptance criteria.

## Install

Requires Python 3.10+ and **ffmpeg**:

```bash
sudo apt install ffmpeg
pip install .
```

For development (tests included):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest
```

## Usage

```bash
# Discover devices
vidstreamer devices

# Inspect what would happen (no casting) — media info + the stream plan
vidstreamer probe movie.mkv
vidstreamer probe movie.mkv --json

# Cast a local file
vidstreamer cast movie.mkv

# Cast with a sidecar subtitle file
vidstreamer cast movie.mp4 --subtitles movie.en.srt --sub-lang eng

# Pick an embedded subtitle track (by index or language) and a device
vidstreamer cast movie.mkv --sub-track eng -d "Living Room"

# Burn in image-based subtitles (PGS/VOBSUB) — forces a re-encode
vidstreamer cast movie.mkv --sub-track 0 --burn-subs

# Cast a direct web URL (streamed through this machine while casting)
vidstreamer cast https://example.com/clip.mp4

# Stop playback
vidstreamer stop -d "Living Room"
```

Interactive controls while attached: `p` pause, `r` resume, `s <sec>` seek,
`v <0-1>` volume, `q` quit. Use `--non-interactive` for scripting.

## How it works

1. **Probe** the source with `ffprobe` (codecs, container, subtitle tracks).
2. **Plan** the minimal work: direct play → remux (container only) → transcode.
3. **Subtitles**: convert text subs (SRT/embedded text) to WebVTT; image subs can
   only be burned in (`--burn-subs`).
4. **Serve** the (possibly transcoded) video + WebVTT over a local HTTP server with
   CORS and byte-range support; the Chromecast streams from it.
5. **Cast** via the Chromecast Default Media Receiver.

## Known limitations (v1)

- **Image-based subtitles** (PGS/VOBSUB) cannot be converted to WebVTT; pass
  `--burn-subs` to hardcode them into the video (this forces a re-encode).
- **Seeking on transcoded/remuxed streams** uses *restart-on-seek*: ffmpeg is
  relaunched at the new offset and the device reloads. Seek precision on stream
  copies is bounded by the source keyframe interval.
- **HDR** content is passed through, not tone-mapped; it may look washed out on an
  SDR TV.
- **Web sources** must be a *direct* media URL. Service extractors (YouTube, etc.)
  are out of scope for v1 (a `yt-dlp` resolver is a planned seam).

## Validation notes / deviations from `VALIDATION.md`

- **Image-subtitle fixtures (V1.4, V2.7, V3.5):** ffmpeg 4.4 (Ubuntu 22.04) has no
  text→bitmap path, so a real PGS/VOBSUB file can't be generated locally. Per the
  spec's allowance, these use a synthetic ffprobe-shaped stand-in run through the
  real `build_media_info()` parser, exercising the actual classification logic.
- **Pipe validation (V4.7, V4.8, V6.x):** the served stream is validated by
  fetching the bytes over HTTP (as the Chromecast does — linearly) and probing the
  capture, rather than pointing `ffprobe` at the URL. `ffprobe`-over-HTTP attempts
  to *seek* a non-seekable pipe, which a real Chromecast never does.
- **PyChromecast pin:** `>=13,<15`. PyChromecast 14 requires Python 3.11+, but the
  stated target (Ubuntu 22.04) ships Python 3.10; 13.x exposes the same load API.
- **`--non-interactive`** keeps the local server alive and blocks until the media
  ends or `Ctrl-C`, since exiting would stop the stream it is serving.
