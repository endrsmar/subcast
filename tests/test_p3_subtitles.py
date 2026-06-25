"""V3 — subtitle pipeline."""

from __future__ import annotations

import pytest

from vidstreamer.compat import PlanOptions, plan_stream
from vidstreamer.probe import build_media_info, probe_source
from vidstreamer.source import Source
from vidstreamer.subtitles import (
    convert_sidecar_to_vtt,
    plan_subtitles,
    prepare_subtitles,
    shift_vtt,
    srt_to_webvtt,
)
from vidstreamer.transcode import build_ffmpeg_command


def _is_valid_vtt(text: str) -> bool:
    return text.lstrip().startswith("WEBVTT")


def test_shift_vtt_offsets_and_drops_past_cues():
    vtt = ("WEBVTT\n\n"
           "00:09:58.000 --> 00:09:59.500\nskipped\n\n"
           "00:10:05.250 --> 00:10:08.000\nkept\n\n"
           "00:10:09.000 --> 00:10:10.000 align:start\nstyled\n")
    out = shift_vtt(vtt, 600)
    assert out.startswith("WEBVTT")
    assert "skipped" not in out                       # cue before offset dropped
    assert "00:00:05.250 --> 00:00:08.000" in out     # 605.25s - 600s
    assert "00:00:09.000 --> 00:00:10.000 align:start" in out  # settings preserved


def test_shift_vtt_noop_for_zero_offset():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
    assert shift_vtt(vtt, 0) == vtt


def test_shift_vtt_negative_offset_delays_cues():
    # A negative offset moves cues later (manual subtitle delay) and drops nothing.
    vtt = "WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nhi\n"
    out = shift_vtt(vtt, -3)
    assert "00:00:13.000 --> 00:00:15.000" in out
    assert "hi" in out


def test_v3_1_srt_to_webvtt_basic():
    srt = ("1\n00:00:00,500 --> 00:00:01,500\nHello\n\n"
           "2\n00:00:02,000 --> 00:00:02,800\nWorld\n")
    vtt = srt_to_webvtt(srt)
    assert _is_valid_vtt(vtt)
    assert "00:00:00.500 --> 00:00:01.500" in vtt  # comma -> dot
    assert "," not in vtt.split("\n")[2] if len(vtt.split("\n")) > 2 else True
    # numeric counters removed
    assert "\n1\n" not in vtt and not vtt.split("\n")[2].strip().isdigit()
    assert "Hello" in vtt and "World" in vtt


def test_v3_2_bom_handled(media_dir):
    dst = media_dir / "out_bom.vtt"
    convert_sidecar_to_vtt(str(media_dir / "sample_bom.srt"), str(dst))
    text = dst.read_text(encoding="utf-8")
    assert text.startswith("WEBVTT")
    assert "﻿" not in text  # no stray BOM


def test_v3_3_latin1_transcoded(media_dir):
    dst = media_dir / "out_latin1.vtt"
    convert_sidecar_to_vtt(str(media_dir / "sample_latin1.srt"), str(dst))
    text = dst.read_text(encoding="utf-8")  # must be valid utf-8
    assert text.startswith("WEBVTT")
    assert "Café crème" in text


def test_v3_4_embedded_text_extract(media_dir):
    info = probe_source(str(media_dir / "embedded_text.mkv"))
    plan = plan_subtitles(
        info, sidecar=None, sub_track="0", sub_lang=None,
        auto_subs=False, burn_subs=False, no_subs=False,
    )
    assert plan.mode == "embedded_text"
    workdir = str(media_dir / "wd_embed")
    import os
    os.makedirs(workdir, exist_ok=True)
    prepare_subtitles(plan, info, workdir, sidecar=None)
    text = open(plan.vtt_path, encoding="utf-8").read()
    assert text.startswith("WEBVTT")
    assert "vidstreamer" in text


def test_v3_5_image_track_warns_and_drops(image_probe_raw):
    src = Source(raw="x", is_remote=False, ffmpeg_input="x")
    info = build_media_info(src, image_probe_raw)
    plan = plan_subtitles(
        info, sidecar=None, sub_track="0", sub_lang=None,
        auto_subs=False, burn_subs=False, no_subs=False,
    )
    assert plan.mode == "none"
    assert plan.warnings and "image-based" in plan.warnings[0]


def test_v3_6_burn_in_command_reencodes(image_probe_raw):
    src = Source(raw="x.mkv", is_remote=False, ffmpeg_input="x.mkv")
    info = build_media_info(src, image_probe_raw)
    plan = plan_subtitles(
        info, sidecar=None, sub_track="0", sub_lang=None,
        auto_subs=False, burn_subs=True, no_subs=False,
    )
    assert plan.mode == "burn_in"
    stream_plan = plan_stream(info, PlanOptions(burn_in=True))
    cmd = build_ffmpeg_command(stream_plan, info, burn_in=plan.burn_in,
                               ffmpeg_path="ffmpeg")
    joined = " ".join(cmd)
    assert "overlay" in joined          # burn-in filter present
    assert "libx264" in joined          # video is re-encoded
    assert "-c:v copy" not in joined


def test_v3_6b_text_burn_in_uses_subtitles_filter(media_dir):
    info = probe_source(str(media_dir / "remux.mkv"))
    plan = plan_subtitles(
        info, sidecar=str(media_dir / "sample.srt"), sub_track=None,
        sub_lang="eng", auto_subs=False, burn_subs=True, no_subs=False,
    )
    assert plan.mode == "burn_in"
    stream_plan = plan_stream(info, PlanOptions(burn_in=True))
    cmd = build_ffmpeg_command(stream_plan, info, burn_in=plan.burn_in,
                               ffmpeg_path="ffmpeg")
    joined = " ".join(cmd)
    assert "subtitles=" in joined
    assert "libx264" in joined
