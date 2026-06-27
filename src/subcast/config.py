"""Defaults, environment overrides, and logging setup."""

from __future__ import annotations

import logging
import os
import shutil

from .errors import DependencyError

ENV_PREFIX = "SUBCAST_"

DEFAULT_DISCOVERY_TIMEOUT = 8.0  # seconds
DEFAULT_PORT = 0  # 0 = ephemeral

REQUIRED_BINARIES = ("ffmpeg", "ffprobe")

log = logging.getLogger("subcast")


def env(name: str, default: str | None = None) -> str | None:
    """Read an env override, e.g. env('DEVICE') -> SUBCAST_DEVICE."""
    return os.environ.get(ENV_PREFIX + name, default)


def setup_logging(verbosity: int = 0) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


def find_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DependencyError(
            f"required binary '{name}' not found on PATH. "
            f"Install it with: sudo apt install ffmpeg"
        )
    return path


def check_dependencies() -> None:
    """Verify ffmpeg/ffprobe are available; raise DependencyError otherwise."""
    for binary in REQUIRED_BINARIES:
        find_binary(binary)
