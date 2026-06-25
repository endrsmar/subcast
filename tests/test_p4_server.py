"""V4 — local HTTP server (Range, CORS, WebVTT, ffmpeg pipe, LAN IP)."""

from __future__ import annotations

import asyncio
import os
import subprocess

import aiohttp
import pytest

from vidstreamer.compat import plan_stream
from vidstreamer.netutil import detect_lan_ip
from vidstreamer.probe import probe_source
from vidstreamer.server import MediaServer
from vidstreamer.subtitles import convert_sidecar_to_vtt

pytestmark = pytest.mark.usefixtures("media_dir")


async def _make_server(media_dir, name, vtt=None):
    info = probe_source(str(media_dir / name))
    plan = plan_stream(info)
    server = MediaServer(plan=plan, info=info, vtt_path=vtt, bind_ip="127.0.0.1")
    await server.start()
    return server


async def test_v4_1_healthz(media_dir):
    server = await _make_server(media_dir, "compat.mp4")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{server.base_url}/healthz") as r:
                assert r.status == 200
    finally:
        await server.stop()


async def test_v4_2_full_get(media_dir):
    server = await _make_server(media_dir, "compat.mp4")
    size = os.path.getsize(str(media_dir / "compat.mp4"))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{server.base_url}/video") as r:
                body = await r.read()
                assert r.status == 200
                assert int(r.headers["Content-Length"]) == size
                assert r.headers["Content-Type"] == "video/mp4"
                assert r.headers["Accept-Ranges"] == "bytes"
                assert len(body) == size
    finally:
        await server.stop()


async def test_v4_3_range_request(media_dir):
    server = await _make_server(media_dir, "compat.mp4")
    size = os.path.getsize(str(media_dir / "compat.mp4"))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{server.base_url}/video",
                             headers={"Range": "bytes=100-199"}) as r:
                body = await r.read()
                assert r.status == 206
                assert r.headers["Content-Range"] == f"bytes 100-199/{size}"
                assert len(body) == 100
    finally:
        await server.stop()


async def test_v4_4_suffix_range(media_dir):
    server = await _make_server(media_dir, "compat.mp4")
    size = os.path.getsize(str(media_dir / "compat.mp4"))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{server.base_url}/video",
                             headers={"Range": "bytes=-10"}) as r:
                body = await r.read()
                assert r.status == 206
                assert r.headers["Content-Range"] == f"bytes {size-10}-{size-1}/{size}"
                assert len(body) == 10
    finally:
        await server.stop()


async def test_v4_5_cors_and_options(media_dir):
    vtt = str(media_dir / "srv.vtt")
    convert_sidecar_to_vtt(str(media_dir / "sample.srt"), vtt)
    server = await _make_server(media_dir, "compat.mp4", vtt=vtt)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{server.base_url}/video") as r:
                assert r.headers["Access-Control-Allow-Origin"] == "*"
                assert "Range" in r.headers["Access-Control-Allow-Headers"]
            async with s.options(f"{server.base_url}/video") as r:
                assert r.status == 204
                assert r.headers["Access-Control-Allow-Origin"] == "*"
            async with s.get(f"{server.base_url}/sub/0.vtt") as r:
                assert r.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        await server.stop()


async def test_v4_6_subtitle_vtt(media_dir):
    vtt = str(media_dir / "srv6.vtt")
    convert_sidecar_to_vtt(str(media_dir / "sample.srt"), vtt)
    server = await _make_server(media_dir, "compat.mp4", vtt=vtt)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{server.base_url}/sub/0.vtt") as r:
                body = await r.text()
                assert r.status == 200
                assert r.headers["Content-Type"].startswith("text/vtt")
                assert body.startswith("WEBVTT")
    finally:
        await server.stop()


async def _fetch(url, dst):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            with open(dst, "wb") as fh:
                async for chunk in r.content.iter_chunked(65536):
                    fh.write(chunk)


def _ffprobe_file(path):
    import json
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-print_format", "json", path],
        capture_output=True, text=True, timeout=30,
    )
    return json.loads(out.stdout) if out.returncode == 0 else {}


async def test_v4_7_pipe_mode_valid_fmp4(media_dir, tmp_path):
    # remux.mkv => ffmpeg_pipe to fragmented mp4. Capture the served stream over
    # HTTP (as the Chromecast would, linearly) and verify it is a valid fMP4.
    server = await _make_server(media_dir, "remux.mkv")
    assert server.plan.serve_mode == "ffmpeg_pipe"
    try:
        out = tmp_path / "pipe.mp4"
        await _fetch(f"{server.base_url}/video", out)
        data = _ffprobe_file(str(out))
        codecs = {s["codec_type"]: s["codec_name"] for s in data.get("streams", [])}
        assert codecs.get("video") == "h264"
        assert codecs.get("audio") == "aac"
        assert "mp4" in data.get("format", {}).get("format_name", "")
    finally:
        await server.stop()


async def test_v4_8_seek_restarts_ffmpeg(media_dir, tmp_path):
    server = await _make_server(media_dir, "remux.mkv")
    try:
        full_f, seek_f = tmp_path / "full.mp4", tmp_path / "seek.mp4"
        await _fetch(f"{server.base_url}/video", full_f)
        await _fetch(f"{server.base_url}/video?t=2.0", seek_f)
        full_dur = float(_ffprobe_file(str(full_f)).get("format", {}).get("duration", 0))
        seek_dur = float(_ffprobe_file(str(seek_f)).get("format", {}).get("duration", 0))
        # Seeking ~1.5s into a ~3s clip must yield a shorter remaining stream.
        assert full_dur > 2.0
        assert seek_dur < full_dur - 0.8
    finally:
        await server.stop()


def test_v4_9_lan_ip_detection():
    ip = detect_lan_ip()
    parts = ip.split(".")
    assert len(parts) == 4 and all(p.isdigit() for p in parts)
    if ip == "127.0.0.1":
        pytest.skip("no non-loopback interface available in this environment")
    assert not ip.startswith("127.")


async def test_v4_10_no_orphan_ffmpeg(media_dir):
    server = await _make_server(media_dir, "remux.mkv")
    async with aiohttp.ClientSession() as s:
        # Open the pipe, read a little, then drop the connection.
        async with s.get(f"{server.base_url}/video") as r:
            await r.content.read(1024)
    await asyncio.sleep(0.2)
    await server.stop()
    assert len(server._procs) == 0
