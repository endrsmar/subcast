"""ffprobe wrapper producing a normalized MediaInfo."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field

from .config import find_binary, log
from .errors import SourceError
from .source import Source, resolve_source

# Subtitle codecs that carry text (convertible to WebVTT) vs. bitmaps (not).
TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text",
    "subviewer", "subviewer1", "microdvd", "eia_608", "stl",
}
IMAGE_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
    "dvb_subtitle", "dvbsub", "xsub",
}

# Probe limits for remote sources so we never download the whole file.
REMOTE_PROBESIZE = "5M"
REMOTE_ANALYZEDURATION = "5M"
REMOTE_TIMEOUT_S = 30


@dataclass
class VideoStream:
    index: int
    codec: str
    profile: str | None = None
    level: int | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    frame_rate: float | None = None
    is_hdr: bool = False


@dataclass
class AudioStream:
    index: int            # absolute stream index in the container
    codec: str
    channels: int | None = None
    sample_rate: int | None = None
    language: str | None = None
    audio_index: int = 0  # index among audio streams (0-based) -> ffmpeg 0:a:N
    title: str | None = None
    default: bool = False

    def label(self) -> str:
        """A short human label for pickers, e.g. 'eng · ac3 5.1 (Director)'."""
        ch = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(
            self.channels or 0, f"{self.channels}ch" if self.channels else "")
        parts = [self.language or "und", self.codec]
        if ch:
            parts.append(ch)
        base = " ".join(p for p in parts if p)
        if self.title:
            base += f" ({self.title})"
        return base


@dataclass
class SubtitleTrack:
    index: int            # absolute stream index in the container
    sub_index: int        # index among subtitle streams (0-based) -> ffmpeg 0:s:N
    codec: str
    language: str | None = None
    title: str | None = None
    text_based: bool = False
    forced: bool = False
    default: bool = False


@dataclass
class MediaInfo:
    source: str
    is_remote: bool
    container: str
    ffmpeg_input: str = ""        # resolved input path/URL for ffmpeg
    duration: float | None = None
    video: VideoStream | None = None
    audio: AudioStream | None = None  # the first/default audio track (back-compat)
    audio_tracks: list[AudioStream] = field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d

    def summary(self) -> str:
        lines = [f"Source: {self.source} ({'remote' if self.is_remote else 'local'})"]
        lines.append(f"Container: {self.container}"
                     + (f"  duration={self.duration:.1f}s" if self.duration else ""))
        if self.video:
            v = self.video
            lines.append(
                f"Video: {v.codec} {v.profile or ''} {v.width}x{v.height} "
                f"{'HDR' if v.is_hdr else 'SDR'}".strip()
            )
        if self.audio_tracks:
            if len(self.audio_tracks) == 1:
                a = self.audio_tracks[0]
                lines.append(
                    f"Audio: {a.codec} {a.channels}ch {a.language or ''}".strip())
            else:
                lines.append("Audio:")
                for a in self.audio_tracks:
                    flag = " default" if a.default else ""
                    lines.append(f"  [{a.audio_index}] {a.label()}{flag}")
        elif self.audio:
            a = self.audio
            lines.append(f"Audio: {a.codec} {a.channels}ch {a.language or ''}".strip())
        if self.subtitle_tracks:
            lines.append("Subtitles:")
            for s in self.subtitle_tracks:
                kind = "text" if s.text_based else "image"
                flags = ",".join(f for f, on in
                                 (("default", s.default), ("forced", s.forced)) if on)
                lines.append(
                    f"  [{s.sub_index}] {s.codec} {s.language or '?'} ({kind})"
                    + (f" {flags}" if flags else "")
                )
        else:
            lines.append("Subtitles: none")
        return "\n".join(lines)


def _to_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_frac(val) -> float | None:
    if not val or val in ("0/0", "N/A"):
        return None
    try:
        if "/" in str(val):
            num, den = str(val).split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(val)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _is_hdr(stream: dict) -> bool:
    transfer = (stream.get("color_transfer") or "").lower()
    primaries = (stream.get("color_primaries") or "").lower()
    if transfer in ("smpte2084", "arib-std-b67"):  # PQ / HLG
        return True
    if primaries == "bt2020":
        return True
    return False


def run_ffprobe(src: Source) -> dict:
    ffprobe = find_binary("ffprobe")
    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams",
           "-print_format", "json"]
    if src.is_remote:
        cmd += ["-probesize", REMOTE_PROBESIZE,
                "-analyzeduration", REMOTE_ANALYZEDURATION]
    cmd.append(src.ffmpeg_input)
    log.debug("ffprobe: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=REMOTE_TIMEOUT_S if src.is_remote else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"timed out probing {src.raw}") from exc
    if proc.returncode != 0:
        raise SourceError(f"could not probe {src.raw}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SourceError(f"unexpected ffprobe output for {src.raw}") from exc


def _normalize_container(format_name: str, is_remote: bool) -> str:
    # ffprobe returns comma-joined demuxer names, e.g. "mov,mp4,m4a,3gp,3g2,mj2".
    names = format_name.split(",")
    first = names[0]
    # Common normalizations.
    if "matroska" in format_name or "webm" in format_name:
        return "webm" if "webm" in format_name and "matroska" not in names else "matroska"
    return first


def build_media_info(src: Source, raw: dict) -> MediaInfo:
    fmt = raw.get("format", {})
    container = _normalize_container(fmt.get("format_name", "unknown"), src.is_remote)
    duration = None
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") else None
    except (TypeError, ValueError):
        duration = None

    video: VideoStream | None = None
    audio: AudioStream | None = None
    audios: list[AudioStream] = []
    subs: list[SubtitleTrack] = []
    sub_counter = 0
    audio_counter = 0

    for st in raw.get("streams", []):
        kind = st.get("codec_type")
        if kind == "video" and video is None:
            # Skip cover-art / attached pictures.
            if st.get("disposition", {}).get("attached_pic"):
                continue
            video = VideoStream(
                index=st.get("index", 0),
                codec=(st.get("codec_name") or "unknown").lower(),
                profile=st.get("profile"),
                level=_to_int(st.get("level")),
                width=_to_int(st.get("width")),
                height=_to_int(st.get("height")),
                pix_fmt=st.get("pix_fmt"),
                frame_rate=_parse_frac(st.get("avg_frame_rate")),
                is_hdr=_is_hdr(st),
            )
        elif kind == "audio":
            tags = st.get("tags", {})
            disp = st.get("disposition", {})
            track = AudioStream(
                index=st.get("index", 0),
                codec=(st.get("codec_name") or "unknown").lower(),
                channels=_to_int(st.get("channels")),
                sample_rate=_to_int(st.get("sample_rate")),
                language=tags.get("language"),
                audio_index=audio_counter,
                title=tags.get("title"),
                default=bool(disp.get("default")),
            )
            audios.append(track)
            if audio is None:
                audio = track
            audio_counter += 1
        elif kind == "subtitle":
            codec = (st.get("codec_name") or "unknown").lower()
            tags = st.get("tags", {})
            disp = st.get("disposition", {})
            subs.append(SubtitleTrack(
                index=st.get("index", 0),
                sub_index=sub_counter,
                codec=codec,
                language=tags.get("language"),
                title=tags.get("title"),
                text_based=codec in TEXT_SUBTITLE_CODECS,
                forced=bool(disp.get("forced")),
                default=bool(disp.get("default")),
            ))
            sub_counter += 1

    return MediaInfo(
        source=src.raw,
        is_remote=src.is_remote,
        container=container,
        ffmpeg_input=src.ffmpeg_input,
        duration=duration,
        video=video,
        audio=audio,
        audio_tracks=audios,
        subtitle_tracks=subs,
    )


def probe_source(source: str) -> MediaInfo:
    """Resolve and probe a source string, returning a MediaInfo."""
    src = resolve_source(source)
    raw = run_ffprobe(src)
    return build_media_info(src, raw)
