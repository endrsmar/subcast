"""V2 — compatibility decision engine."""

from __future__ import annotations

import pytest

from subcast.compat import PlanOptions, plan_stream
from subcast.errors import UnsupportedMediaError
from subcast.probe import AudioStream, MediaInfo, VideoStream, build_media_info
from subcast.source import Source


def mi(container, vcodec="h264", vlevel=40, acodec="aac", **kw):
    return MediaInfo(
        source="x", is_remote=False, container=container,
        video=VideoStream(index=0, codec=vcodec, level=vlevel,
                          width=kw.get("w", 320), height=kw.get("h", 240)),
        audio=AudioStream(index=1, codec=acodec, channels=2),
    )


def test_v2_1_compat_mp4_direct():
    plan = plan_stream(mi("mp4"))
    assert plan.video_action == "copy"
    assert plan.audio_action == "copy"
    assert plan.serve_mode == "direct_range"
    assert plan.container == "passthrough"


def test_v2_2_remux_mkv():
    plan = plan_stream(mi("matroska"))
    assert plan.video_action == "copy"
    assert plan.audio_action == "copy"
    assert plan.container == "mp4"
    assert plan.serve_mode == "ffmpeg_pipe"


def test_v2_3_unsupported_video_transcodes():
    plan = plan_stream(mi("mp4", vcodec="mpeg4"))
    assert plan.video_action == "transcode"
    assert plan.video_codec == "h264"
    assert plan.serve_mode == "ffmpeg_pipe"


def test_v2_4_audio_only_transcode_keeps_video_copy():
    plan = plan_stream(mi("matroska", acodec="ac3"))
    assert plan.video_action == "copy"
    assert plan.audio_action == "transcode"
    assert plan.audio_codec == "aac"


def test_v2_5_force_transcode():
    plan = plan_stream(mi("mp4"), PlanOptions(force_transcode=True))
    assert plan.video_action == "transcode"


def test_v2_6_no_transcode_refuses_unsupported():
    with pytest.raises(UnsupportedMediaError) as exc:
        plan_stream(mi("mp4", vcodec="mpeg4"), PlanOptions(no_transcode=True))
    assert exc.value.exit_code == 6


def test_v2_7_burn_in_upgrades_to_transcode(image_probe_raw):
    src = Source(raw="x", is_remote=False, ffmpeg_input="x")
    info = build_media_info(src, image_probe_raw)
    # With burn-in requested, video must be re-encoded.
    plan = plan_stream(info, PlanOptions(burn_in=True))
    assert plan.video_action == "transcode"
    assert plan.burn_in is True
    # Without burn-in, the video decision is unchanged (copy here).
    plan2 = plan_stream(info, PlanOptions(burn_in=False))
    assert plan2.video_action == "copy"
    assert plan2.burn_in is False


def test_v2_vp9_webm_direct():
    plan = plan_stream(mi("webm", vcodec="vp9", acodec="opus"))
    assert plan.video_action == "copy"
    assert plan.serve_mode == "direct_range"


def test_v2_vp9_in_mkv_remux_to_webm():
    plan = plan_stream(mi("matroska", vcodec="vp9", acodec="opus"))
    assert plan.video_action == "copy"
    assert plan.container == "webm"
    assert plan.serve_mode == "ffmpeg_pipe"


def test_v2_h264_level_too_high_transcodes():
    plan = plan_stream(mi("mp4", vlevel=62))  # > 5.1
    assert plan.video_action == "transcode"


def _mi_multi_audio():
    """An mp4 that would direct-play, but with two audio tracks."""
    info = mi("mp4")
    info.audio_tracks = [
        AudioStream(index=1, codec="aac", channels=2, language="eng",
                    audio_index=0, default=True),
        AudioStream(index=2, codec="ac3", channels=6, language="ger",
                    audio_index=1),
    ]
    info.audio = info.audio_tracks[0]
    return info


def test_v2_audio_track_default_stays_direct():
    plan = plan_stream(_mi_multi_audio(), PlanOptions(audio_track=0))
    assert plan.serve_mode == "direct_range"
    assert plan.audio_index == 0


def test_v2_audio_track_nondefault_forces_pipe_and_maps_index():
    # Selecting the German ac3 track: must proxy via ffmpeg, transcode ac3->aac,
    # and map 0:a:1.
    plan = plan_stream(_mi_multi_audio(), PlanOptions(audio_track=1))
    assert plan.serve_mode == "ffmpeg_pipe"
    assert plan.audio_index == 1
    assert plan.audio_action == "transcode"  # ac3 not Chromecast-compatible
