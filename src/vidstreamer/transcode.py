"""Build (and later run) ffmpeg commands for remux / transcode / burn-in."""

from __future__ import annotations

from .compat import StreamPlan
from .config import find_binary
from .probe import MediaInfo
from .subtitles import BurnIn

# Fragmented MP4 flags so the pipe is playable as it is produced.
FRAG_MP4_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"

_VIDEO_ENCODERS = {"h264": "libx264", "hevc": "libx265"}


def _escape_subs_path(path: str) -> str:
    # ffmpeg filtergraph escaping for the subtitles= filter.
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_ffmpeg_command(
    plan: StreamPlan,
    info: MediaInfo,
    *,
    burn_in: BurnIn | None = None,
    seek: float | None = None,
    ffmpeg_path: str | None = None,
) -> list[str]:
    """Construct the ffmpeg argv that streams the planned output to stdout (pipe:1).

    - Remux: ``-c:v copy -c:a copy`` into fragmented MP4 / WebM.
    - Transcode: re-encode video and/or audio.
    - Burn-in: overlay (image subs) or subtitles= (text subs); forces a video encode.
    - Seek: input ``-ss`` for fast restart-on-seek.
    """
    ffmpeg = ffmpeg_path or find_binary("ffmpeg")
    cmd: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error"]

    # Input-side seek (fast, keyframe-accurate enough for restart-on-seek).
    if seek and seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]
    cmd += ["-i", info.ffmpeg_input]

    video_filter: str | None = None
    filter_complex: str | None = None
    mapped_video = "0:v:0"

    if burn_in is not None:
        if burn_in.kind == "image":
            # Overlay the bitmap subtitle stream onto the video.
            filter_complex = f"[0:v:0][0:s:{burn_in.sub_index}]overlay[vout]"
            mapped_video = "[vout]"
        else:  # text burn-in
            path = burn_in.path or info.ffmpeg_input
            spec = f"subtitles='{_escape_subs_path(path)}'"
            if burn_in.sub_index is not None and burn_in.path is None:
                spec = (f"subtitles='{_escape_subs_path(info.ffmpeg_input)}'"
                        f":si={burn_in.sub_index}")
            video_filter = spec

    # --- Mapping ---
    if filter_complex:
        cmd += ["-filter_complex", filter_complex, "-map", mapped_video]
    else:
        cmd += ["-map", "0:v:0"]
        if video_filter:
            cmd += ["-vf", video_filter]
    cmd += ["-map", "0:a:0?"]

    # --- Video codec ---
    if plan.video_action == "transcode" or burn_in is not None:
        encoder = _VIDEO_ENCODERS.get(plan.video_codec or "h264", "libx264")
        cmd += ["-c:v", encoder, "-pix_fmt", "yuv420p", "-preset", "veryfast"]
        # Downscale only when no other -vf/-filter_complex is already in play
        # (burn-in filters take precedence; scaling alongside them is a later
        # enhancement, see SPEC §6.4 open items).
        if plan.max_height and not video_filter and not filter_complex:
            cmd += ["-vf", f"scale=-2:'min({plan.max_height},ih)'"]
    else:
        cmd += ["-c:v", "copy"]

    # --- Audio codec ---
    if plan.audio_action == "transcode":
        cmd += ["-c:a", "aac", "-ac", "2", "-b:a", "160k"]
    else:
        cmd += ["-c:a", "copy"]

    # --- Container / muxer to stdout ---
    if plan.container == "webm":
        cmd += ["-f", "webm"]
    else:
        cmd += ["-movflags", FRAG_MP4_FLAGS, "-f", "mp4"]
    cmd += ["pipe:1"]
    return cmd
