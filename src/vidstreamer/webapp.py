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
import webbrowser
from pathlib import Path

import click
from aiohttp import web

from . import discovery
from .app import CastOptions, Session, prepare_session
from .caster import STREAM_BUFFERED
from .config import log
from .errors import VidstreamerError
from .probe import MediaInfo, probe_source
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
        # For ffmpeg-pipe streams a seek re-origins the device clock to 0, so we
        # add this base to the device's reported time to show absolute position.
        self.seek_base: float = 0.0
        self.orig_vtt: str | None = None  # original WebVTT, for re-shift on seek
        self.lock = asyncio.Lock()

    async def teardown(self) -> None:
        sess, self.session = self.session, None
        self.seek_base = 0.0
        self.orig_vtt = None
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
    return web.json_response(_info_payload(info))


async def api_fs(request: web.Request) -> web.Response:
    """List a local directory for the in-browser file picker."""
    kind = request.query.get("kind", "video")
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
        if not is_dir and child.suffix.lower() not in exts:
            continue
        entries.append({"name": child.name, "path": str(child), "is_dir": is_dir})
    parent = str(p.parent) if p.parent != p else None
    return web.json_response({"path": str(p), "parent": parent, "entries": entries})


def _opts_from_request(data: dict) -> tuple[str, CastOptions]:
    source = (data.get("source") or "").strip()

    def _opt(key):
        v = data.get(key)
        return v if v not in (None, "") else None

    opts_dict = {
        "device": _opt("device"),
        "subtitle_path": _opt("subtitle_path"),
        "sub_track": (str(_opt("sub_track")) if _opt("sub_track") is not None else None),
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


async def _seek(state: UIState, pos: float) -> None:
    s = state.session
    if s is None:
        return
    if s.plan.serve_mode == "direct_range":
        await _run(s.caster.seek, pos)
        state.seek_base = 0.0
        return
    # Pipe streams aren't seekable on the device: reload at the offset, which
    # restarts the device clock at 0, and shift the WebVTT to match.
    h = s.server.handles
    if state.orig_vtt and s.subtitle_plan.vtt_path:
        shifted = shift_vtt(state.orig_vtt, pos)
        Path(s.subtitle_plan.vtt_path).write_text(shifted, encoding="utf-8")
    video_url = f"{h.video_url}?t={pos:.3f}"

    def _play():
        s.caster.play(
            video_url, s.plan.content_type, title=state.title,
            subtitles=h.subtitle_url, subtitles_lang=s.subtitle_plan.language,
            stream_type=STREAM_BUFFERED,
        )

    await _run(_play)
    state.seek_base = pos


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


def build_app() -> web.Application:
    app = web.Application()
    app["state"] = UIState()
    app.router.add_get("/", index)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/probe", api_probe)
    app.router.add_get("/api/fs", api_fs)
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
