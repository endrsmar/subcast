"""Typed exceptions with stable exit codes (see SPEC.md §8)."""

from __future__ import annotations


class SubcastError(Exception):
    """Base error. ``exit_code`` maps to the process exit status."""

    exit_code: int = 1


class UsageError(SubcastError):
    exit_code = 2


class SourceError(SubcastError):
    """Source file/URL not found or unreachable."""

    exit_code = 3


class DeviceError(SubcastError):
    """No Chromecast found, or the requested device is unreachable."""

    exit_code = 4


class DependencyError(SubcastError):
    """A required system binary (ffmpeg/ffprobe) is missing."""

    exit_code = 5


class UnsupportedMediaError(SubcastError):
    """Media cannot be played and transcoding was refused (--no-transcode)."""

    exit_code = 6


class SubSearchError(SubcastError):
    """Subtitle search or download failed (network, config, or remote API)."""

    exit_code = 7
