"""V1 — media probing."""

from __future__ import annotations

import pytest

from subcast.errors import SourceError
from subcast.probe import build_media_info, probe_source
from subcast.source import Source

pytestmark = pytest.mark.usefixtures("media_dir")


def test_v1_1_compat_mp4(media_dir):
    info = probe_source(str(media_dir / "compat.mp4"))
    assert info.container in ("mp4", "mov")
    assert info.video.codec == "h264"
    assert info.audio.codec == "aac"
    assert info.video.width == 320 and info.video.height == 240
    assert info.subtitle_tracks == []


def test_v1_2_remux_mkv(media_dir):
    info = probe_source(str(media_dir / "remux.mkv"))
    assert info.container == "matroska"
    assert info.video.codec == "h264"
    assert info.audio.codec == "aac"


def test_v1_3_embedded_text_track(media_dir):
    info = probe_source(str(media_dir / "embedded_text.mkv"))
    assert len(info.subtitle_tracks) >= 1
    track = info.subtitle_tracks[0]
    assert track.language == "eng"
    assert track.text_based is True


def test_v1_4_embedded_image_track(image_probe_raw):
    # Synthetic stand-in (see conftest) run through the real parser, since this
    # ffmpeg build cannot encode a bitmap subtitle from text.
    src = Source(raw="image.mkv", is_remote=False, ffmpeg_input="image.mkv")
    info = build_media_info(src, image_probe_raw)
    assert len(info.subtitle_tracks) >= 1
    track = info.subtitle_tracks[0]
    assert track.text_based is False
    assert track.codec == "dvd_subtitle"
    assert track.language == "eng"


def test_v1_5_remote_source(http_media_server, media_dir):
    url = f"{http_media_server}/compat.mp4"
    info = probe_source(url)
    assert info.is_remote is True
    assert info.video.codec == "h264"
    assert info.audio.codec == "aac"


def test_v1_6_missing_local_source_exits_3():
    with pytest.raises(SourceError) as exc:
        probe_source("/does/not/exist.mp4")
    assert exc.value.exit_code == 3


def test_v1_6_unreachable_url_exits_3():
    with pytest.raises(SourceError) as exc:
        probe_source("http://127.0.0.1:1/missing.mp4")
    assert exc.value.exit_code == 3
