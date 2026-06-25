"""V6 — end-to-end (full app + real server + FakeChromecast)."""

from __future__ import annotations

import json
import os
import subprocess

import aiohttp
import pytest

from vidstreamer.app import CastOptions, prepare_session

from fakes import FakeChromecast, fake_device

pytestmark = pytest.mark.usefixtures("media_dir")

HW = os.environ.get("VIDSTREAMER_TEST_DEVICE")
requires_hw = pytest.mark.skipif(not HW, reason="set VIDSTREAMER_TEST_DEVICE to run")


def _discoverer(dev):
    def _fn(timeout=8.0):
        return [dev]
    return _fn


async def _fetch(url, dst):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            assert r.status in (200, 206)
            with open(dst, "wb") as fh:
                async for chunk in r.content.iter_chunked(65536):
                    fh.write(chunk)


def _ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-print_format", "json", path],
        capture_output=True, text=True, timeout=30,
    )
    return json.loads(out.stdout) if out.returncode == 0 else {}


def _codecs(data):
    return {s["codec_type"]: s["codec_name"] for s in data.get("streams", [])}


async def _session(source, media_dir, **optkw):
    cc = FakeChromecast()
    opts = CastOptions(non_interactive=True, bind_ip="127.0.0.1", **optkw)
    session = await prepare_session(source, opts, discover_fn=_discoverer(fake_device(cc)))
    return cc, session


async def test_v6_1_direct_play_e2e(media_dir, tmp_path):
    cc, session = await _session(str(media_dir / "compat.mp4"), media_dir)
    try:
        url = cc.media_controller.play_media_calls[0]["url"]
        out = tmp_path / "d.mp4"
        await _fetch(url, out)
        assert _codecs(_ffprobe(str(out))).get("video") == "h264"
    finally:
        await session.close()


async def test_v6_2_remux_mkv_e2e(media_dir, tmp_path):
    cc, session = await _session(str(media_dir / "remux.mkv"), media_dir)
    try:
        assert session.plan.serve_mode == "ffmpeg_pipe"
        out = tmp_path / "r.mp4"
        await _fetch(cc.media_controller.play_media_calls[0]["url"], out)
        c = _codecs(_ffprobe(str(out)))
        assert c.get("video") == "h264" and c.get("audio") == "aac"
    finally:
        await session.close()


async def test_v6_3_transcode_e2e(media_dir, tmp_path):
    cc, session = await _session(str(media_dir / "transcode.mp4"), media_dir)
    try:
        assert session.plan.video_action == "transcode"
        out = tmp_path / "t.mp4"
        await _fetch(cc.media_controller.play_media_calls[0]["url"], out)
        c = _codecs(_ffprobe(str(out)))
        assert c.get("video") == "h264"   # mpeg4 -> h264
        assert c.get("audio") == "aac"
    finally:
        await session.close()


async def test_v6_4_sidecar_subtitle_e2e(media_dir, tmp_path):
    cc, session = await _session(
        str(media_dir / "compat.mp4"), media_dir,
        subtitle_path=str(media_dir / "sample.srt"), sub_lang="eng",
    )
    try:
        sub_url = cc.media_controller.play_media_calls[0]["subtitles"]
        assert sub_url and sub_url.endswith(".vtt")
        out = tmp_path / "s.vtt"
        await _fetch(sub_url, out)
        assert out.read_text(encoding="utf-8").startswith("WEBVTT")
        assert cc.media_controller.enabled_subtitles == [1]
    finally:
        await session.close()


async def test_v6_5_embedded_text_subtitle_e2e(media_dir, tmp_path):
    cc, session = await _session(
        str(media_dir / "embedded_text.mkv"), media_dir,
        sub_track="0",
    )
    try:
        sub_url = cc.media_controller.play_media_calls[0]["subtitles"]
        assert sub_url and sub_url.endswith(".vtt")
        out = tmp_path / "es.vtt"
        await _fetch(sub_url, out)
        text = out.read_text(encoding="utf-8")
        assert text.startswith("WEBVTT")
        assert "vidstreamer" in text
    finally:
        await session.close()


async def test_start_offset_direct_uses_current_time(media_dir):
    # Direct (Range-seekable) play: offset is handed to the device as current_time,
    # and the served URL carries no ?t= (the device seeks via Range).
    cc, session = await _session(str(media_dir / "compat.mp4"), media_dir, start=90.0)
    try:
        assert session.plan.serve_mode == "direct_range"
        call = cc.media_controller.play_media_calls[0]
        assert call["current_time"] == 90.0
        assert "?t=" not in call["url"]
    finally:
        await session.close()


async def test_start_offset_pipe_reorigins_stream(media_dir):
    # ffmpeg pipe is non-seekable: offset is encoded in the URL (?t=) so ffmpeg
    # restarts with -ss, and the device timeline starts at 0.
    cc, session = await _session(str(media_dir / "remux.mkv"), media_dir, start=90.0)
    try:
        assert session.plan.serve_mode == "ffmpeg_pipe"
        call = cc.media_controller.play_media_calls[0]
        assert call["current_time"] == 0.0
        assert "t=90.000" in call["url"]
    finally:
        await session.close()


async def test_v6_6_remote_source_e2e(http_media_server, media_dir, tmp_path):
    url = f"{http_media_server}/compat.mp4"
    cc, session = await _session(url, media_dir)
    try:
        assert session.info.is_remote is True
        # Proxied through the local server (not the remote URL directly).
        play_url = cc.media_controller.play_media_calls[0]["url"]
        assert play_url.startswith("http://127.0.0.1:")
        out = tmp_path / "rem.mp4"
        await _fetch(play_url, out)
        assert _codecs(_ffprobe(str(out))).get("video") == "h264"
    finally:
        await session.close()


async def test_v6_7_cleanup_no_orphans(media_dir):
    cc, session = await _session(str(media_dir / "remux.mkv"), media_dir)
    workdir = session.workdir
    # touch the pipe so an ffmpeg gets spawned
    async with aiohttp.ClientSession() as s:
        async with s.get(session.server.handles.video_url) as r:
            await r.content.read(2048)
    await session.close()
    assert len(session.server._procs) == 0
    assert not os.path.exists(workdir)  # workdir cleaned


# --- Hardware / manual acceptance (gated) -------------------------------- #

@requires_hw
def test_v6_8_real_cast_plays():
    pytest.skip("manual: verify compat.mp4 plays on the TV and controls work")


@requires_hw
def test_v6_9_real_embedded_subs():
    pytest.skip("manual: verify embedded MKV subtitles show on the TV")


@requires_hw
def test_v6_10_real_sidecar_subs():
    pytest.skip("manual: verify sidecar .srt subtitles show on the TV")


@requires_hw
def test_v6_11_real_web_url():
    pytest.skip("manual: verify a web URL streams and plays with subtitles")


@requires_hw
def test_v6_12_real_seek_sync():
    pytest.skip("manual: verify seeking resumes near target with A/V in sync")
