"""V5 — discovery & control (FakeChromecast, no hardware)."""

from __future__ import annotations

import pytest

from vidstreamer.app import CastOptions, prepare_session
from vidstreamer.caster import Caster
from vidstreamer.discovery import select_device
from vidstreamer.errors import DeviceError
from vidstreamer.netutil import detect_lan_ip

from fakes import FakeChromecast, fake_device

pytestmark = pytest.mark.usefixtures("media_dir")


def _discoverer(*devices):
    def _fn(timeout=8.0):
        return list(devices)
    return _fn


async def test_v5_1_direct_play_load_request(media_dir):
    cc = FakeChromecast()
    dev = fake_device(cc)
    opts = CastOptions(non_interactive=True, bind_ip="127.0.0.1")
    session = await prepare_session(
        str(media_dir / "compat.mp4"), opts, discover_fn=_discoverer(dev)
    )
    try:
        calls = cc.media_controller.play_media_calls
        assert len(calls) == 1
        call = calls[0]
        assert call["content_type"] == "video/mp4"
        assert call["url"].startswith("http://127.0.0.1:")
        assert call["url"].endswith("/video")
        assert call["stream_type"] == "BUFFERED"
        assert session.plan.serve_mode == "direct_range"
    finally:
        await session.close()


async def test_v5_2_sidecar_subtitles_in_load(media_dir):
    cc = FakeChromecast()
    opts = CastOptions(
        non_interactive=True, bind_ip="127.0.0.1",
        subtitle_path=str(media_dir / "sample.srt"), sub_lang="eng",
    )
    session = await prepare_session(
        str(media_dir / "compat.mp4"), opts, discover_fn=_discoverer(fake_device(cc))
    )
    try:
        call = cc.media_controller.play_media_calls[0]
        assert call["subtitles"].endswith(".vtt")
        assert call["subtitles_mime"] == "text/vtt"
        assert cc.media_controller.enabled_subtitles == [1]
    finally:
        await session.close()


async def test_v5_3_pipe_mode_buffered_and_seek_reloads(media_dir):
    cc = FakeChromecast()
    opts = CastOptions(non_interactive=True, bind_ip="127.0.0.1")
    session = await prepare_session(
        str(media_dir / "remux.mkv"), opts, discover_fn=_discoverer(fake_device(cc))
    )
    try:
        assert session.plan.serve_mode == "ffmpeg_pipe"
        call = cc.media_controller.play_media_calls[0]
        assert call["stream_type"] == "BUFFERED"
        # A seek issues a controller seek (restart-on-seek wiring lives in server).
        session.caster.seek(2.0)
        assert ("seek", 2.0) in cc.media_controller.control_calls
    finally:
        await session.close()


def test_v5_4_devices_listing_and_empty():
    cc = FakeChromecast(name="Bedroom", model="Chromecast Ultra", host="10.0.0.9")
    dev = fake_device(cc)
    assert dev.as_dict() == {
        "name": "Bedroom", "model": "Chromecast Ultra",
        "host": "10.0.0.9", "uuid": "uuid-1234",
    }
    # Empty discovery -> selection error (exit 4) handled at CLI as "none found".
    with pytest.raises(DeviceError):
        select_device([], None)


def test_v5_5_device_selection_by_name():
    a = fake_device(FakeChromecast(name="Living Room", host="1.1.1.1"))
    b = fake_device(FakeChromecast(name="Bedroom", host="2.2.2.2", uuid="u2"))
    assert select_device([a, b], "Bedroom").host == "2.2.2.2"
    assert select_device([a, b], "1.1.1.1").name == "Living Room"
    with pytest.raises(DeviceError) as exc:
        select_device([a, b], "Kitchen")
    assert exc.value.exit_code == 4


def test_v5_6_controls_map_to_calls():
    cc = FakeChromecast()
    caster = Caster(fake_device(cc))
    caster.pause()
    caster.resume()
    caster.seek(12.5)
    caster.set_volume(0.3)
    caster.stop()
    calls = cc.media_controller.control_calls
    assert ("pause",) in calls
    assert ("play",) in calls
    assert ("seek", 12.5) in calls
    assert ("stop",) in calls
    assert cc.volume == 0.3
    assert cc.quit_called is True
