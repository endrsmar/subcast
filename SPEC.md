# vidstreamer — Specification

A CLI tool for Ubuntu that casts video to a **Chromecast Ultra**, with subtitle support
(separate `.srt`/text files *or* subtitles embedded in a container such as `.mkv`), where the
video source can be either a **local file** or a **web (HTTP/HTTPS) resource** that is streamed
and cast at the same time.

This document is the implementation contract. `VALIDATION.md` defines the acceptance criteria
that an implementation loop must satisfy, phase by phase.

---

## 1. Goals & non-goals

### 1.1 Goals (v1)
1. Discover and select a Chromecast on the LAN; cast to a Chromecast Ultra.
2. Cast a **local** video file (`.mp4`, `.mkv`, `.webm`, …).
3. Cast a **web** video resource by streaming it through the local machine while casting.
4. Show subtitles from:
   - a sidecar `.srt` (or other text subtitle) file supplied by the user, and
   - subtitles **embedded** in the container (e.g. MKV text tracks), selectable by track index/language.
5. Make casting "just work" for inputs the Chromecast can't natively play, by **remuxing** or
   **transcoding** on the fly when needed (and not otherwise).
6. Basic playback control from the CLI: play/pause, stop, seek, volume.

### 1.2 Non-goals (v1, defer)
- Graphical UI (a later phase; the architecture must keep core logic UI-agnostic).
- Casting from online *services* that need extractors (YouTube, Netflix, etc.). v1 handles a
  *direct* media URL only. (Leave a seam for a future `yt-dlp` resolver.)
- Multi-device / audio-group / synchronized multiroom casting.
- Persistent media library, playlists/queues beyond a single item.
- DRM-protected content.

---

## 2. Target platform & dependencies

- **OS:** Ubuntu (Linux). Must run on Ubuntu 22.04+ (Python 3.10+ available).
- **Language:** Python 3.10+.
- **System dependencies (must be detected at startup, with a clear error if missing):**
  - `ffmpeg` and `ffprobe` (transcoding, remuxing, subtitle extraction, media analysis).
- **Python libraries (pinned in `pyproject.toml`):**
  - `PyChromecast` (device discovery + media control). Target the current major (>=14).
  - `zeroconf` (mDNS discovery; pulled in by PyChromecast).
  - `aiohttp` (async local HTTP server with Range support and the ability to stream a piped
    subprocess body). *Rationale:* the server must simultaneously (a) serve byte-range requests
    for direct play and (b) stream an ffmpeg pipe for on-the-fly remux/transcode; an async server
    handles many concurrent Range/segment requests from the device cleanly.
  - `click` (CLI parsing) — or `argparse` if a zero-dependency CLI is preferred. Either is acceptable.
- **Packaging:** installable via `pip install .` exposing a console entry point `vidstreamer`.

The implementation MUST NOT assume an internet egress for the casting path itself except when the
source is a web resource.

---

## 3. Domain facts the design is built on

These are fixed constraints from the Chromecast platform; the implementation must respect them.

### 3.1 Chromecast Ultra media support
- **Video codecs:** H.264 (High Profile up to L5.1, up to 4K30), **HEVC/H.265** Main & Main10
  (up to 4K60), VP8, VP9 Profile-2 (up to 4K60). *Practical caveat:* some 4K HEVC streams fail in
  the field; treat 4K HEVC as "attempt direct, allow fallback to transcode" (configurable).
- **Audio codecs:** AAC-LC, MP3, Opus, Vorbis, FLAC; AC-3/E-AC-3 passthrough (HDMI) — but to be
  safe, the default-compatible audio target is **AAC-LC stereo**.
- **Containers the Default Media Receiver accepts:** **MP4**, **WebM**, MP3, WAV, etc.
  **MKV / Matroska is NOT an accepted container** even when its codecs are supported — it must be
  remuxed (container change only, no re-encode) to fragmented MP4 or WebM.
- **HDR/Dolby Vision:** Ultra supports HDR10/Dolby Vision, but only end-to-end (HDR file + HDR TV).
  v1 does not attempt tone-mapping; HDR streams are passed through when codec/container allow.

### 3.2 Subtitle support
- The Default Media Receiver accepts **side-loaded text tracks in WebVTT only** (`text/vtt`),
  referenced by URL in the load request's `tracks` array and enabled via `activeTrackIds`.
- **CORS is mandatory** for tracks. When media has any track, BOTH the media stream and the track
  stream must be served with CORS headers. Required headers include `Access-Control-Allow-Origin`,
  and the server must allow/echo `Content-Type`, `Accept-Encoding`, and `Range`. The local server
  will send permissive CORS headers on every response.
- **Text** subtitle formats (SRT, WebVTT, ASS/SSA text, `mov_text`) can be **converted to WebVTT**.
  ASS styling/positioning is lost in the conversion (text is preserved).
- **Image-based** subtitle formats (**PGS/HDMV**, **VOBSUB/DVD**) **cannot** be converted to WebVTT.
  The only way to show them is to **burn them into the video** (hardsub), which forces a video
  re-encode. v1 behavior: detect, warn, and require an explicit opt-in flag to burn them in;
  otherwise proceed without that subtitle track.

### 3.3 pychromecast load API (reference)
`MediaController.play_media(url, content_type, *, title, thumb, current_time, autoplay,
stream_type, metadata, subtitles, subtitles_lang, subtitles_mime, subtitle_id, enqueue, …)` and
`enable_subtitle(track_id)`. `subtitles` is a WebVTT URL; `subtitles_mime` defaults to `text/vtt`.
`stream_type` ∈ {`BUFFERED`, `LIVE`}. Use **BUFFERED** for seekable direct/remux play; use **LIVE**
for non-seekable pipes (see §6.4).

---

## 4. High-level architecture

UI-agnostic core with a thin CLI on top. Suggested package layout:

```
vidstreamer/
  __init__.py
  cli.py            # arg parsing, command dispatch, interactive controls
  discovery.py      # find/select Chromecast devices (pychromecast wrapper)
  caster.py         # MediaController orchestration, status, playback control
  source.py         # Source abstraction: LocalSource / RemoteSource resolution + validation
  probe.py          # ffprobe wrapper -> MediaInfo (streams, codecs, container, sub tracks)
  compat.py         # Chromecast capability matrix + decision engine (direct/remux/transcode)
  transcode.py      # ffmpeg command construction (remux / transcode / burn-in) + process mgmt
  subtitles.py      # subtitle discovery, extraction, text->WebVTT conversion
  server.py         # aiohttp server: Range serving, ffmpeg-pipe streaming, /video /sub /healthz
  config.py         # defaults, env/flag config, logging setup
  errors.py         # typed exceptions
  __main__.py       # python -m vidstreamer
tests/
  ...
SPEC.md
VALIDATION.md
README.md
pyproject.toml
```

### 4.1 End-to-end flow
1. **Parse** CLI args → a `CastRequest` (source, subtitle spec, device selector, options).
2. **Resolve source** (`source.py`): classify local vs web; validate existence/reachability.
3. **Probe** (`probe.py`): run `ffprobe -show_streams -show_format -print_format json` to build a
   `MediaInfo` (container, video codec/profile/level/resolution/HDR, audio codec/channels,
   subtitle tracks with codec + language + whether text or image based).
   - For remote sources, probe the URL directly (ffprobe accepts http(s)); cap analyzeduration/probesize.
4. **Plan** (`compat.py`): produce a `StreamPlan`:
   - `video`: `copy` (direct) | `transcode(target=h264|hevc, …)`
   - `audio`: `copy` | `transcode(aac)`
   - `container`: `mp4` (fragmented) | `webm` | `passthrough`
   - `serve_mode`: `direct_range` (serve the original bytes) | `ffmpeg_pipe` (remux/transcode pipe)
   - `subtitle_plan`: which track(s) → WebVTT side-load, or burn-in, or none.
5. **Prepare subtitles** (`subtitles.py`): produce a WebVTT file/bytes per selected text track;
   for sidecar `.srt`, convert; for embedded text track, extract+convert; for image track marked
   burn-in, fold into the transcode command instead.
6. **Start local server** (`server.py`) bound to the LAN-facing IP on an ephemeral port; register
   routes for the (possibly transcoded) video and each WebVTT track. Detect the correct outbound
   LAN IP (the address the Chromecast can reach — see §6.5).
7. **Cast** (`caster.py`): connect to the chosen device, `play_media(...)` with the local video URL,
   content type, subtitle URL(s) + `stream_type`, then `enable_subtitle()` for the active track.
8. **Control loop:** print status; accept interactive commands (play/pause/seek/volume/stop/quit)
   or run unattended until the media ends or the user interrupts. Clean up server + ffmpeg on exit.

---

## 5. Compatibility decision engine (`compat.py`)

Given `MediaInfo` and options, decide the minimal-work plan. Order of preference:
**direct play > remux (container only) > transcode (re-encode)** — do the least work that yields a
playable stream, because re-encoding is CPU-heavy and lossy.

Decision table (video):
| Source video codec | Container | Decision |
|---|---|---|
| H.264 ≤ High@L5.1, ≤4K30 | mp4/webm-compatible | **direct** (copy) |
| H.264 compatible | mkv/avi/other | **remux** to fragmented mp4 (`-c:v copy`) |
| HEVC Main/Main10 ≤4K60 | mp4 | **direct** (allow transcode fallback for 4K HEVC if configured) |
| HEVC compatible | mkv | **remux** to mp4 (`-c:v copy`) |
| VP8/VP9 | webm | **direct** |
| VP9 | mkv | **remux** to webm (`-c:v copy`) |
| anything else (e.g. AV1 unsupported, H.264 > L5.1) | any | **transcode** to H.264 High |

Decision table (audio): if codec ∈ {AAC-LC, MP3, Opus, Vorbis, FLAC} and channels supported → copy;
else transcode to `aac` stereo. (Audio transcode alone does **not** force a video re-encode — copy
video, transcode audio, remux container.)

Subtitle interaction: if a selected subtitle track is **image-based** and the user passed the
burn-in opt-in, the plan MUST upgrade video to **transcode** (burn-in requires re-encode).

`serve_mode`:
- `direct_range`: video==copy AND audio==copy AND container already MP4/WebM-compatible → serve the
  original file bytes with HTTP Range (seekable, `BUFFERED`).
- `ffmpeg_pipe`: any remux or transcode → stream ffmpeg stdout (see §6.4 for seek handling).

The engine must be **configurable/overridable** via flags: `--force-transcode`, `--no-transcode`
(fail instead of transcoding), `--video-codec`, `--audio-codec`, `--max-height`.

---

## 6. Local HTTP server (`server.py`)

### 6.1 Responsibilities
- Serve the video to the Chromecast: either the original file with Range support, or an ffmpeg pipe.
- Serve each WebVTT subtitle track.
- Send permissive **CORS** headers on every response (`Access-Control-Allow-Origin: *`,
  `Access-Control-Allow-Headers: Content-Type, Range, Accept-Encoding`,
  `Access-Control-Allow-Methods: GET, HEAD, OPTIONS`, `Accept-Ranges: bytes`) and answer `OPTIONS`.
- Set correct `Content-Type` (`video/mp4`, `video/webm`, `text/vtt`).
- Bind to `0.0.0.0` on an ephemeral port; advertise URLs using the LAN IP (§6.5).
- For a **remote source in direct-play mode** where the remote already serves valid ranges+CORS,
  the implementation MAY hand the remote URL straight to the device (no proxy). When subtitles are
  involved or CORS is uncertain, proxy through the local server.

### 6.2 Routes
- `GET/HEAD /video` — the media stream.
- `GET/HEAD /sub/<id>.vtt` — a WebVTT track.
- `GET /healthz` — returns 200 (used by startup self-test and validation).
- `OPTIONS *` — CORS preflight → 204 with CORS headers.

### 6.3 Direct Range serving
Implement HTTP/1.1 byte ranges correctly: parse `Range: bytes=start-end`, respond `206 Partial
Content` with `Content-Range` and the correct slice; respond `200` with full `Content-Length` when
no Range; support open-ended ranges (`bytes=N-`). This is what enables Chromecast seeking on direct
and remux-to-file play.

### 6.4 ffmpeg-pipe serving and seeking (the hard part)
On-the-fly remux/transcode produces a non-seekable stdout stream. Two supported strategies; v1 MUST
implement at least **(A)** and SHOULD implement **(B)**:

- **(A) Restart-on-seek (required).** Stream ffmpeg stdout as the response body. Tell the device
  `stream_type=BUFFERED` but treat the pipe as resumable: when the user issues a seek, kill the
  current ffmpeg, relaunch with `-ss <target>` (input seek for speed), and re-issue `play_media`
  pointed at `/video?t=<target>` so the device reloads from the new origin. The server maps the
  `t` query param to the ffmpeg `-ss` value. Output muxer flags for fragmented MP4:
  `-movflags +frag_keyframe+empty_moov+default_base_moof -f mp4`. For WebM, `-f webm`.
- **(B) On-the-fly segmenting (optional, better seeking).** Produce HLS/fMP4 segments via ffmpeg
  and serve a playlist; enables native scrubbing without ffmpeg restarts. Optional for v1.

Audio/video sync and start latency: prefer **input** `-ss` (before `-i`) for fast seeks; verify A/V
sync after seek in validation. Use `-fflags +genpts` if timestamps are missing.

### 6.5 LAN IP detection
Determine the local address the Chromecast can reach (not `127.0.0.1`). Strategy: open a UDP socket
"connected" to the Chromecast's IP (or `8.8.8.8` as fallback) and read `getsockname()[0]`. The
chosen IP MUST be on the same subnet/reachable by the device; expose `--bind-ip` to override.

---

## 7. Subtitle pipeline (`subtitles.py`)

Inputs that must be supported:
1. **Sidecar file** via `--subtitles PATH` (or auto-detect a `.srt`/`.vtt` next to a local video
   with the same basename when `--auto-subs` is set). Text formats: SRT, WebVTT, ASS/SSA.
2. **Embedded track** via `--sub-track <index|lang>` (e.g. `--sub-track 2` or `--sub-track eng`).
   Default selection when `--subtitles`/`--sub-track` not given: none, unless `--auto-subs` picks
   the first forced/default text track or a preferred-language track (`--sub-lang`).

Processing rules:
- **SRT / text sidecar → WebVTT:** convert (timestamps `,`→`.`, prepend `WEBVTT` header, strip
  numeric counters). Handle BOM/`utf-8-sig`; detect & transcode unknown encodings to UTF-8.
  `ffmpeg -i in.srt out.vtt` is acceptable; an in-process converter is also acceptable and avoids a
  subprocess. Either way output MUST be valid WebVTT.
- **Embedded text track → WebVTT:** `ffmpeg -i INPUT -map 0:s:<n> -f webvtt OUT.vtt` (or extract
  then convert). ASS is converted as plain text (styling dropped).
- **Image-based track (PGS/VOBSUB):** cannot become WebVTT. If `--burn-subs` is set, fold into the
  transcode via the `subtitles`/`overlay` filter (forces re-encode); otherwise emit a clear warning
  and continue without it.
- Encoding: always emit UTF-8 WebVTT. Validate the output starts with `WEBVTT`.
- Multiple tracks: v1 may side-load multiple WebVTT tracks but only one is active at a time
  (`enable_subtitle`). Track selection at runtime is a nice-to-have.

---

## 8. CLI interface (`cli.py`)

Primary command:
```
vidstreamer cast <SOURCE> [options]
```
`<SOURCE>` = local path or http(s) URL.

Options (minimum set):
- `-d, --device <name|ip>`   Target device by friendly name or IP. If omitted and exactly one is
  found, use it; if multiple, list and prompt (or error in `--non-interactive`).
- `-s, --subtitles <path>`   Sidecar subtitle file.
- `--sub-track <index|lang>` Select an embedded subtitle track.
- `--sub-lang <lang>`        Preferred subtitle language for auto-selection.
- `--auto-subs`              Auto-detect sidecar / default embedded subtitle.
- `--burn-subs`              Burn the selected (esp. image-based) subtitles into the video (re-encode).
- `--no-subs`                Disable subtitles entirely.
- `--force-transcode` / `--no-transcode`
- `--video-codec <h264|hevc>` / `--audio-codec <aac|copy>` / `--max-height <px>`
- `--bind-ip <ip>` / `--port <n>`
- `--non-interactive`        No prompts; exit after starting playback (for scripting/tests).
- `--volume <0.0-1.0>`
- `-v/-vv`                   Verbosity; `--json-status` for machine-readable status output.

Auxiliary commands:
- `vidstreamer devices`      Discover and list Chromecasts (name, model, IP). Exit 0 even if none,
  printing a clear "no devices found" message.
- `vidstreamer probe <SOURCE>`  Print the `MediaInfo` + the computed `StreamPlan` (no casting).
  This is the key **introspection command for automated validation** (machine-readable with `--json`).
- `vidstreamer stop [-d device]`  Stop playback / quit the receiver app on a device.

Interactive controls (when attached, not `--non-interactive`): `space`=play/pause, `←/→`=seek ±10s,
`↑/↓`=volume, `s`=cycle subtitle track, `q`=quit. A simple line-based command mode is acceptable if
raw key handling is troublesome.

Exit codes: `0` success; `2` usage error; `3` source not found/unreachable; `4` no device / device
unreachable; `5` ffmpeg/dependency missing; `6` unsupported media with `--no-transcode`; `1` other.

---

## 9. Error handling & UX
- Detect missing `ffmpeg`/`ffprobe` at startup → exit 5 with install hint (`sudo apt install ffmpeg`).
- Source not found / URL unreachable → exit 3 with the offending path/URL.
- No Chromecast found (after a bounded discovery timeout, default 8s, `--timeout`) → exit 4.
- Image subtitles without `--burn-subs` → warn, continue without that track.
- ffmpeg failures → surface stderr tail; non-zero exit with context.
- Always clean up: terminate ffmpeg children and stop the HTTP server on exit/interrupt (SIGINT).
- Logging via `logging`; default human-friendly, `--json-status` for structured.

---

## 10. Configuration
- Flags override env vars override defaults. Env prefix `VIDSTREAMER_` (e.g. `VIDSTREAMER_DEVICE`,
  `VIDSTREAMER_BIND_IP`, `VIDSTREAMER_PORT`).
- No config file required in v1 (optional `~/.config/vidstreamer/config.toml` is a nice-to-have).

---

## 11. Implementation phases (for the loop)

Each phase is independently testable. The loop should implement and validate them in order; later
phases must not regress earlier validation. See `VALIDATION.md` for the concrete criteria/IDs.

- **P0 — Scaffolding & deps.** Package skeleton, `pyproject.toml`, entry point, dependency check,
  `vidstreamer --version/--help`, logging/config. *(Validation group V0)*
- **P1 — Media probing.** `probe.py` + `vidstreamer probe` producing accurate `MediaInfo` JSON for
  local files (mp4/mkv/webm) and a remote URL. *(V1)*
- **P2 — Compatibility engine.** `compat.py` `StreamPlan` decisions; surfaced via `probe --json`.
  Pure logic, unit-testable with synthetic `MediaInfo`. *(V2)*
- **P3 — Subtitle pipeline.** SRT→WebVTT, embedded-text extraction→WebVTT, image detection +
  burn-in plan. Pure/ffmpeg, file-level tests, no device. *(V3)*
- **P4 — Local HTTP server.** Range serving, CORS, WebVTT route, ffmpeg-pipe streaming, LAN IP
  detection, `/healthz`. Tested with a local HTTP client (no device). *(V4)*
- **P5 — Discovery & control.** `discovery.py`/`caster.py`; `vidstreamer devices`. Mockable;
  hardware test casts a known-good MP4. *(V5)*
- **P6 — End-to-end casting.** Wire it all: direct play, remux MKV, transcode, sidecar subs,
  embedded subs, remote source, seeking. *(V6 — includes hardware acceptance tests)*

---

## 12. Risks & open questions
- **Seeking on transcoded streams** is the most fragile area; restart-on-seek (A) is the v1 floor.
- **4K HEVC** field failures → keep a configurable transcode-fallback.
- **HDR** passthrough without tone-mapping may look washed out on SDR TVs; document, don't fix in v1.
- **Hardware-dependent validation:** several criteria need a real Chromecast Ultra + TV; these are
  marked `[HW]` in `VALIDATION.md` and gated behind a `VIDSTREAMER_TEST_DEVICE` env var so the loop
  can run the non-HW suite unattended and defer HW checks to a human-in-the-loop run.

---

## 13. Sources
- Supported Media for Google Cast — https://developers.google.com/cast/docs/media
- Add Advanced Features (tracks/CORS) — https://developers.google.com/cast/docs/web_sender/advanced
- DefaultMediaReceiver subtitles + CORS — https://github.com/thibauts/node-castv2-client/wiki/How-to-use-subtitles-with-the-DefaultMediaReceiver-app
- pychromecast media controller — https://github.com/home-assistant-libs/pychromecast/blob/master/pychromecast/controllers/media.py
- catt (SRT→WebVTT on the fly, no transcode) — https://pypi.org/project/catt/
- pychromecast local subtitles example — https://gist.github.com/arqtiq/0dc302797d80b2a68fdc0e06dd970818
- FFmpeg fragmented MP4 / remux — https://ffmpeg.org/ffmpeg-formats.html
- Extracting subtitles with FFmpeg — https://www.mux.com/articles/extracting-subtitles-and-captions-from-video-files-with-ffmpeg
- Chromecast Ultra HEVC caveats — http://www.multipelife.com/play-4k-hevc-on-chromecast-ultra.html
