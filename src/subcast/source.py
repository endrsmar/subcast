"""Source resolution: classify local path vs web URL and validate it."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import SourceError

_REMOTE_SCHEMES = {"http", "https"}


@dataclass
class Source:
    raw: str
    is_remote: bool
    # The value to hand to ffmpeg/ffprobe as input (abs path for local files).
    ffmpeg_input: str

    @property
    def basename(self) -> str:
        if self.is_remote:
            path = urlparse(self.raw).path
            return os.path.basename(path) or "stream"
        return os.path.basename(self.ffmpeg_input)


def resolve_source(source: str) -> Source:
    """Classify and validate a source string.

    Raises SourceError (exit 3) if a local path does not exist.
    Remote reachability is validated lazily at probe/stream time.
    """
    if not source:
        raise SourceError("empty source")

    parsed = urlparse(source)
    if parsed.scheme in _REMOTE_SCHEMES:
        return Source(raw=source, is_remote=True, ffmpeg_input=source)

    # Treat anything else as a local filesystem path.
    abspath = os.path.abspath(os.path.expanduser(source))
    if not os.path.exists(abspath):
        raise SourceError(f"source not found: {source}")
    if not os.path.isfile(abspath):
        raise SourceError(f"source is not a file: {source}")
    return Source(raw=source, is_remote=False, ffmpeg_input=abspath)
