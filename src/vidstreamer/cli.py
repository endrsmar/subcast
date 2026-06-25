"""Command-line interface for vidstreamer."""

from __future__ import annotations

import json
import sys

import click

from . import __version__
from .config import check_dependencies, setup_logging
from .errors import VidstreamerError


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="vidstreamer")
@click.option("-v", "--verbose", count=True, help="Increase verbosity (-v, -vv).")
@click.pass_context
def cli(ctx: click.Context, verbose: int) -> None:
    """Cast local or web video to a Chromecast Ultra with subtitle support."""
    setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    # Commands need ffmpeg/ffprobe. --version/--help are eager and exit before
    # reaching this callback, so they are exempt from the dependency check.
    check_dependencies()


@cli.command()
@click.argument("source")
@click.option("-d", "--device", help="Target device by friendly name or IP.")
@click.option("-s", "--subtitles", "subtitle_path", type=click.Path(), help="Sidecar subtitle file.")
@click.option("--sub-track", help="Select an embedded subtitle track (index or language).")
@click.option("--sub-lang", help="Preferred subtitle language for auto-selection.")
@click.option("--auto-subs", is_flag=True, help="Auto-detect sidecar / default embedded subtitle.")
@click.option("--burn-subs", is_flag=True, help="Burn selected subtitles into the video (re-encode).")
@click.option("--no-subs", is_flag=True, help="Disable subtitles entirely.")
@click.option("--force-transcode", is_flag=True, help="Always transcode the video.")
@click.option("--no-transcode", is_flag=True, help="Never transcode; fail if unsupported.")
@click.option("--video-codec", type=click.Choice(["h264", "hevc"]), help="Target video codec.")
@click.option("--audio-codec", type=click.Choice(["aac", "copy"]), help="Target audio codec.")
@click.option("--max-height", type=int, help="Cap output height (px) when transcoding.")
@click.option("--bind-ip", help="LAN IP to advertise to the device.")
@click.option("--port", type=int, default=0, help="HTTP server port (0 = ephemeral).")
@click.option("--volume", type=float, help="Initial volume 0.0-1.0.")
@click.option("--timeout", type=float, default=8.0, help="Device discovery timeout (s).")
@click.option("--non-interactive", is_flag=True, help="No prompts; exit after starting playback.")
@click.option("--json-status", is_flag=True, help="Emit machine-readable status.")
@click.pass_context
def cast(ctx: click.Context, source: str, **opts: object) -> None:
    """Cast SOURCE (local path or http(s) URL) to a Chromecast."""
    from .app import run_cast

    run_cast(source, opts)


@cli.command()
@click.option("--timeout", type=float, default=8.0, help="Discovery timeout (s).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def devices(timeout: float, as_json: bool) -> None:
    """Discover and list Chromecasts on the LAN."""
    from .discovery import discover

    found = discover(timeout=timeout)
    if as_json:
        click.echo(json.dumps([d.as_dict() for d in found], indent=2))
        return
    if not found:
        click.echo("No devices found.")
        return
    for d in found:
        click.echo(f"{d.name}\t{d.model}\t{d.host}")


@cli.command()
@click.argument("source")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def probe(ctx: click.Context, source: str, as_json: bool) -> None:
    """Probe SOURCE and print MediaInfo + the computed StreamPlan."""
    from .probe import probe_source
    from .compat import plan_stream

    info = probe_source(source)
    plan = plan_stream(info)
    if as_json:
        click.echo(json.dumps({"media": info.as_dict(), "plan": plan.as_dict()}, indent=2))
    else:
        click.echo(info.summary())
        click.echo("")
        click.echo(plan.summary())


@cli.command()
@click.option("-d", "--device", help="Target device by friendly name or IP.")
@click.option("--timeout", type=float, default=8.0, help="Discovery timeout (s).")
def stop(device: str | None, timeout: float) -> None:
    """Stop playback / quit the receiver app on a device."""
    from .discovery import discover, select_device
    from .caster import Caster

    found = discover(timeout=timeout)
    cc = select_device(found, device)
    Caster(cc).stop()
    click.echo("Stopped.")


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Maps VidstreamerError to its exit code."""
    try:
        cli.main(args=argv, standalone_mode=False)
        return 0
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 1
    except VidstreamerError as exc:
        click.echo(f"error: {exc}", err=True)
        return exc.exit_code
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
