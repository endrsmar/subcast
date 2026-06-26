"""Local web UI: a single-page control panel served over aiohttp.

The browser talks to a small JSON API that drives the same core used by the CLI
(``prepare_session`` + ``Caster``). Only one cast session is active at a time;
its handle lives in :class:`UIState` on the aiohttp application.

The control server binds to localhost (only the user's browser reaches it); the
media server it spins up per cast still binds to the LAN so the Chromecast can
fetch the video and WebVTT track.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import webbrowser
from pathlib import Path

import click
from aiohttp import web

from . import artwork, discovery, subsearch
from .app import CastOptions, Session, prepare_session
from .caster import STREAM_BUFFERED
from .config import log
from .errors import SubSearchError, VidstreamerError
from .probe import MediaInfo, probe_source
from .settings import load_settings, system_language, update_settings
from .source import resolve_source
from .subtitles import shift_vtt

WEB_DIR = Path(__file__).parent / "web"

VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts",
    ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ogv",
}
SUB_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub"}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

class UIState:
    """Holds the single active cast session and seek bookkeeping."""

    def __init__(self) -> None:
        self.session: Session | None = None
        self.title: str = "vidstreamer"
        # Poster descriptor ({key, query, kind} or None) for the casting title,
        # so the player screen can show real artwork — not just a gradient.
        self.art: dict | None = None
        # For ffmpeg-pipe streams a seek re-origins the device clock to 0, so we
        # add this base to the device's reported time to show absolute position.
        self.seek_base: float = 0.0
        self.orig_vtt: str | None = None  # original WebVTT, for re-shift on seek
        # Manual subtitle delay (seconds; positive = subs later). Applied live by
        # rewriting the served WebVTT and reloading the track on the device.
        self.sub_offset: float = 0.0
        # Bumped on every track rewrite so the reload URL changes and the receiver
        # re-fetches instead of serving a cached copy of the old timing.
        self.sub_version: int = 0
        self.lock = asyncio.Lock()

    async def teardown(self) -> None:
        sess, self.session = self.session, None
        self.seek_base = 0.0
        self.orig_vtt = None
        self.sub_offset = 0.0
        self.art = None
        if sess is not None:
            try:
                await sess.close()
            except Exception as exc:  # best-effort cleanup
                log.debug("session teardown error: %s", exc)


async def _run(fn, *args):
    """Run a blocking core call off the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def _art_for_source(source: str) -> dict | None:
    """Poster descriptor for a source from its filename (lazy library import)."""
    from . import library

    title = os.path.basename(source.rstrip("/")) or source
    ep = library.parse_episode(title)
    return artwork.describe(library.clean_title(title), ep["series"] if ep else None)


def _info_payload(info: MediaInfo) -> dict:
    return {
        "source": info.source,
        "is_remote": info.is_remote,
        "container": info.container,
        "duration": info.duration,
        "video": (
            {
                "codec": info.video.codec,
                "width": info.video.width,
                "height": info.video.height,
                "hdr": info.video.is_hdr,
            }
            if info.video else None
        ),
        "audio_tracks": [
            {
                "index": a.audio_index,
                "label": a.label(),
                "language": a.language,
                "codec": a.codec,
                "channels": a.channels,
                "default": a.default,
            }
            for a in info.audio_tracks
        ],
        "subtitle_tracks": [
            {
                "index": s.sub_index,
                "label": _sub_label(s),
                "language": s.language,
                "codec": s.codec,
                "text_based": s.text_based,
                "default": s.default,
                "forced": s.forced,
            }
            for s in info.subtitle_tracks
        ],
    }


def _sub_label(s) -> str:
    parts = [s.language or "und", s.codec]
    base = " · ".join(parts)
    if s.title:
        base += f" ({s.title})"
    if not s.text_based:
        base += " [image]"
    if s.forced:
        base += " [forced]"
    return base


def _status_payload(state: UIState) -> dict:
    s = state.session
    if s is None:
        return {"connected": False}
    mc_status = s.caster.status
    player_state = getattr(mc_status, "player_state", None)
    dev_time = getattr(mc_status, "current_time", None) or 0.0
    cast_status = getattr(s.caster.cast, "status", None)
    cast_info = getattr(s.caster.cast, "cast_info", None)
    return {
        "connected": True,
        "state": player_state,
        "current_time": state.seek_base + dev_time,
        "duration": s.info.duration,
        "volume": getattr(cast_status, "volume_level", None),
        "muted": bool(getattr(cast_status, "volume_muted", False)),
        "title": state.title,
        "device": getattr(cast_info, "friendly_name", None),
        "serve_mode": s.plan.serve_mode,
        "subtitles": s.server.handles.subtitle_url is not None,
        "sub_offset": state.sub_offset,
        "art": state.art,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def api_devices(request: web.Request) -> web.Response:
    timeout = float(request.query.get("timeout", 5))
    try:
        found = await _run(discovery.discover, timeout)
    except Exception as exc:  # discovery is flaky on multi-NIC hosts
        log.warning("discovery failed: %s", exc)
        return web.json_response({"devices": [], "error": str(exc)})
    return web.json_response({"devices": [d.as_dict() for d in found]})


async def api_probe(request: web.Request) -> web.Response:
    data = await request.json()
    source = (data.get("source") or "").strip()
    if not source:
        return web.json_response({"error": "no source given"}, status=400)
    try:
        info = await _run(probe_source, source)
    except VidstreamerError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("probe failed")
        return web.json_response({"error": str(exc)}, status=500)
    payload = _info_payload(info)
    # Attach a poster descriptor so the setup screen can show real artwork as its
    # backdrop the moment a source is entered (before any cast).
    payload["art"] = _art_for_source(source)
    return web.json_response(payload)


async def api_library(request: web.Request) -> web.Response:
    """Recursively list videos under the media root for the library sidebar.

    Imported lazily to avoid a circular import (``library`` pulls ``VIDEO_EXTS``
    from this module).
    """
    from . import library

    refresh = request.query.get("refresh") in ("1", "true", "yes")
    root = load_settings().media_root
    if not root or not os.path.isdir(root):
        return web.json_response({
            "root": root, "items": [], "count": 0,
            "error": "media root is not a valid directory",
        })
    try:
        items = await _run(library.scan_library, root, refresh)
    except Exception as exc:
        log.exception("library scan failed")
        return web.json_response(
            {"root": root, "items": [], "count": 0, "error": str(exc)}
        )
    return web.json_response({"root": root, "items": items, "count": len(items)})


async def api_art(request: web.Request) -> web.Response:
    """Serve a cached poster by key, or 404 so the UI keeps its gradient.

    A long cache lifetime is safe: a poster file's contents never change for a
    given key (a new lookup writes a fresh key), so the browser can hold it.
    """
    key = request.match_info["key"]
    variant = request.query.get("variant", artwork.POSTER)
    path = artwork.cached_path(key, variant)
    if path is None:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


async def api_art_request(request: web.Request) -> web.Response:
    """Report artwork status for a batch of items, scheduling missing fetches.

    Body: ``{"want": [{"id", "key", "query", "kind", "year", "variant"}, ...]}``.
    ``id`` is a caller-chosen token (it distinguishes the same key requested in
    different variants); ``variant`` is ``"poster"`` (default) or ``"backdrop"``.
    Returns ``{"art": {id: "ready"|"pending"|"none"}}``. "ready" items load from
    ``GET /api/art/{key}?variant=...``; "pending" should be polled again shortly.
    """
    art: artwork.ArtworkService = request.app["art"]
    data = await request.json()
    tmdb_key = load_settings().tmdb_api_key
    out: dict[str, str] = {}
    for spec in data.get("want") or []:
        key = (spec.get("key") or "").strip()
        if not key:
            continue
        variant = spec.get("variant") or artwork.POSTER
        rid = spec.get("id") or key
        if rid in out:
            continue
        out[rid] = art.state(
            key, spec.get("query") or "", spec.get("kind") or "movie",
            str(spec.get("year") or ""), tmdb_key, variant,
        )
    return web.json_response({"art": out})


async def api_fs(request: web.Request) -> web.Response:
    """List a local directory for the in-browser file picker.

    ``kind`` selects which leaf files are shown: ``video`` (default), ``sub``, or
    ``dir`` (directories only, for picking a media-root folder).
    """
    kind = request.query.get("kind", "video")
    dir_only = kind == "dir"
    exts = SUB_EXTS if kind == "sub" else VIDEO_EXTS
    raw = request.query.get("path") or str(Path.home())
    p = Path(raw).expanduser()
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else Path.home()
    entries = []
    try:
        children = sorted(
            p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())
        )
    except (PermissionError, OSError) as exc:
        return web.json_response({"error": str(exc), "path": str(p)}, status=400)
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if dir_only and not is_dir:
            continue
        if not dir_only and not is_dir and child.suffix.lower() not in exts:
            continue
        entries.append({"name": child.name, "path": str(child), "is_dir": is_dir})
    parent = str(p.parent) if p.parent != p else None
    return web.json_response({"path": str(p), "parent": parent, "entries": entries})


# --------------------------------------------------------------------------- #
# Settings + subtitle search
# --------------------------------------------------------------------------- #

def _settings_payload() -> dict:
    s = load_settings()
    return {
        "media_root": s.media_root,
        "preferred_sub_lang": s.preferred_sub_lang,
        "opensubtitles_api_key": s.opensubtitles_api_key,
        "has_api_key": bool(s.opensubtitles_api_key),
        "tmdb_api_key": s.tmdb_api_key,
        "has_tmdb_key": bool(s.tmdb_api_key),
        "system_language": system_language(),
    }


def _local_path(source: str) -> str | None:
    """Return the absolute local path for ``source``, or None for remote/invalid."""
    try:
        src = resolve_source(source)
    except VidstreamerError:
        return None
    return None if src.is_remote else src.ffmpeg_input


def _subcache_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "vidstreamer-subs")
    os.makedirs(d, exist_ok=True)
    return d


async def api_settings_get(request: web.Request) -> web.Response:
    return web.json_response(await _run(_settings_payload))


async def api_settings_post(request: web.Request) -> web.Response:
    data = await request.json()
    fields = {
        k: data[k]
        for k in ("media_root", "preferred_sub_lang", "opensubtitles_api_key",
                  "tmdb_api_key")
        if k in data
    }

    def _save():
        before = load_settings().tmdb_api_key
        s = update_settings(**fields)
        # A changed artwork-provider key can turn prior misses into hits, so
        # drop the negative markers to let those titles be looked up again.
        if s.tmdb_api_key != before:
            artwork.clear_misses()
        return _settings_payload()

    return web.json_response(await _run(_save))


async def api_subsearch(request: web.Request) -> web.Response:
    """Cheap subtitle discovery for a source: sidecar + media-root scan."""
    data = await request.json()
    source = (data.get("source") or "").strip()
    settings = load_settings()
    # An explicit per-search language overrides the saved preference.
    lang = (data.get("lang") or "").strip() or settings.preferred_sub_lang
    local = _local_path(source)
    if local is None:
        return web.json_response({
            "sidecar": None, "local": [],
            "online_available": bool(settings.opensubtitles_api_key),
        })

    def _work():
        sidecar = subsearch.find_sidecar(local, lang)
        matches = subsearch.search_media_root(local, settings.media_root, lang)
        # Don't list the same-folder sidecar again under "local".
        if sidecar:
            matches = [m for m in matches if m["path"] != sidecar]
        return sidecar, matches[:20]

    sidecar, matches = await _run(_work)
    return web.json_response({
        "sidecar": sidecar,
        "local": matches,
        "online_available": bool(settings.opensubtitles_api_key),
    })


async def api_subsearch_online(request: web.Request) -> web.Response:
    data = await request.json()
    source = (data.get("source") or "").strip()
    settings = load_settings()
    lang = (data.get("lang") or "").strip() or settings.preferred_sub_lang
    name = source
    local = _local_path(source)
    if local:
        name = local
    try:
        results = await _run(
            subsearch.search_online, name,
            lang, settings.opensubtitles_api_key,
        )
    except SubSearchError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("online subtitle search failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"results": results})


async def api_subdownload(request: web.Request) -> web.Response:
    data = await request.json()
    file_id = data.get("file_id")
    settings = load_settings()
    if file_id in (None, ""):
        return web.json_response({"error": "no file_id given"}, status=400)
    try:
        path = await _run(
            subsearch.download_online, str(file_id),
            settings.opensubtitles_api_key, _subcache_dir(),
        )
    except SubSearchError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("subtitle download failed")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({"path": path})


def _opts_from_request(data: dict) -> tuple[str, CastOptions]:
    source = (data.get("source") or "").strip()

    def _opt(key):
        v = data.get(key)
        return v if v not in (None, "") else None

    # Default the subtitle language to the user's preferred language unless the
    # request overrides it; this seeds embedded-track auto-selection and the
    # sidecar track label.
    sub_lang = _opt("sub_lang")
    if sub_lang is None:
        try:
            sub_lang = load_settings().preferred_sub_lang or None
        except Exception:  # never let a settings hiccup block a cast
            sub_lang = None

    opts_dict = {
        "device": _opt("device"),
        "subtitle_path": _opt("subtitle_path"),
        "sub_track": (str(_opt("sub_track")) if _opt("sub_track") is not None else None),
        "sub_lang": sub_lang,
        "audio_track": (int(_opt("audio_track")) if _opt("audio_track") is not None else None),
        "no_subs": bool(data.get("no_subs")),
        "volume": (float(data["volume"]) if data.get("volume") is not None else None),
        "timeout": float(data.get("timeout", 8.0)),
    }
    return source, CastOptions.from_dict(opts_dict)


async def api_cast(request: web.Request) -> web.Response:
    state: UIState = request.app["state"]
    data = await request.json()
    source, opts = _opts_from_request(data)
    if not source:
        return web.json_response({"ok": False, "error": "no source given"}, status=400)

    async with state.lock:
        await state.teardown()
        try:
            session = await prepare_session(source, opts)
        except VidstreamerError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            log.exception("cast failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

        state.session = session
        state.seek_base = 0.0
        title = os.path.basename(source.rstrip("/")) or source
        state.title = title
        # Derive a poster descriptor from the filename so the player shows real art.
        state.art = _art_for_source(source)
        vtt = session.subtitle_plan.vtt_path
        state.orig_vtt = (
            Path(vtt).read_text(encoding="utf-8")
            if vtt and os.path.isfile(vtt) else None
        )
    return web.json_response({"ok": True, **_status_payload(state)})


def _refresh(session: Session) -> None:
    update = getattr(session.caster.mc, "update_status", None)
    if callable(update):
        try:
            update()
        except Exception as exc:
            log.debug("status refresh failed: %s", exc)


async def api_status(request: web.Request) -> web.Response:
    state: UIState = request.app["state"]
    if state.session is None:
        return web.json_response({"connected": False})
    await _run(_refresh, state.session)
    return web.json_response(_status_payload(state))


def _write_vtt(state: UIState, seek_base: float) -> None:
    """Rewrite the served WebVTT for the given device-clock origin.

    The net cue shift combines the re-origin (``seek_base``) with the manual
    subtitle delay: cues move back by ``seek_base`` and forward by ``sub_offset``.
    """
    s = state.session
    if s is None or not state.orig_vtt or not s.subtitle_plan.vtt_path:
        return
    shifted = shift_vtt(state.orig_vtt, seek_base - state.sub_offset)
    Path(s.subtitle_plan.vtt_path).write_text(shifted, encoding="utf-8")


def _next_sub_url(state: UIState) -> str | None:
    """Cache-busting subtitle URL so the receiver re-fetches the rewritten track.

    Busts on the URL *path* (``/sub/<n>.vtt``), not a query string: the Chromecast
    receiver keys its track cache on the path and ignores ``?v=``, so a query-only
    change would keep serving the stale timing. The server serves the current VTT
    for any ``/sub/{name}`` path.
    """
    s = state.session
    if s is None or not s.server.handles.subtitle_url:
        return None
    state.sub_version += 1
    return f"{s.server.base_url}/sub/{state.sub_version}.vtt"


def _device_position(state: UIState) -> float:
    """Current absolute playback position, in source time."""
    s = state.session
    if s is None:
        return 0.0
    dev_time = getattr(s.caster.status, "current_time", None) or 0.0
    return state.seek_base + dev_time


async def _reload(state: UIState, pos: float) -> None:
    """Reload media at absolute ``pos`` with the freshly-written subtitle track.

    Used by both seek (pipe streams) and any subtitle-offset change, since a
    side-loaded track can only be replaced on the device by re-issuing the load.
    """
    s = state.session
    if s is None:
        return
    h = s.server.handles
    direct = s.plan.serve_mode == "direct_range"
    seek_base = 0.0 if direct else pos
    # Clear any on-screen cue first, or it sticks across the reload and later
    # cues stack on top of it.
    if h.subtitle_url:
        await _run(s.caster.disable_subtitles)
    _write_vtt(state, seek_base)
    sub_url = _next_sub_url(state)
    video_url = h.video_url if direct else f"{h.video_url}?t={pos:.3f}"
    current_time = pos if direct else 0.0

    def _play():
        s.caster.play(
            video_url, s.plan.content_type, title=state.title,
            subtitles=sub_url, subtitles_lang=s.subtitle_plan.language,
            stream_type=STREAM_BUFFERED, current_time=current_time,
        )

    await _run(_play)
    state.seek_base = seek_base


async def _seek(state: UIState, pos: float) -> None:
    s = state.session
    if s is None:
        return
    if s.plan.serve_mode == "direct_range":
        # Seekable on the device; the absolute-timed track stays valid as-is.
        await _run(s.caster.seek, pos)
        state.seek_base = 0.0
        return
    # Pipe streams aren't seekable on the device: reload at the offset, which
    # restarts the device clock at 0, and shift the WebVTT to match.
    await _reload(state, pos)


async def _apply_sub_offset(state: UIState, offset: float) -> None:
    """Change the subtitle delay live and reload the track at the current spot."""
    s = state.session
    if s is None or not state.orig_vtt:
        return
    state.sub_offset = offset
    await _run(_refresh, s)
    pos = _device_position(state)
    log.info("subtitle offset -> %+.1fs; reloading track at %.1fs", offset, pos)
    await _reload(state, pos)


async def api_control(request: web.Request) -> web.Response:
    state: UIState = request.app["state"]
    data = await request.json()
    action = data.get("action")
    value = data.get("value")
    s = state.session
    if action == "stop":
        await state.teardown()
        return web.json_response({"ok": True})
    if s is None:
        return web.json_response({"ok": False, "error": "no active session"}, status=409)
    try:
        if action == "pause":
            await _run(s.caster.pause)
        elif action in ("resume", "play"):
            await _run(s.caster.resume)
        elif action == "volume":
            await _run(s.caster.set_volume, float(value))
        elif action == "seek":
            await _seek(state, float(value))
        elif action == "sub_offset":
            await _apply_sub_offset(state, float(value))
        else:
            return web.json_response({"ok": False, "error": f"unknown action {action!r}"},
                                     status=400)
    except Exception as exc:
        log.exception("control %s failed", action)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------- #
# App wiring
# --------------------------------------------------------------------------- #

async def _on_cleanup(app: web.Application) -> None:
    await app["state"].teardown()
    app["art"].close()


def build_app() -> web.Application:
    app = web.Application()
    app["state"] = UIState()
    app["art"] = artwork.ArtworkService()
    app.router.add_get("/", index)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/probe", api_probe)
    app.router.add_get("/api/library", api_library)
    app.router.add_get("/api/art/{key}", api_art)
    app.router.add_post("/api/art", api_art_request)
    app.router.add_get("/api/fs", api_fs)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_post("/api/settings", api_settings_post)
    app.router.add_post("/api/subsearch", api_subsearch)
    app.router.add_post("/api/subsearch/online", api_subsearch_online)
    app.router.add_post("/api/subdownload", api_subdownload)
    app.router.add_post("/api/cast", api_cast)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/control", api_control)
    app.on_cleanup.append(_on_cleanup)
    return app


def run_ui(host: str = "127.0.0.1", port: int = 8420, *, open_browser: bool = True) -> None:
    app = build_app()
    url = f"http://{host}:{port}"
    click.echo(f"vidstreamer UI running at {url}")
    click.echo("Press Ctrl-C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # headless / no browser available
            pass
    web.run_app(app, host=host, port=port, print=None)
