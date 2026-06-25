"""Shared pytest fixtures: tiny ffmpeg-generated media, no committed binaries."""

from __future__ import annotations

import functools
import shutil
import subprocess
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed"
)

SRT_TEXT = (
    "1\n"
    "00:00:00,500 --> 00:00:01,500\n"
    "Hello from vidstreamer\n"
    "\n"
    "2\n"
    "00:00:02,000 --> 00:00:02,800\n"
    "Second cue\n"
)


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def _src_video(dst: Path, vcodec: str, container_args: list[str] | None = None) -> None:
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "testsrc=d=3:s=320x240:r=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", vcodec, "-pix_fmt", "yuv420p",
        # Keyframe every ~1s so stream-copy seeks (restart-on-seek) are testable.
        "-g", "10",
        "-c:a", "aac", "-shortest",
    ]
    if container_args:
        cmd += container_args
    cmd.append(str(dst))
    _run(cmd)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("media")

    srt = d / "sample.srt"
    srt.write_text(SRT_TEXT, encoding="utf-8")

    # BOM and non-UTF-8 variants for the subtitle decoder tests.
    (d / "sample_bom.srt").write_text(SRT_TEXT, encoding="utf-8-sig")
    (d / "sample_latin1.srt").write_text(
        SRT_TEXT.replace("Second cue", "Café crème"), encoding="latin-1"
    )

    # compat.mp4 — H.264 + AAC in MP4: direct-play candidate.
    _src_video(d / "compat.mp4", "libx264")

    # remux.mkv — compatible codecs, wrong container: remux candidate.
    _src_video(d / "remux.mkv", "libx264")

    # transcode.mp4 — MPEG-4 ASP video (unsupported): transcode candidate.
    _src_video(d / "transcode.mp4", "mpeg4")

    # embedded_text.mkv — add an embedded SRT (text) track tagged eng.
    _run([
        FFMPEG, "-y", "-i", str(d / "remux.mkv"), "-i", str(srt),
        "-map", "0:v", "-map", "0:a", "-map", "1",
        "-c:v", "copy", "-c:a", "copy", "-c:s", "srt",
        "-metadata:s:s:0", "language=eng",
        str(d / "embedded_text.mkv"),
    ])

    # NOTE: an embedded *image-based* subtitle (PGS/dvd_subtitle) cannot be
    # generated from text on ffmpeg builds without a text->bitmap path (e.g. the
    # 4.4 build on Ubuntu 22.04). Per VALIDATION.md, the image case uses a
    # synthetic ffprobe-shaped stand-in driven through the real parsing code; see
    # the `image_probe_raw` fixture and build_media_info().

    return d


# A synthetic ffprobe `-show_streams -show_format` payload for a container with
# an H.264 video, AAC audio, and an *image-based* (dvd_subtitle) track. Parsed by
# the real build_media_info() so V1.4/V2.7/V3.5 exercise actual classification.
IMAGE_PROBE_RAW = {
    "format": {"format_name": "matroska,webm", "duration": "3.0"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "profile": "High", "level": 40, "width": 320, "height": 240,
         "pix_fmt": "yuv420p", "avg_frame_rate": "10/1"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac",
         "channels": 2, "sample_rate": "44100", "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "dvd_subtitle",
         "tags": {"language": "eng"}, "disposition": {"default": 1}},
    ],
}


@pytest.fixture
def image_probe_raw():
    """Synthetic ffprobe output for media with an image-based subtitle track."""
    import copy

    return copy.deepcopy(IMAGE_PROBE_RAW)


@pytest.fixture(scope="session")
def http_media_server(media_dir):
    """Serve media_dir over HTTP to emulate a remote source."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(media_dir))
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
