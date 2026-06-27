"""V7 — live subtitle-offset control + device stop on teardown."""

from __future__ import annotations

import os

import aiohttp
import pytest

from subcast.app import CastOptions, prepare_session
from subcast.subtitles import shift_vtt
from subcast.webapp import UIState, _apply_sub_offset

from fakes import FakeChromecast, fake_device

pytestmark = pytest.mark.usefixtures("media_dir")


def _discoverer(dev):
    def _fn(timeout=8.0):
        return [dev]
    return _fn


async def _state(source, media_dir, **optkw):
    cc = FakeChromecast()
    opts = CastOptions(non_interactive=True, bind_ip="127.0.0.1", **optkw)
    session = await prepare_session(source, opts, discover_fn=_discoverer(fake_device(cc)))
    state = UIState()
    state.session = session
    state.title = "test"
    vtt = session.subtitle_plan.vtt_path
    state.orig_vtt = (
        open(vtt, encoding="utf-8").read() if vtt and os.path.isfile(vtt) else None
    )
    return cc, state, session


async def test_sub_offset_reloads_and_delays_cues(media_dir):
    # Direct play: applying a +2s offset rewrites the served VTT (cues 2s later)
    # and reloads the track with a cache-busted URL so the device re-fetches it.
    cc, state, session = await _state(
        str(media_dir / "compat.mp4"), media_dir,
        subtitle_path=str(media_dir / "sample.srt"), sub_lang="eng",
    )
    try:
        before = len(cc.media_controller.play_media_calls)
        await _apply_sub_offset(state, 2.0)
        calls = cc.media_controller.play_media_calls
        assert len(calls) == before + 1                  # reloaded on the device
        # Path-based cache-bust (not ?v=): a fresh /sub/<n>.vtt the receiver
        # can't have cached.
        assert calls[-1]["subtitles"].endswith("/sub/1.vtt")

        served = open(session.subtitle_plan.vtt_path, encoding="utf-8").read()
        assert served == shift_vtt(state.orig_vtt, -2.0)  # +2s delay == -2 shift
    finally:
        await session.close()


async def test_sub_offset_pipe_reorigins_with_offset(media_dir):
    # Pipe stream: a sub-offset change re-origins at the current position (?t=)
    # while the device clock restarts at 0.
    cc, state, session = await _state(
        str(media_dir / "remux.mkv"), media_dir,
        subtitle_path=str(media_dir / "sample.srt"), sub_lang="eng",
    )
    try:
        assert session.plan.serve_mode == "ffmpeg_pipe"
        await _apply_sub_offset(state, 1.5)
        call = cc.media_controller.play_media_calls[-1]
        assert "?t=" in call["url"]
        assert call["current_time"] == 0.0
        assert call["subtitles"].endswith("/sub/1.vtt")
    finally:
        await session.close()


async def test_busted_url_serves_shifted_vtt_over_http(media_dir):
    # What the device actually does: fetch the new /sub/<n>.vtt path and get the
    # *shifted* timing (not a cached copy of the original).
    cc, state, session = await _state(
        str(media_dir / "compat.mp4"), media_dir,
        subtitle_path=str(media_dir / "sample.srt"), sub_lang="eng",
    )
    try:
        await _apply_sub_offset(state, 2.0)
        sub_url = cc.media_controller.play_media_calls[-1]["subtitles"]
        assert "/sub/1.vtt" in sub_url
        async with aiohttp.ClientSession() as http:
            async with http.get(sub_url) as r:
                assert r.status == 200
                served = await r.text()
        assert served == shift_vtt(state.orig_vtt, -2.0)
        assert served != state.orig_vtt          # actually changed
    finally:
        await session.close()


async def test_sub_offset_clears_captions_before_reload(media_dir):
    # A cue on screen at reload time would stick; captions must be cleared on the
    # device before the track-swapping reload.
    cc, state, session = await _state(
        str(media_dir / "compat.mp4"), media_dir,
        subtitle_path=str(media_dir / "sample.srt"), sub_lang="eng",
    )
    try:
        await _apply_sub_offset(state, 1.0)
        calls = cc.media_controller.control_calls
        assert ("disable_subtitle",) in calls
        clear_at = calls.index(("disable_subtitle",))
        reenable_at = max(
            i for i, c in enumerate(calls) if c[0] == "enable_subtitle"
        )
        assert clear_at < reenable_at  # cleared, then re-enabled on the new track
    finally:
        await session.close()


async def test_close_stops_device(media_dir):
    # Teardown must halt the device, not just the local server, or the receiver
    # keeps draining its prebuffer for ~30s.
    cc, _state_obj, session = await _state(str(media_dir / "compat.mp4"), media_dir)
    await session.close()
    assert ("stop",) in cc.media_controller.control_calls
    assert cc.quit_called is True
