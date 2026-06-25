"""Chromecast Ultra capability matrix and the stream-planning decision engine.

Preference order: direct play > remux (container only) > transcode (re-encode).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .errors import UnsupportedMediaError
from .probe import MediaInfo

# Codecs the Chromecast Ultra can decode directly.
COMPATIBLE_VIDEO_CODECS = {"h264", "hevc", "h265", "vp8", "vp9"}
COMPATIBLE_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac"}

# Containers the Default Media Receiver accepts as-is.
DIRECT_CONTAINERS = {"mp4", "mov", "m4v", "webm"}
WEBM_VIDEO_CODECS = {"vp8", "vp9"}

H264_MAX_LEVEL = 51  # High@L5.1


@dataclass
class PlanOptions:
    force_transcode: bool = False
    no_transcode: bool = False
    video_codec: str | None = None     # h264 | hevc
    audio_codec: str | None = None     # aac | copy
    max_height: int | None = None
    burn_in: bool = False              # a selected subtitle must be burned in
    audio_track: int | None = None     # pick audio stream by 0:a:N (None = default)


@dataclass
class StreamPlan:
    video_action: str          # "copy" | "transcode"
    audio_action: str          # "copy" | "transcode"
    container: str             # "passthrough" | "mp4" | "webm"
    serve_mode: str            # "direct_range" | "ffmpeg_pipe"
    video_codec: str | None = None    # target codec when transcoding
    audio_codec: str | None = None    # target codec when transcoding
    max_height: int | None = None
    burn_in: bool = False
    audio_index: int = 0              # which audio stream to map (0:a:N)
    reason: str = ""

    def needs_ffmpeg(self) -> bool:
        return self.serve_mode == "ffmpeg_pipe"

    @property
    def content_type(self) -> str:
        if self.container == "webm":
            return "video/webm"
        return "video/mp4"

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"Plan: video={self.video_action}"
            + (f"->{self.video_codec}" if self.video_action == "transcode" else "")
            + f" audio={self.audio_action}"
            + (f"->{self.audio_codec}" if self.audio_action == "transcode" else "")
            + f" container={self.container} serve={self.serve_mode}"
            + (" burn-in" if self.burn_in else "")
            + (f"\nReason: {self.reason}" if self.reason else "")
        )


def _video_compatible(info: MediaInfo, opts: PlanOptions) -> bool:
    v = info.video
    if v is None:
        return True  # audio-only; nothing to decide for video
    codec = v.codec
    if codec not in COMPATIBLE_VIDEO_CODECS:
        return False
    if codec == "h264" and v.level is not None and v.level > H264_MAX_LEVEL:
        return False
    if opts.max_height and v.height and v.height > opts.max_height:
        return False
    return True


def _selected_audio(info: MediaInfo, audio_track: int | None):
    """Resolve the audio stream the user asked for, defaulting to the first."""
    if audio_track is not None:
        for a in info.audio_tracks:
            if a.audio_index == audio_track:
                return a
    return info.audio


def _audio_compatible(audio) -> bool:
    if audio is None:
        return True
    return audio.codec in COMPATIBLE_AUDIO_CODECS


def plan_stream(info: MediaInfo, opts: PlanOptions | None = None) -> StreamPlan:
    """Compute the minimal-work StreamPlan for a probed source."""
    opts = opts or PlanOptions()

    selected_audio = _selected_audio(info, opts.audio_track)
    audio_index = selected_audio.audio_index if selected_audio else 0
    # Selecting a non-default audio track means we must let ffmpeg pick the
    # stream (-map 0:a:N), which rules out direct file serving.
    non_default_audio = opts.audio_track is not None and audio_index != 0

    video_ok = _video_compatible(info, opts)
    audio_ok = _audio_compatible(selected_audio)

    want_video_transcode = opts.force_transcode or opts.burn_in or not video_ok
    want_audio_transcode = (opts.audio_codec == "aac") or not audio_ok

    if want_video_transcode and opts.no_transcode:
        raise UnsupportedMediaError(
            "video requires transcoding but --no-transcode was given"
        )

    reasons: list[str] = []

    # --- Video ---
    if want_video_transcode:
        video_action = "transcode"
        target_v = opts.video_codec or "h264"
        if opts.burn_in:
            reasons.append("subtitle burn-in forces video re-encode")
        elif opts.force_transcode:
            reasons.append("--force-transcode")
        else:
            reasons.append(
                f"video codec {info.video.codec if info.video else '?'} not supported"
            )
    else:
        video_action = "copy"
        target_v = None

    # --- Audio ---
    if want_audio_transcode:
        audio_action = "transcode"
        target_a = "aac"
        if not audio_ok:
            reasons.append(
                f"audio codec {selected_audio.codec if selected_audio else '?'} "
                "not supported"
            )
    else:
        audio_action = "copy"
        target_a = None

    # --- Container & serve mode ---
    src_container = info.container.lower()
    src_video_codec = (info.video.codec if info.video else "") or ""
    target_is_webm = (target_v in WEBM_VIDEO_CODECS) or (
        video_action == "copy" and src_video_codec in WEBM_VIDEO_CODECS
    )

    if video_action == "copy" and audio_action == "copy":
        if src_container in DIRECT_CONTAINERS:
            container = "passthrough"
            serve_mode = "direct_range"
            reasons.append("native container & codecs: direct play")
        else:
            container = "webm" if target_is_webm else "mp4"
            serve_mode = "ffmpeg_pipe"
            reasons.append(f"remux {src_container} -> {container} (no re-encode)")
    else:
        container = "webm" if target_is_webm else "mp4"
        serve_mode = "ffmpeg_pipe"

    # Remote sources can't be byte-range served from disk; proxy them through an
    # ffmpeg pipe (a stream copy when codecs are already compatible). This is how
    # we "stream and cast at the same time" (SPEC §6.1).
    if info.is_remote and serve_mode == "direct_range":
        serve_mode = "ffmpeg_pipe"
        container = "webm" if target_is_webm else "mp4"
        reasons.append("remote source proxied through local server")

    # A non-default audio track requires ffmpeg to map it, so we can't byte-range
    # serve the original file untouched.
    if non_default_audio and serve_mode == "direct_range":
        serve_mode = "ffmpeg_pipe"
        container = "webm" if target_is_webm else "mp4"
        reasons.append(f"audio track {audio_index} selected: remuxed via local server")

    return StreamPlan(
        video_action=video_action,
        audio_action=audio_action,
        container=container,
        serve_mode=serve_mode,
        video_codec=target_v,
        audio_codec=target_a,
        max_height=opts.max_height,
        burn_in=opts.burn_in,
        audio_index=audio_index,
        reason="; ".join(reasons),
    )
