"""Background poster artwork: resolve a normalized title to cached cover art.

A video filename is normalized to a stable *art key* so that variant releases of
the same title (different resolution, codec, source) share a single cached
poster. Artwork is looked up through a small provider chain, best first:

1. **TMDB** (The Movie Database) — rich movie + TV posters, but needs a free API
   key (set in Settings, like the OpenSubtitles key). Used only when a key is
   configured.
2. **iTunes Search API** — public, JSON, no key required. Apple's storefront
   reliably returns TV-show artwork (its movie catalog no longer responds), so
   this is the keyless fallback that gets posters out of the box for series.

The found image is downloaded once and stored permanently under
``~/.vidstreamer/art_library`` (override with ``$VIDSTREAMER_ART_DIR``). All
network work happens on a small background thread pool so the web UI never
blocks; until a poster arrives — or if none is found — the UI keeps its
generated gradient tile.

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .config import log

USER_AGENT = f"vidstreamer v{__version__}"
_HTTP_TIMEOUT = 15.0

_ITUNES_BASE = "https://itunes.apple.com/search"
# iTunes artwork URLs embed the pixel size (``100x100bb.jpg``); we rewrite that
# to a larger square for a crisp poster.
_ITUNES_SIZE = "600x600"

_TMDB_BASE = "https://api.themoviedb.org/3"
# TMDB image CDN renditions (no key needed for images). Posters are portrait
# (2:3); backdrops are wide (16:9) — we fetch the right one per use: tall posters
# for tiles, wide backdrops for the ambient backgrounds.
_TMDB_POSTER_IMG = "https://image.tmdb.org/t/p/w600_and_h900_bestv2"
_TMDB_BACKDROP_IMG = "https://image.tmdb.org/t/p/w1280"

# Artwork variants. "poster" = portrait tile art; "backdrop" = wide background.
POSTER = "poster"
BACKDROP = "backdrop"

# Transport-level failures: a provider couldn't answer (vs. answering "nothing").
# Distinguishing the two keeps us from caching a flaky network as a permanent miss.
_NET_ERRORS = (urllib.error.URLError, TimeoutError, OSError, ValueError)

# Cap a single poster download so a bad/huge URL can't fill the disk.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

ART_DIR_ENV = "VIDSTREAMER_ART_DIR"

# A trailing release year, used both to disambiguate movies and to keep it out
# of the search term (iTunes matches titles better without it).
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #

def art_library_dir() -> Path:
    """Permanent on-disk poster store (honors ``$VIDSTREAMER_ART_DIR``)."""
    override = os.environ.get(ART_DIR_ENV)
    base = Path(override) if override else Path.home() / ".vidstreamer" / "art_library"
    return base


def _suffix(variant: str) -> str:
    """Filename suffix per variant: poster files are bare, backdrops get ``.bg``."""
    return ".bg" if variant == BACKDROP else ""


def _image_path(key: str, variant: str = POSTER) -> Path:
    return art_library_dir() / f"{key}{_suffix(variant)}.jpg"


def _miss_path(key: str, variant: str = POSTER) -> Path:
    return art_library_dir() / f"{key}{_suffix(variant)}.miss"


# --------------------------------------------------------------------------- #
# Normalization — filename -> stable art descriptor
# --------------------------------------------------------------------------- #

def _slug(text: str) -> str:
    """Lowercase to a bare alphanumeric run (drops spaces/punctuation)."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def describe(title: str, series: str | None = None) -> dict | None:
    """Build an art descriptor for a video from its display title / series.

    Returns ``{"key", "query", "kind", "year"}`` where ``key`` is a stable,
    filesystem-safe identifier (so two filename variants of one title collide on
    the same cached poster), ``query`` is the human search term, ``kind`` is
    ``"tv"`` for anything with a parsed series else ``"movie"``, and ``year`` is
    a release year (movies only) used to sharpen the search. Returns ``None``
    when no usable name remains (e.g. an all-numeric stem).

    A trailing year is folded into a movie's key (to separate remakes) but left
    out of the search term. Series keys ignore year entirely so every season /
    episode of a show shares one poster.
    """
    kind = "tv" if series else "movie"
    base = (series or title or "").strip()
    if not base:
        return None

    # For a movie, the (last) release year marks the end of the real title;
    # anything after it is release noise (source/group tags like ``DCPRiP``,
    # ``LiNE`` or a ``-Robo29`` group that we can't enumerate as tokens). Take
    # the text before the year, falling back to the year-stripped base when the
    # year is the leading token. Series ignore year entirely.
    year = ""
    name = base
    if not series and (matches := list(_YEAR_RE.finditer(base))):
        m = matches[-1]
        year = m.group(0)
        name = base[: m.start()].strip() or _YEAR_RE.sub(" ", base)
    name = re.sub(r"\s+", " ", name).strip()

    slug = _slug(name)
    if not slug:
        return None

    key = f"{'tv' if kind == 'tv' else 'mv'}_{slug}"
    if year:
        key += f"_{year}"
    return {"key": key, "query": name or base, "kind": kind, "year": year}


# --------------------------------------------------------------------------- #
# Provider lookups + download
# --------------------------------------------------------------------------- #

def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.read(_MAX_IMAGE_BYTES + 1)


def _tmdb_search(
    query: str, kind: str, year: str, api_key: str, variant: str
) -> str | None:
    """Look up a TMDB image URL for ``query``; ``None`` if nothing matches.

    Picks the wide ``backdrop_path`` for the backdrop variant, else the portrait
    ``poster_path`` — scanning results for the first that has the wanted image.
    """
    params = {"api_key": api_key, "query": query, "include_adult": "false"}
    if year and kind != "tv":
        params["year"] = year
    path = "/search/tv" if kind == "tv" else "/search/movie"
    url = f"{_TMDB_BASE}{path}?" + urllib.parse.urlencode(params)
    body = json.loads(_get(url).decode("utf-8"))
    field, base = (
        ("backdrop_path", _TMDB_BACKDROP_IMG) if variant == BACKDROP
        else ("poster_path", _TMDB_POSTER_IMG)
    )
    for item in body.get("results") or []:
        image = item.get(field)
        if image:
            return f"{base}{image}"
    return None


def _itunes_search(query: str, kind: str, variant: str) -> str | None:
    """Look up an iTunes poster URL for ``query``; ``None`` if nothing matches.

    iTunes only offers square cover art (no wide backdrops), so the backdrop
    variant returns ``None`` here — the caller then falls back to the poster.
    """
    if variant == BACKDROP:
        return None
    params = {
        "term": query,
        "media": "tvShow" if kind == "tv" else "movie",
        "entity": "tvSeason" if kind == "tv" else "movie",
        "limit": 5,
        "country": "US",
    }
    url = f"{_ITUNES_BASE}?" + urllib.parse.urlencode(params)
    body = json.loads(_get(url).decode("utf-8"))
    for item in body.get("results") or []:
        raw = item.get("artworkUrl100") or item.get("artworkUrl60")
        if raw:
            # Rewrite the embedded pixel size for a higher-resolution poster.
            return re.sub(r"/\d+x\d+(bb)?\.", f"/{_ITUNES_SIZE}bb.", raw, count=1)
    return None


def search_artwork_url(
    query: str, kind: str, year: str = "", tmdb_key: str = "", variant: str = POSTER
) -> str | None:
    """Resolve an image URL through the provider chain (TMDB → iTunes).

    ``variant`` selects portrait poster art or a wide backdrop. Returns a URL, or
    ``None`` when a provider answered but found nothing. Raises the underlying
    transport error only when *every* provider failed to respond, so callers can
    tell "no such title" (``None``) from "couldn't reach any service" (exception)
    and avoid caching the latter as a permanent miss.
    """
    if not query:
        return None
    providers = []
    if tmdb_key:
        providers.append(lambda: _tmdb_search(query, kind, year, tmdb_key, variant))
    providers.append(lambda: _itunes_search(query, kind, variant))

    answered = False
    last_exc: Exception | None = None
    for provider in providers:
        try:
            url = provider()
        except _NET_ERRORS as exc:
            last_exc = exc
            log.debug("artwork provider failed for %r: %s", query, exc)
            continue
        answered = True
        if url:
            return url
    if not answered and last_exc is not None:
        raise last_exc
    return None


def fetch(
    key: str, query: str, kind: str, year: str = "", tmdb_key: str = "",
    variant: str = POSTER,
) -> Path | None:
    """Search, download and cache one artwork ``variant``. Returns its path or None.

    A definitive "no artwork for this title/variant" is recorded as a ``.miss``
    marker so it is not retried; transient network errors are swallowed (logged)
    without a marker so a later attempt can still succeed.
    """
    cached = cached_path(key, variant)
    if cached:
        return cached
    try:
        url = search_artwork_url(query, kind, year, tmdb_key, variant)
    except _NET_ERRORS as exc:
        log.debug("artwork lookup failed for %r: %s", query, exc)
        return None
    if not url:
        _mark_miss(key, variant)
        log.debug("no %s found for %r (%s)", variant, query, kind)
        return None
    try:
        data = _get(url)
    except _NET_ERRORS as exc:
        log.debug("artwork download failed for %r: %s", query, exc)
        return None
    if not data or len(data) > _MAX_IMAGE_BYTES:
        log.debug("artwork for %r rejected (empty or too large)", query)
        return None
    return _store(key, data, variant)


def _store(key: str, data: bytes, variant: str = POSTER) -> Path:
    dest = _image_path(key, variant)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)  # atomic publish so readers never see a partial file
    _miss_path(key, variant).unlink(missing_ok=True)
    return dest


def _mark_miss(key: str, variant: str = POSTER) -> None:
    path = _miss_path(key, variant)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text("", encoding="utf-8")
    except OSError as exc:
        log.debug("could not write miss marker for %s: %s", key, exc)


def cached_path(key: str, variant: str = POSTER) -> Path | None:
    """Path to the cached ``variant`` image for ``key`` if present, else ``None``."""
    if not key:
        return None
    path = _image_path(key, variant)
    return path if path.is_file() else None


def is_known_miss(key: str, variant: str = POSTER) -> bool:
    return bool(key) and _miss_path(key, variant).is_file()


def clear_misses() -> int:
    """Drop all negative-result markers so misses are retried.

    Called when the artwork provider configuration changes (e.g. a TMDB key is
    added), since titles previously recorded as "no art" may now resolve.
    Returns the number of markers removed.
    """
    base = art_library_dir()
    if not base.is_dir():
        return 0
    removed = 0
    for marker in base.glob("*.miss"):
        try:
            marker.unlink()
            removed += 1
        except OSError as exc:
            log.debug("could not remove miss marker %s: %s", marker, exc)
    return removed


# --------------------------------------------------------------------------- #
# Background service
# --------------------------------------------------------------------------- #

class ArtworkService:
    """Schedules poster fetches on a bounded thread pool, deduping in-flight keys.

    The web layer asks for a key's :meth:`state` ("ready" / "pending" / "none");
    an unknown key is scheduled for a background fetch and reported "pending".
    Nothing here touches the network on the calling (event-loop) thread.
    """

    def __init__(self, max_workers: int = 3) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="art"
        )
        self._lock = threading.Lock()
        self._inflight: set[str] = set()

    def state(self, key: str, query: str = "", kind: str = "movie",
              year: str = "", tmdb_key: str = "", variant: str = POSTER,
              *, schedule: bool = True) -> str:
        """Report a key/variant's status, scheduling a fetch for an unknown one.

        Returns ``"ready"`` (image cached), ``"none"`` (looked up, nothing
        found), or ``"pending"`` (fetch running or just scheduled). With
        ``schedule=False`` an unscheduled unknown key reports ``"none"``.
        """
        if not key:
            return "none"
        if cached_path(key, variant):
            return "ready"
        if is_known_miss(key, variant):
            return "none"
        flight = f"{key}{_suffix(variant)}"
        with self._lock:
            if flight in self._inflight:
                return "pending"
            if not schedule or not query:
                return "none"
            self._inflight.add(flight)
        self._pool.submit(self._run, key, query, kind, year, tmdb_key, variant, flight)
        return "pending"

    def _run(self, key: str, query: str, kind: str, year: str, tmdb_key: str,
             variant: str, flight: str) -> None:
        try:
            fetch(key, query, kind, year, tmdb_key, variant)
        except Exception as exc:  # never let a worker thread die silently
            log.debug("artwork worker error for %s: %s", flight, exc)
        finally:
            with self._lock:
                self._inflight.discard(flight)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
