# subcast — Validation Criteria

Acceptance criteria the implementation loop must satisfy. Each criterion has an **ID**, a
**method** (how to check it), and a **pass condition**. Criteria are grouped by the phases in
`SPEC.md §11`. A phase is "done" only when all its non-`[HW]` criteria pass and earlier phases
still pass (no regressions).

**Legend**
- `[AUTO]` — fully automated; runs in CI / unattended loop. No Chromecast required.
- `[HW]` — requires a real Chromecast Ultra + TV on the LAN. Gated behind env var
  `SUBCAST_TEST_DEVICE=<name|ip>`. Skipped (not failed) when the var is unset.
- `[MANUAL]` — needs a human to eyeball the TV (subtitle rendering, A/V sync). Logged as a checklist
  item; the loop records "pending manual confirmation", does not block automated progress on it.

**Test harness expectations**
- `pytest` is the test runner; `tests/` mirrors the module layout.
- Tiny synthetic media fixtures are generated with ffmpeg at test time (see §Fixtures) — do NOT
  commit large binaries.
- The Chromecast control plane is mocked for `[AUTO]` device tests (a `FakeChromecast` exposing the
  `media_controller` surface used by `caster.py`), so casting orchestration is testable without HW.
- A machine-readable surface is mandatory: `subcast probe --json` and `--json-status` emit JSON
  the tests assert against. This is the primary automated oracle.

---

## Fixtures (generated at test setup, `[AUTO]`)
Generated via ffmpeg `lavfi`/`testsrc` so they are tiny and license-free:
- `compat.mp4` — H.264 High + AAC, 320x240, 3s (direct-play candidate).
- `remux.mkv` — H.264 + AAC in **Matroska** (remux candidate; compatible codecs, wrong container).
- `embedded_text.mkv` — H.264 + AAC + an **embedded SRT/text** subtitle track tagged `eng`.
- `embedded_image.mkv` — a container with an **image-based (e.g. PGS/dvd_subtitle)** track
  (or a synthetic stand-in whose ffprobe `codec_name` is image-based) for the burn-in path.
- `transcode.ext` — a codec the Ultra does not support (e.g. **AV1** or MPEG-4 ASP) → transcode path.
- `sample.srt` — a few well-formed cues, including a comma-decimal timestamp and a UTF-8 BOM variant.
- A local static-file HTTP server serving `compat.mp4` to emulate a "remote" source for probe/proxy.

---

## V0 — Scaffolding & dependencies (Phase P0)
- **V0.1 [AUTO]** `subcast --version` prints a semver and exits 0.
- **V0.2 [AUTO]** `subcast --help` lists `cast`, `devices`, `probe`, `stop` and exits 0.
- **V0.3 [AUTO]** With `ffmpeg`/`ffprobe` present, dependency check passes silently. When `PATH` is
  stubbed to hide them, the tool exits **5** with a message naming the missing binary and the
  `apt install ffmpeg` hint.
- **V0.4 [AUTO]** `python -m subcast` is equivalent to the `subcast` entry point.
- **V0.5 [AUTO]** `pip install .` succeeds in a clean venv and installs the console script.

## V1 — Media probing (Phase P1)
- **V1.1 [AUTO]** `subcast probe compat.mp4 --json` reports container `mp4`/`mov`, video
  `h264`, audio `aac`, correct width/height, and `subtitle_tracks: []`.
- **V1.2 [AUTO]** `probe remux.mkv --json` reports container `matroska`, video `h264`, audio `aac`.
- **V1.3 [AUTO]** `probe embedded_text.mkv --json` lists ≥1 subtitle track with `language: eng` and
  `text_based: true` (codec e.g. `subrip`/`ass`/`mov_text`).
- **V1.4 [AUTO]** `probe embedded_image.mkv --json` lists a subtitle track with `text_based: false`
  (codec e.g. `hdmv_pgs_subtitle`/`dvd_subtitle`).
- **V1.5 [AUTO]** `probe http://127.0.0.1:<port>/compat.mp4 --json` (local static server standing in
  for a web resource) succeeds and reports the same stream info as V1.1 within a bounded time
  (probesize/analyzeduration capped; no full download).
- **V1.6 [AUTO]** Probing a nonexistent path exits **3**; an unreachable URL exits **3**. Both name
  the offending source.

## V2 — Compatibility engine (Phase P2)
Unit tests feed synthetic `MediaInfo` to `compat.plan()`; also assert via `probe --json` which
includes the `StreamPlan`.
- **V2.1 [AUTO]** compat.mp4 → plan: `video=copy`, `audio=copy`, `serve_mode=direct_range`,
  `container=passthrough`.
- **V2.2 [AUTO]** remux.mkv → plan: `video=copy`, `audio=copy`, `container=mp4`,
  `serve_mode=ffmpeg_pipe` (remux, no re-encode).
- **V2.3 [AUTO]** AV1/unsupported → plan: `video=transcode(h264)`, `serve_mode=ffmpeg_pipe`.
- **V2.4 [AUTO]** Compatible video + unsupported audio (synthetic) → `video=copy`,
  `audio=transcode(aac)` (audio-only re-encode never sets video=transcode).
- **V2.5 [AUTO]** `--force-transcode` forces `video=transcode` even for compat.mp4.
- **V2.6 [AUTO]** `--no-transcode` on the AV1 fixture exits **6** (unsupported, refused to transcode).
- **V2.7 [AUTO]** Selecting an **image-based** subtitle track with `--burn-subs` upgrades the plan to
  `video=transcode` and records the burn-in filter; without `--burn-subs` the plan keeps the video
  decision unchanged and flags the track as dropped-with-warning.

## V3 — Subtitle pipeline (Phase P3)
- **V3.1 [AUTO]** `sample.srt` → WebVTT: output begins with `WEBVTT`, timestamps use `.` decimals,
  numeric counters removed, and it parses as valid WebVTT.
- **V3.2 [AUTO]** BOM/`utf-8-sig` SRT input is handled (no stray BOM in output; valid UTF-8 out).
- **V3.3 [AUTO]** A non-UTF-8 (e.g. Latin-1/Windows-1250) SRT is transcoded to valid UTF-8 WebVTT.
- **V3.4 [AUTO]** Embedded **text** track of `embedded_text.mkv` extracts to WebVTT beginning with
  `WEBVTT` and containing the expected cue text.
- **V3.5 [AUTO]** Embedded **image** track is correctly classified as not convertible: without
  `--burn-subs` a warning is emitted and processing continues with no WebVTT for that track.
- **V3.6 [AUTO]** With `--burn-subs`, the constructed ffmpeg command for `embedded_image.mkv`
  contains a burn-in (`subtitles=`/`overlay`) filter and re-encodes video. (Command construction
  asserted; a short real burn-in render is a `[MANUAL]` visual check.)
- **V3.7 [MANUAL]** Burned-in subtitles are visually present and legible on the TV.

## V4 — Local HTTP server (Phase P4)
Tested with an in-process HTTP client (`aiohttp`/`httpx`) against the running server; no Chromecast.
- **V4.1 [AUTO]** `GET /healthz` → 200.
- **V4.2 [AUTO]** `GET /video` (direct mode, compat.mp4) without Range → 200, `Content-Length` ==
  file size, `Content-Type: video/mp4`, `Accept-Ranges: bytes`.
- **V4.3 [AUTO]** `GET /video` with `Range: bytes=100-199` → **206**, `Content-Range:
  bytes 100-199/<size>`, body is exactly those 100 bytes.
- **V4.4 [AUTO]** Open-ended `Range: bytes=<size-10>-` → 206 returning the final 10 bytes.
- **V4.5 [AUTO]** Every response (incl. `/sub/<id>.vtt`) carries CORS headers
  (`Access-Control-Allow-Origin`, allowed headers incl. `Range`/`Content-Type`/`Accept-Encoding`);
  `OPTIONS /video` → 204 with those headers.
- **V4.6 [AUTO]** `GET /sub/0.vtt` → 200, `Content-Type: text/vtt`, body starts with `WEBVTT`.
- **V4.7 [AUTO]** `GET /video` in **ffmpeg_pipe** mode (remux.mkv) streams a non-empty `video/mp4`
  body that ffprobe (reading from the served URL) parses as a valid fragmented MP4 with the expected
  codecs.
- **V4.8 [AUTO]** Restart-on-seek: `GET /video?t=1.0` in pipe mode starts ffmpeg with input `-ss 1.0`
  and returns a stream whose first decodable PTS is ≈ the requested offset (±1 keyframe interval).
- **V4.9 [AUTO]** LAN IP detection returns a non-loopback IPv4 (not `127.0.0.1`) on a host with a
  real interface; `--bind-ip` overrides it.
- **V4.10 [AUTO]** On shutdown/SIGINT, the server stops and no orphan ffmpeg processes remain.

## V5 — Discovery & control (Phase P5)
- **V5.1 [AUTO]** `caster.py` orchestration against `FakeChromecast`: `cast compat.mp4
  --non-interactive` results in exactly one `play_media` call with `content_type=video/mp4`, a
  `/video` URL on the detected LAN IP, and `stream_type=BUFFERED` for direct mode.
- **V5.2 [AUTO]** With a sidecar `.srt`, the `play_media` call includes a `subtitles` URL ending
  `.vtt` with `subtitles_mime=text/vtt`, and `enable_subtitle` is called for the active track.
- **V5.3 [AUTO]** Pipe/transcode mode sets `stream_type` appropriately (BUFFERED with restart-on-seek
  per SPEC §6.4) and the seek control triggers an ffmpeg restart + reload at the new offset.
- **V5.4 [AUTO]** `subcast devices` with a mocked discovery returns the fake device's
  name/model/IP; with no devices it prints "no devices found" and exits 0.
- **V5.5 [AUTO]** `--device <name>` selects the matching fake device; an unknown name exits **4**.
- **V5.6 [AUTO]** Playback controls map to the right controller calls (pause→`pause()`,
  resume→`play()`, stop→`stop()`, volume→`set_volume()`, seek→`seek()`/reload).
- **V5.7 [HW]** `subcast devices` lists the real Chromecast Ultra (name + IP) within the
  discovery timeout.

## V6 — End-to-end (Phase P6)
Non-HW items assert orchestration end-to-end with the fake device + real server + real ffmpeg.
- **V6.1 [AUTO]** Direct play (compat.mp4) end-to-end: server serves Range, fake device receives a
  valid load request; `--json-status` reports `PLAYING`/buffering transitions from faked status.
- **V6.2 [AUTO]** Remux MKV (remux.mkv) end-to-end through the pipe; served stream is valid fMP4.
- **V6.3 [AUTO]** Transcode path (AV1 fixture) end-to-end produces an H.264/AAC fMP4 stream.
- **V6.4 [AUTO]** Sidecar-subtitle E2E: WebVTT served at `/sub/..`, load request references it,
  `enable_subtitle` called.
- **V6.5 [AUTO]** Embedded-text-subtitle E2E (embedded_text.mkv): extracted WebVTT served & referenced.
- **V6.6 [AUTO]** Remote-source E2E: source = `http://127.0.0.1:<port>/compat.mp4`; tool streams it
  (proxy or direct per plan) and the fake device gets a playable load request — proving "stream and
  cast at the same time".
- **V6.7 [AUTO]** Cleanup: after E2E run completes/aborts, HTTP server is closed and no orphan
  ffmpeg processes remain (assert process table).
- **V6.8 [HW][MANUAL]** Real cast: compat.mp4 plays on the TV via the Ultra; play/pause/seek/volume
  from the CLI take effect.
- **V6.9 [HW][MANUAL]** Real cast of an MKV with **embedded subtitles** shows the subtitles on the TV.
- **V6.10 [HW][MANUAL]** Real cast of a video with a **sidecar `.srt`** shows the subtitles on the TV.
- **V6.11 [HW][MANUAL]** Real cast of a **web URL** streams and plays on the TV with subtitles.
- **V6.12 [HW][MANUAL]** Seeking during a transcoded/remuxed stream resumes near the target with A/V
  in sync.

---

## Definition of done (v1)
- All `[AUTO]` criteria (V0–V6) pass under `pytest` on Ubuntu 22.04+ with ffmpeg installed.
- All `[HW]`/`[MANUAL]` criteria pass at least once on a real Chromecast Ultra and are recorded in a
  manual test log (`docs/manual-test-log.md`) with date + device + result.
- No orphaned processes/sockets after any run; clean exit codes per `SPEC.md §8`.
- `README.md` documents install (incl. `apt install ffmpeg`), usage, and the known-limitations list
  (image subs require `--burn-subs`; transcoded-stream seeking is restart-based; HDR not tone-mapped).
