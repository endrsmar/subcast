"""Layered subtitle search: same-folder sidecar, media-root scan, online API.

Three tiers, cheapest first:

1. :func:`find_sidecar` — a subtitle next to the video sharing its stem.
2. :func:`search_media_root` — a fuzzy stem match anywhere under the media root.
3. :func:`search_online` / :func:`download_online` — the OpenSubtitles REST API.

Online access uses ``urllib.request`` (no third-party HTTP client) and requires
an API key configured in settings; without one, online search is disabled with a
clear message.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .config import log
from .errors import SubSearchError

# Subtitle extensions we recognize, in preference order (.srt then .vtt first).
SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")
_EXT_RANK = {ext: i for i, ext in enumerate(SUB_EXTS)}

USER_AGENT = f"subcast v{__version__}"
_OS_BASE = "https://api.opensubtitles.com/api/v1"
_HTTP_TIMEOUT = 15.0

# Guardrails for the recursive media-root walk.
_MAX_FILES_SCANNED = 20000
_MAX_DEPTH = 8


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _normalize(name: str) -> str:
    """Lowercase a basename and strip punctuation to bare alphanumeric tokens."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return name.strip()


def _tokens(name: str) -> set[str]:
    return {t for t in _normalize(name).split() if t}


def _similarity(a: set[str], b: set[str]) -> float:
    """Jaccard token overlap in [0, 1] between two filename token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_sub(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _EXT_RANK


def _lang_of(stem: str, video_stem: str) -> str:
    """Guess a language suffix: 'movie.en' relative to 'movie' -> 'en'."""
    if stem.lower().startswith(video_stem.lower()):
        suffix = stem[len(video_stem):].lstrip(".")
        part = suffix.split(".")[0].lower()
        if 2 <= len(part) <= 3 and part.isalpha():
            return part
    return ""


def _ext_rank(path: str) -> int:
    return _EXT_RANK.get(os.path.splitext(path)[1].lower(), len(SUB_EXTS))


# --------------------------------------------------------------------------- #
# Tier 1: same-folder sidecar
# --------------------------------------------------------------------------- #

def find_sidecar(video_path: str, preferred_lang: str | None = None) -> str | None:
    """Find a subtitle in the video's folder whose stem matches the video stem.

    Matches an exact stem (``movie.srt``) or a language-suffixed stem
    (``movie.en.srt``). Ranking: preferred-language variant first, then exact
    stem, then any matching stem; ties broken by extension preference
    (.srt > .vtt > ...). Returns an absolute path or ``None``.
    """
    video = Path(video_path)
    folder = video.parent
    video_stem = video.stem
    if not folder.is_dir():
        return None

    pref = (preferred_lang or "").lower()
    candidates: list[tuple[int, int, int, str]] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file() or not _is_sub(entry.name):
            continue
        stem = entry.stem  # e.g. "movie.en" for movie.en.srt
        # Require the subtitle stem to start with the video stem.
        if not stem.lower().startswith(video_stem.lower()):
            continue
        lang = _lang_of(stem, video_stem)
        exact = stem.lower() == video_stem.lower()
        # Lower sort key sorts first.
        lang_rank = 0 if (pref and lang == pref) else 1
        exact_rank = 0 if exact else 1
        candidates.append((lang_rank, exact_rank, _ext_rank(str(entry)),
                           str(entry.resolve())))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[:3])
    return candidates[0][3]


# --------------------------------------------------------------------------- #
# Tier 2: recursive media-root scan
# --------------------------------------------------------------------------- #

def search_media_root(
    video_path: str, media_root: str, preferred_lang: str | None = None
) -> list[dict]:
    """Scan ``media_root`` for subtitles whose basename matches the video's stem.

    Uses a normalized token match on the basename (punctuation stripped,
    lowercased). Hidden directories are skipped and the walk is bounded by file
    count and depth. Returns a ranked list of
    ``{"path", "name", "lang", "score"}`` (best score first).
    """
    root = Path(media_root)
    if not root.is_dir():
        return []
    video_stem = Path(video_path).stem
    want = _tokens(video_stem)
    if not want:
        return []
    pref = (preferred_lang or "").lower()
    root_str = str(root.resolve())

    results: list[dict] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden directories (mutate in place to prune the walk).
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        depth = dirpath[len(root_str):].count(os.sep)
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        for fname in filenames:
            scanned += 1
            if scanned > _MAX_FILES_SCANNED:
                log.warning("media-root scan hit file cap (%d); truncating",
                            _MAX_FILES_SCANNED)
                break
            if not _is_sub(fname):
                continue
            stem = os.path.splitext(fname)[0]
            have = _tokens(stem)
            if not have:
                continue
            overlap = want & have
            if not overlap:
                continue
            # Jaccard-ish score in [0, 1]; reward full-stem matches.
            score = len(overlap) / len(want | have)
            full = os.path.abspath(os.path.join(dirpath, fname))
            lang = _lang_of(stem, video_stem)
            if pref and lang == pref:
                score += 0.25
            # Prefer .srt/.vtt slightly.
            score += (len(SUB_EXTS) - _ext_rank(fname)) * 0.01
            results.append({
                "path": full,
                "name": fname,
                "lang": lang or "und",
                "score": round(score, 4),
            })
        else:
            continue
        break  # reached only when the inner loop hit the file cap

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# --------------------------------------------------------------------------- #
# Tier 3: OpenSubtitles REST API
# --------------------------------------------------------------------------- #

def _os_request(url: str, api_key: str, *, data: bytes | None = None) -> dict:
    """Perform a JSON request against the OpenSubtitles API; return parsed body."""
    headers = {
        "Api-Key": api_key,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise SubSearchError(
            f"OpenSubtitles API error {exc.code}: {exc.reason}"
            + (f" — {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise SubSearchError(f"could not reach OpenSubtitles: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise SubSearchError(f"network error contacting OpenSubtitles: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        raise SubSearchError("invalid response from OpenSubtitles") from exc


def search_online(
    video_path: str, preferred_lang: str, api_key: str, *, limit: int = 10
) -> list[dict]:
    """Query OpenSubtitles for subtitles matching the video's file name.

    Requires a non-empty ``api_key``; otherwise raises :class:`SubSearchError`.
    Returns candidates as
    ``[{"id", "name", "lang", "release", "download_count", "file_id", "score"}]``,
    ranked by filename similarity to the video (download count breaks ties).
    """
    if not api_key:
        raise SubSearchError(
            "online subtitle search needs an OpenSubtitles API key — "
            "set one in Settings"
        )
    query = Path(video_path).stem
    want = _tokens(query)
    params = {"query": query}
    if preferred_lang:
        params["languages"] = preferred_lang.lower()
    url = f"{_OS_BASE}/subtitles?" + urllib.parse.urlencode(params)
    body = _os_request(url, api_key)

    results: list[dict] = []
    for item in (body.get("data") or []):
        attrs = item.get("attributes") or {}
        files = attrs.get("files") or []
        file_id = files[0].get("file_id") if files else None
        fname = files[0].get("file_name") if files else None
        release = attrs.get("release") or ""
        name = fname or release or query
        lang = attrs.get("language") or "und"
        downloads = attrs.get("download_count") or 0
        # Rank by how closely the subtitle's name/release matches the video file
        # name (Jaccard over normalized tokens), with a small nudge for the
        # preferred language. Download count is only a tiebreaker.
        score = _similarity(want, _tokens(f"{name} {release}"))
        if preferred_lang and lang.lower().startswith(preferred_lang.lower()):
            score += 0.05
        results.append({
            "id": item.get("id"),
            "name": name,
            "lang": lang,
            "release": release,
            "download_count": downloads,
            "file_id": file_id,
            "score": round(score, 4),
        })
    # Most similar to the video filename first; downloads break ties.
    results.sort(key=lambda r: (r["score"], r["download_count"]), reverse=True)
    return results[:limit]


def download_online(file_id: str, api_key: str, dest_dir: str) -> str:
    """Resolve a download link for ``file_id`` and save the subtitle locally.

    Returns the saved file path. Errors are wrapped in :class:`SubSearchError`.
    """
    if not api_key:
        raise SubSearchError("downloading subtitles needs an OpenSubtitles API key")
    if not file_id:
        raise SubSearchError("no file_id given for download")

    payload = json.dumps({"file_id": file_id}).encode("utf-8")
    body = _os_request(f"{_OS_BASE}/download", api_key, data=payload)
    link = body.get("link")
    if not link:
        raise SubSearchError("OpenSubtitles did not return a download link")
    file_name = body.get("file_name") or f"subtitle-{file_id}.srt"
    # Keep only the basename and ensure a subtitle extension.
    file_name = os.path.basename(file_name)
    if os.path.splitext(file_name)[1].lower() not in _EXT_RANK:
        file_name += ".srt"

    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, file_name)
    req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.URLError as exc:
        raise SubSearchError(f"failed to download subtitle: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise SubSearchError(f"failed to download subtitle: {exc}") from exc
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest
