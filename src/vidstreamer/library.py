"""Media library: a recursive scan of the media root into a flat list of videos.

The scan is intentionally cheap (a bounded ``os.walk`` plus a small filename
parser) so the web UI can pull the whole library in one request and then do all
filtering, grouping and sorting client-side — keeping the sidebar snappy and
fully offline once loaded.

A tiny in-process cache keyed by ``media_root`` avoids re-walking the tree on
every poll; pass ``refresh=True`` to invalidate it (the UI's refresh button).

Stdlib only.
"""

from __future__ import annotations

import os
import re

from . import artwork
from .webapp import VIDEO_EXTS

# Walk bounds — defensive caps so a pathological tree can't hang the UI.
MAX_FILES = 50_000
MAX_DEPTH = 10

# Dependency / build / VCS directories that never hold real media but can
# contain huge numbers of files. Notably ``node_modules`` is full of TypeScript
# ``*.d.ts`` files, which match the ``.ts`` (MPEG-TS) video extension and would
# otherwise show up as bogus "videos". Pruned from the walk by name.
_SKIP_DIRS = {
    "node_modules", "bower_components", "build", "dist", "out", "target",
    "__pycache__", "venv", "env", "site-packages", ".git", ".svn", ".hg",
    ".tox", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

# Common release/junk tokens stripped from display titles (resolution, codec,
# source, container-y bits). Matched case-insensitively as whole words.
_RELEASE_TOKENS = {
    "480p", "576p", "720p", "1080p", "1440p", "2160p", "4k", "8k", "uhd",
    "hd", "sd", "hdr", "hdr10", "dolby", "dovi", "sdr",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "av1",
    "aac", "ac3", "eac3", "dts", "dd5", "ddp", "flac", "mp3", "opus",
    "bluray", "blu-ray", "brrip", "bdrip", "webrip", "web-dl", "webdl", "web",
    "hdrip", "dvdrip", "dvd", "hdtv", "pdtv", "cam", "ts", "remux", "proper",
    "repack", "extended", "unrated", "internal", "limited", "dl",
    "5", "1", "atmos", "truehd", "10bit", "8bit",
}

# Episode markers. Each yields (season, episode) via named groups s/e.
# Boundaries use ``[^a-z0-9]`` lookarounds (not ``\b``) so separators like
# ``_`` and ``.`` — which are word characters — still count as boundaries.
_B0 = r"(?<![a-z0-9])"
_B1 = r"(?![a-z0-9])"
_EPISODE_PATTERNS = [
    # S01E02 / s01.e02 / S1E2
    re.compile(rf"(?i){_B0}s(?P<s>\d{{1,2}})[\s._-]*e(?P<e>\d{{1,3}}){_B1}"),
    # 1x02 / 01x02
    re.compile(rf"(?i){_B0}(?P<s>\d{{1,2}})x(?P<e>\d{{1,3}}){_B1}"),
    # Season 1 Episode 2
    re.compile(
        rf"(?i){_B0}season[\s._-]*(?P<s>\d{{1,2}})[\s._-]*"
        rf"episode[\s._-]*(?P<e>\d{{1,3}}){_B1}"
    ),
]


def _clean_text(text: str) -> str:
    """Turn a raw filename fragment into a readable, title-cased-ish string."""
    text = text.replace(".", " ").replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_release_tokens(text: str) -> str:
    """Drop release noise (resolution/codec/source) words from a title.

    May return an empty string when every word is junk; callers that need a
    guaranteed-non-empty label apply their own fallback.
    """
    words = text.split()
    kept = [w for w in words if w.lower().strip("[]()") not in _RELEASE_TOKENS]
    return " ".join(kept).strip()


def _title_case(text: str) -> str:
    """Light title-casing that leaves all-caps acronyms and numbers alone."""
    out = []
    for w in text.split():
        if w.isupper() or any(ch.isdigit() for ch in w):
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def parse_episode(filename: str) -> dict | None:
    """Parse series/season/episode from a filename.

    Recognizes ``S01E02``, ``s01e02``, ``1x02`` and ``Season 1 Episode 2``.
    Returns ``{"series", "season", "episode", "ep_title"}`` with a cleaned show
    name (text before the marker) and a cleaned episode title (text after it,
    often empty), or ``None`` when no episode marker is present.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    for pat in _EPISODE_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        season = int(m.group("s"))
        episode = int(m.group("e"))
        show = _title_case(_strip_release_tokens(_clean_text(stem[: m.start()])))
        ep_title = _title_case(_strip_release_tokens(_clean_text(stem[m.end():])))
        return {
            "series": show or None, "season": season,
            "episode": episode, "ep_title": ep_title,
        }
    return None


def clean_title(filename: str) -> str:
    """Cleaned display title for a video file (extension + release noise off)."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    cleaned = _clean_text(stem)
    return _title_case(_strip_release_tokens(cleaned)) or _title_case(cleaned) or stem


def _make_item(path: str) -> dict:
    name = os.path.basename(path)
    try:
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    ep = parse_episode(name)
    title = clean_title(name)
    item = {
        "path": path,
        "name": name,
        "title": title,
        "dir": os.path.basename(os.path.dirname(path)),
        "size": size,
        "mtime": mtime,
        "series": None,
        "season": None,
        "episode": None,
        "ep_title": None,
    }
    if ep:
        item["series"] = ep["series"]
        item["season"] = ep["season"]
        item["episode"] = ep["episode"]
        item["ep_title"] = ep["ep_title"] or None
    # Stable poster descriptor (key/query/kind) so the UI can request real
    # artwork; ``None`` when no usable title remains (the UI keeps its gradient).
    item["art"] = artwork.describe(title, item["series"])
    return item


def _walk(media_root: str) -> list[dict]:
    items: list[dict] = []
    root = os.path.abspath(media_root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden + dependency/build directories in-place so os.walk doesn't
        # descend them.
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
        )
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() not in VIDEO_EXTS:
                continue
            items.append(_make_item(os.path.join(dirpath, fn)))
            if len(items) >= MAX_FILES:
                return items
    return items


# Tiny in-process cache: {media_root: items}. Invalidated per-root on refresh.
_cache: dict[str, list[dict]] = {}


def scan_library(media_root: str, refresh: bool = False) -> list[dict]:
    """Recursively scan ``media_root`` for video files.

    Returns a list of dicts (see ``_make_item``). Hidden directories are
    skipped; the walk is bounded by ``MAX_FILES`` and ``MAX_DEPTH``. Results are
    cached per ``media_root``; pass ``refresh=True`` to force a re-walk.
    """
    if not media_root or not os.path.isdir(media_root):
        return []
    key = os.path.abspath(media_root)
    if refresh:
        _cache.pop(key, None)
    if key in _cache:
        return _cache[key]
    items = _walk(media_root)
    _cache[key] = items
    return items
