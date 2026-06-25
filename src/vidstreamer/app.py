"""End-to-end orchestration for the `cast` command."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

import click

from . import discovery
from .caster import STREAM_BUFFERED, Caster
from .compat import PlanOptions, StreamPlan, plan_stream
from .config import check_dependencies, log
from .errors import VidstreamerError
from .probe import MediaInfo, probe_source
from .server import MediaServer
from .source import resolve_source
from .subtitles import SubtitlePlan, plan_subtitles, prepare_subtitles


@dataclass
class CastOptions:
    device: str | None = None
    subtitle_path: str | None = None
    sub_track: str | None = None
    sub_lang: str | None = None
    auto_subs: bool = False
    burn_subs: bool = False
    no_subs: bool = False
    force_transcode: bool = False
    no_transcode: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None
    max_height: int | None = None
    bind_ip: str | None = None
    port: int = 0
    volume: float | None = None
    timeout: float = 8.0
    non_interactive: bool = False
    json_status: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "CastOptions":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Session:
    info: MediaInfo
    plan: StreamPlan
    subtitle_plan: SubtitlePlan
    server: MediaServer
    caster: Caster
    workdir: str

    async def close(self) -> None:
        try:
            await self.server.stop()
        finally:
            self.caster.disconnect()
            shutil.rmtree(self.workdir, ignore_errors=True)


async def prepare_session(
    source: str,
    opts: CastOptions,
    *,
    discover_fn=None,
) -> Session:
    """Resolve, probe, plan, start the server, and begin playback. Returns a Session.

    ``discover_fn`` is injectable for tests (defaults to real mDNS discovery).
    """
    check_dependencies()
    resolve_source(source)  # validates existence early (raises SourceError)
    info = probe_source(source)

    # --- Subtitles (may force a burn-in / re-encode) ---
    sub_plan = plan_subtitles(
        info,
        sidecar=opts.subtitle_path,
        sub_track=opts.sub_track,
        sub_lang=opts.sub_lang,
        auto_subs=opts.auto_subs,
        burn_subs=opts.burn_subs,
        no_subs=opts.no_subs,
    )
    for warning in sub_plan.warnings:
        log.warning(warning)
        click.echo(f"warning: {warning}", err=True)

    # --- Stream plan ---
    plan_opts = PlanOptions(
        force_transcode=opts.force_transcode,
        no_transcode=opts.no_transcode,
        video_codec=opts.video_codec,
        audio_codec=opts.audio_codec,
        max_height=opts.max_height,
        burn_in=sub_plan.burn_in is not None,
    )
    plan = plan_stream(info, plan_opts)

    workdir = tempfile.mkdtemp(prefix="vidstreamer-")
    sub_plan = prepare_subtitles(sub_plan, info, workdir, opts.subtitle_path)

    # --- Local HTTP server ---
    server = MediaServer(
        plan=plan,
        info=info,
        vtt_path=sub_plan.vtt_path,
        burn_in=sub_plan.burn_in,
        bind_ip=opts.bind_ip,
        port=opts.port,
    )
    await server.start()
    handles = server.handles

    # --- Device ---
    # When the target is given as an IP and we're not under test injection,
    # connect straight to the host. This sidesteps mDNS entirely, which is
    # unreliable on machines with many virtual interfaces / no multicast.
    if discover_fn is None and discovery.looks_like_host(opts.device):
        log.info("connecting directly to %s (skipping discovery)", opts.device)
        device = discovery.connect_host(opts.device, timeout=opts.timeout)
    else:
        discover_fn = discover_fn or discovery.discover
        found = discover_fn(timeout=opts.timeout)
        log.info("discovered %d device(s): %s", len(found),
                 ", ".join(f"{d.name}@{d.host}" for d in found) or "none")
        device = discovery.select_device(found, opts.device)
    log.info("casting to %s (%s) at %s", device.name, device.model, device.host)
    caster = Caster(device)
    caster.connect()
    if opts.volume is not None:
        caster.set_volume(opts.volume)

    title = resolve_source(source).basename
    caster.play(
        handles.video_url,
        plan.content_type,
        title=title or "vidstreamer",
        subtitles=handles.subtitle_url,
        subtitles_lang=sub_plan.language,
        stream_type=STREAM_BUFFERED,
    )

    return Session(
        info=info, plan=plan, subtitle_plan=sub_plan,
        server=server, caster=caster, workdir=workdir,
    )


def run_cast(source: str, opts_dict: dict) -> None:
    """Synchronous entry point used by the CLI."""
    opts = CastOptions.from_dict(opts_dict)

    async def _go() -> None:
        session = await prepare_session(source, opts)
        h = session.server.handles
        click.echo(f"Casting {source}")
        click.echo(f"  serving: {h.video_url}  ({session.plan.content_type})")
        if h.subtitle_url:
            click.echo(f"  subtitles: {h.subtitle_url}")
        click.echo(f"  plan: {session.plan.summary().splitlines()[0]}")
        await _report_status(session)
        try:
            # Reading keyboard input requires a real TTY; otherwise (piped stdin,
            # nohup, systemd) keep serving instead of tearing down immediately.
            if opts.non_interactive or not sys.stdin.isatty():
                click.echo("Playback started. Ctrl-C to stop.")
                await _wait_forever()
            else:
                await _control_loop(session)
        finally:
            await session.close()

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        click.echo("\nStopped.")
    except VidstreamerError:
        raise


async def _report_status(session: Session, attempts: int = 6) -> None:
    """Poll the receiver briefly so the user sees BUFFERING -> PLAYING or an ERROR."""
    loop = asyncio.get_event_loop()
    last = None
    for _ in range(attempts):
        update = getattr(session.caster.mc, "update_status", None)
        if callable(update):
            try:
                await loop.run_in_executor(None, update)
            except Exception as exc:  # network hiccup; keep trying
                log.debug("status update failed: %s", exc)
        line = session.caster.status_line()
        if line != last:
            click.echo(f"  device: {line}")
            last = line
        state = getattr(session.caster.status, "player_state", None)
        idle = getattr(session.caster.status, "idle_reason", None)
        if state == "PLAYING":
            return
        if idle == "ERROR":
            click.echo("  device reported ERROR — it could not load/play the stream.",
                       err=True)
            click.echo("  Check the device can reach the serving URL above "
                       "(firewall/subnet), and re-run with -vv.", err=True)
            return
        await asyncio.sleep(1.0)


async def _wait_forever() -> None:
    while True:
        await asyncio.sleep(3600)


async def _control_loop(session: Session) -> None:
    """Minimal line-based controls (SPEC §8 permits this over raw key handling)."""
    loop = asyncio.get_event_loop()
    click.echo("Controls: [p]ause [r]esume [s <sec>] seek [v <0-1>] volume [q]uit")
    while True:
        line = await loop.run_in_executor(None, _read_line)
        if line is None:
            break
        cmd = line.strip().split()
        if not cmd:
            continue
        key = cmd[0].lower()
        try:
            if key in ("q", "quit"):
                break
            elif key in ("p", "pause"):
                session.caster.pause()
            elif key in ("r", "resume", "play"):
                session.caster.resume()
            elif key in ("s", "seek") and len(cmd) > 1:
                session.caster.seek(float(cmd[1]))
            elif key in ("v", "volume") and len(cmd) > 1:
                session.caster.set_volume(float(cmd[1]))
            else:
                click.echo("?")
        except Exception as exc:  # keep the loop alive on bad input
            click.echo(f"error: {exc}")


def _read_line() -> str | None:
    try:
        return input()
    except EOFError:
        return None
