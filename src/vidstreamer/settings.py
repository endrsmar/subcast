"""Persisted user settings (media root, subtitle language, OpenSubtitles key).

Settings live as JSON at ``$XDG_CONFIG_HOME/vidstreamer/settings.json`` (falling
back to ``~/.config``). Loading is defensive: a missing or corrupt file yields
defaults rather than raising, so the UI/CLI never crash on a bad config.

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import json
import locale
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .config import log

SETTINGS_FILENAME = "settings.json"
_APP_DIR = "vidstreamer"


def config_dir() -> Path:
    """Directory holding the settings file (honors $XDG_CONFIG_HOME)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / _APP_DIR


def settings_path() -> Path:
    return config_dir() / SETTINGS_FILENAME


def system_language() -> str:
    """Best-effort 2-letter ISO-639-1 language code for the current system.

    Tries ``locale.getlocale`` then ``getdefaultlocale`` then the LANG/LANGUAGE
    environment variables. Falls back to ``"en"`` when nothing usable is found.
    """
    candidates: list[str | None] = []
    try:  # getlocale is the non-deprecated path (3.11 deprecates getdefaultlocale)
        candidates.append(locale.getlocale()[0])
    except (ValueError, TypeError):
        pass
    try:
        candidates.append(locale.getdefaultlocale()[0])  # type: ignore[attr-defined]
    except (ValueError, TypeError, AttributeError):
        pass
    for env_var in ("LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        candidates.append(os.environ.get(env_var))

    for raw in candidates:
        if not raw:
            continue
        # Normalize forms like "en_US.UTF-8", "en-GB", "en:fr", "C".
        token = raw.replace("-", "_").split(":")[0].split(".")[0].strip()
        code = token.split("_")[0].lower()
        if len(code) == 2 and code.isalpha():
            return code
    return "en"


@dataclass
class Settings:
    media_root: str = ""
    preferred_sub_lang: str = ""
    opensubtitles_api_key: str = ""
    tmdb_api_key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _defaults() -> Settings:
    return Settings(
        media_root=os.getcwd(),
        preferred_sub_lang=system_language(),
        opensubtitles_api_key="",
        tmdb_api_key="",
    )


def _coerce(data: dict) -> Settings:
    """Build Settings from a dict, ignoring unknown keys and filling defaults."""
    base = _defaults()
    known = {f.name for f in fields(Settings)}
    for key, value in data.items():
        if key in known and isinstance(value, str):
            setattr(base, key, value)
    # Empty media_root/lang fall back to defaults so a blank persisted value is
    # never returned to callers.
    if not base.media_root:
        base.media_root = os.getcwd()
    if not base.preferred_sub_lang:
        base.preferred_sub_lang = system_language()
    return base


def load_settings() -> Settings:
    """Load settings, returning defaults for a missing or corrupt file."""
    path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return _defaults()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        log.warning("settings file is corrupt; using defaults: %s", path)
        return _defaults()
    if not isinstance(data, dict):
        return _defaults()
    return _coerce(data)


def save_settings(s: Settings) -> None:
    """Persist settings to disk, creating the config directory as needed."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s.to_dict(), indent=2) + "\n", encoding="utf-8")


def update_settings(**kwargs) -> Settings:
    """Load, apply the given fields, persist, and return the updated settings."""
    current = load_settings()
    known = {f.name for f in fields(Settings)}
    for key, value in kwargs.items():
        if key in known and value is not None:
            setattr(current, key, str(value))
    save_settings(current)
    return current
