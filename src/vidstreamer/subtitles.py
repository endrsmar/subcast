"""Subtitle discovery, extraction, and conversion to WebVTT.

Chromecast only accepts side-loaded WebVTT text tracks. Text subtitles (SRT, ASS,
mov_text, embedded text) are converted; image subtitles (PGS/VOBSUB) cannot be and
must instead be burned into the video (handled by the transcode/plan layer).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from .config import find_binary, log
from .errors import SourceError, UsageError
from .probe import MediaInfo, SubtitleTrack

# Encodings tried in order when decoding a sidecar subtitle file.
_FALLBACK_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_TIMESTAMP_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}),(\d{3})"
)


@dataclass
class BurnIn:
    """A subtitle to be burned into the video during transcode."""

    kind: str            # "image" | "text"
    sub_index: int | None = None   # stream index among subs (0:s:N) for embedded
    path: str | None = None        # external file for text burn-in


@dataclass
class SubtitlePlan:
    mode: str                      # "sidecar_text" | "embedded_text" | "burn_in" | "none"
    vtt_path: str | None = None    # produced WebVTT, when mode is a text mode
    language: str = "und"
    label: str = "Subtitles"
    sub_index: int | None = None   # embedded stream index (0:s:N), when relevant
    burn_in: BurnIn | None = None
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Text -> WebVTT conversion
# --------------------------------------------------------------------------- #

def decode_subtitle_bytes(data: bytes) -> str:
    """Decode subtitle bytes to text, trying common encodings."""
    for enc in _FALLBACK_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Last resort: lossy latin-1 (never raises).
    return data.decode("latin-1", errors="replace")


def srt_to_webvtt(srt_text: str) -> str:
    """Convert SRT (or already-VTT) text to a valid WebVTT document."""
    text = srt_text.lstrip("﻿")  # strip any BOM that survived decoding
    if text.lstrip().startswith("WEBVTT"):
        return text if text.endswith("\n") else text + "\n"

    out_lines = ["WEBVTT", ""]
    # Normalize line endings, then walk blocks.
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        # Drop a leading numeric counter line (SRT cue index).
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        # Convert the timestamp line's comma decimals to dots.
        lines[0] = _TIMESTAMP_RE.sub(r"\1.\2 --> \3.\4", lines[0])
        out_lines.extend(lines)
        out_lines.append("")
    return "\n".join(out_lines) + "\n"


def _parse_vtt_time(tok: str) -> float:
    """Parse a WebVTT timestamp token (HH:MM:SS.mmm or MM:SS.mmm) to seconds."""
    parts = tok.replace(",", ".").split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        raise ValueError(f"bad VTT timestamp: {tok!r}")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _fmt_vtt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    t -= h * 3600
    m = int(t // 60)
    t -= m * 60
    return f"{h:02d}:{m:02d}:{t:06.3f}"


def shift_vtt(vtt_text: str, offset: float) -> str:
    """Subtract ``offset`` seconds from every cue, dropping cues that end before 0.

    A positive ``offset`` moves cues *earlier* (used when the ffmpeg-pipe stream is
    re-origined at a start offset: the device clock restarts at 0, so cue timings
    must move back to match). A negative ``offset`` moves cues *later* — this is how
    a manual subtitle delay is applied. Zero is a no-op.
    """
    if offset == 0:
        return vtt_text
    blocks = re.split(r"\r?\n\r?\n", vtt_text.strip())
    out_blocks: list[str] = []
    for i, block in enumerate(blocks):
        lines = block.split("\n")
        ts_idx = next((j for j, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:  # header or note block — keep as-is
            out_blocks.append(block)
            continue
        left, _, rest = lines[ts_idx].partition("-->")
        rest = rest.strip()
        end_tok = rest.split()[0] if rest else ""
        settings = rest[len(end_tok):].strip()
        try:
            start = _parse_vtt_time(left.strip()) - offset
            end = _parse_vtt_time(end_tok) - offset
        except ValueError:
            out_blocks.append(block)
            continue
        if end <= 0:  # cue lies entirely before the start point
            continue
        timing = f"{_fmt_vtt_time(start)} --> {_fmt_vtt_time(end)}"
        if settings:
            timing += " " + settings
        lines[ts_idx] = timing
        out_blocks.append("\n".join(lines))
    return "\n\n".join(out_blocks) + "\n"


def shift_vtt_file(path: str, offset: float) -> None:
    """In-place variant of :func:`shift_vtt`."""
    if offset == 0:
        return
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(shift_vtt(text, offset))


def convert_sidecar_to_vtt(src_path: str, dst_path: str) -> str:
    """Read a sidecar subtitle file and write a UTF-8 WebVTT to dst_path."""
    if not os.path.isfile(src_path):
        raise SourceError(f"subtitle file not found: {src_path}")
    with open(src_path, "rb") as fh:
        raw = fh.read()
    text = decode_subtitle_bytes(raw)
    vtt = srt_to_webvtt(text)
    with open(dst_path, "w", encoding="utf-8") as fh:
        fh.write(vtt)
    return dst_path


def extract_embedded_to_vtt(
    input_path: str, sub_index: int, dst_path: str, *, is_remote: bool = False
) -> str:
    """Extract an embedded *text* subtitle stream (0:s:N) to a WebVTT file.

    Note: ffmpeg must read the whole container to collect all subtitle packets
    (they are interleaved throughout). For a remote source this means downloading
    the entire file, so this can take a long time; ``is_remote`` adds reconnect /
    I/O-timeout flags so a stalled connection errors out instead of hanging
    forever.
    """
    ffmpeg = find_binary("ffmpeg")
    cmd = [ffmpeg, "-nostdin", "-y"]
    if is_remote:
        # Survive transient network drops; error (don't hang) on a dead socket.
        cmd += [
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "30000000",  # 30s with no I/O progress -> fail
        ]
    cmd += ["-i", input_path, "-map", f"0:s:{sub_index}", "-f", "webvtt", dst_path]
    log.debug("extract subs: %s", " ".join(cmd))
    if is_remote:
        log.warning("extracting embedded subtitles from a remote source reads the "
                    "entire file; this can take several minutes")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        hint = ""
        if is_remote:
            hint = (" (remote extraction is slow/fragile; prefer a sidecar file via "
                    "-s <subs.srt>, or --no-subs)")
        raise SourceError(
            f"failed to extract subtitle track {sub_index}: "
            f"{proc.stderr.strip()[-500:]}{hint}"
        )
    return dst_path


# --------------------------------------------------------------------------- #
# Track selection
# --------------------------------------------------------------------------- #

def select_embedded_track(
    info: MediaInfo, selector: str | None, lang: str | None, auto: bool
) -> SubtitleTrack | None:
    """Choose an embedded subtitle track by index, language, or auto rules."""
    tracks = info.subtitle_tracks
    if not tracks:
        return None

    if selector is not None:
        if selector.isdigit():
            idx = int(selector)
            for t in tracks:
                if t.sub_index == idx:
                    return t
            raise UsageError(f"no embedded subtitle track with index {idx}")
        # treat as language code
        for t in tracks:
            if (t.language or "").lower().startswith(selector.lower()):
                return t
        raise UsageError(f"no embedded subtitle track for language '{selector}'")

    if lang:
        for t in tracks:
            if (t.language or "").lower().startswith(lang.lower()):
                return t

    if auto:
        # Prefer default, then forced, then the first track.
        for t in tracks:
            if t.default:
                return t
        for t in tracks:
            if t.forced:
                return t
        return tracks[0]

    return None


def plan_subtitles(
    info: MediaInfo,
    *,
    sidecar: str | None,
    sub_track: str | None,
    sub_lang: str | None,
    auto_subs: bool,
    burn_subs: bool,
    no_subs: bool,
) -> SubtitlePlan:
    """Decide how subtitles will be delivered. Does no I/O (no file is written)."""
    if no_subs:
        return SubtitlePlan(mode="none")

    # Sidecar file wins if explicitly provided.
    if sidecar:
        if burn_subs:
            return SubtitlePlan(
                mode="burn_in",
                language=sub_lang or "und",
                burn_in=BurnIn(kind="text", path=sidecar),
            )
        return SubtitlePlan(
            mode="sidecar_text", language=sub_lang or "und",
            label="Subtitles", burn_in=None,
            warnings=(),
            vtt_path=None,  # produced later by prepare_subtitles
        )

    track = select_embedded_track(info, sub_track, sub_lang, auto_subs)
    if track is None:
        return SubtitlePlan(mode="none")

    lang = track.language or "und"
    if track.text_based:
        if burn_subs:
            return SubtitlePlan(
                mode="burn_in", language=lang,
                burn_in=BurnIn(kind="text", sub_index=track.sub_index),
            )
        return SubtitlePlan(mode="embedded_text", language=lang,
                            sub_index=track.sub_index)

    # Image-based track.
    if burn_subs:
        return SubtitlePlan(
            mode="burn_in", language=lang,
            burn_in=BurnIn(kind="image", sub_index=track.sub_index),
        )
    return SubtitlePlan(
        mode="none",
        warnings=(
            f"subtitle track {track.sub_index} ({track.codec}) is image-based and "
            f"cannot be converted to WebVTT; pass --burn-subs to hardcode it.",
        ),
    )


def prepare_subtitles(plan: SubtitlePlan, info: MediaInfo, workdir: str,
                      sidecar: str | None) -> SubtitlePlan:
    """Materialize the WebVTT file for a text subtitle plan; return updated plan."""
    if plan.mode == "sidecar_text":
        dst = os.path.join(workdir, "sub.vtt")
        convert_sidecar_to_vtt(sidecar, dst)
        plan.vtt_path = dst
    elif plan.mode == "embedded_text":
        dst = os.path.join(workdir, "sub.vtt")
        extract_embedded_to_vtt(info.ffmpeg_input, plan.sub_index or 0, dst,
                                is_remote=info.is_remote)
        plan.vtt_path = dst
    return plan
