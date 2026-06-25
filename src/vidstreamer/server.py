"""Local HTTP server: Range serving, CORS, WebVTT tracks, and ffmpeg-pipe streaming.

The Chromecast fetches the (possibly transcoded) video and the WebVTT track from
this server. CORS is mandatory once any text track is present, so every response
carries permissive CORS headers (SPEC §3.2, §6).
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from aiohttp import web

from .compat import StreamPlan
from .config import find_binary, log
from .netutil import detect_lan_ip
from .probe import MediaInfo
from .subtitles import BurnIn
from .transcode import build_ffmpeg_command

CHUNK = 64 * 1024

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Range, Accept-Encoding",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
}

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@dataclass
class ServerHandles:
    base_url: str
    video_url: str
    subtitle_url: str | None


def _with_cors(headers: dict | None = None) -> dict:
    """Merge CORS headers into a response header dict.

    Needed because StreamResponse handlers call ``prepare()`` themselves, so the
    middleware (which runs after the handler returns) is too late to add headers.
    """
    merged = dict(CORS_HEADERS)
    if headers:
        merged.update(headers)
    return merged


@web.middleware
async def _cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)
    resp = await handler(request)
    for k, v in CORS_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


class MediaServer:
    def __init__(
        self,
        *,
        plan: StreamPlan,
        info: MediaInfo,
        vtt_path: str | None = None,
        burn_in: BurnIn | None = None,
        bind_ip: str | None = None,
        port: int = 0,
    ) -> None:
        self.plan = plan
        self.info = info
        self.vtt_path = vtt_path
        self.burn_in = burn_in
        self.bind_ip = bind_ip or detect_lan_ip()
        self.port = port
        self._runner: web.AppRunner | None = None
        self._procs: set[asyncio.subprocess.Process] = set()
        self._actual_port: int | None = None

    # -- lifecycle -------------------------------------------------------- #

    async def start(self) -> ServerHandles:
        app = web.Application(middlewares=[_cors_middleware])
        app.router.add_route("*", "/healthz", self._healthz)
        app.router.add_route("*", "/video", self._video)
        app.router.add_route("*", "/sub/{name}", self._subtitle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        self._actual_port = self._runner.addresses[0][1]
        return self.handles

    async def stop(self) -> None:
        for proc in list(self._procs):
            await self._kill(proc)
        self._procs.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        self._procs.discard(proc)

    # -- urls ------------------------------------------------------------- #

    @property
    def base_url(self) -> str:
        return f"http://{self.bind_ip}:{self._actual_port}"

    @property
    def handles(self) -> ServerHandles:
        sub = f"{self.base_url}/sub/0.vtt" if self.vtt_path else None
        return ServerHandles(
            base_url=self.base_url,
            video_url=f"{self.base_url}/video",
            subtitle_url=sub,
        )

    # -- routes ----------------------------------------------------------- #

    async def _healthz(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _subtitle(self, request: web.Request) -> web.StreamResponse:
        if not self.vtt_path or not os.path.isfile(self.vtt_path):
            raise web.HTTPNotFound()
        resp = web.FileResponse(self.vtt_path, headers=_with_cors())
        resp.headers["Content-Type"] = "text/vtt; charset=utf-8"
        return resp

    async def _video(self, request: web.Request) -> web.StreamResponse:
        if self.plan.serve_mode == "direct_range":
            return await self._serve_file(request)
        return await self._serve_pipe(request)

    # -- direct file serving with Range ---------------------------------- #

    async def _serve_file(self, request: web.Request) -> web.StreamResponse:
        path = self.info.ffmpeg_input
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        size = os.path.getsize(path)
        ctype = self.plan.content_type

        start, end = 0, size - 1
        status = 200
        range_header = request.headers.get("Range")
        if range_header:
            m = _RANGE_RE.search(range_header)
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1 == "" and g2:                       # bytes=-N (suffix)
                    start = max(0, size - int(g2))
                else:
                    start = int(g1)
                    end = int(g2) if g2 else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    return web.Response(
                        status=416,
                        headers=_with_cors({"Content-Range": f"bytes */{size}"}),
                    )
                status = 206

        length = end - start + 1
        headers = _with_cors({
            "Content-Type": ctype,
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        })
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        resp = web.StreamResponse(status=status, headers=headers)
        if request.method == "HEAD":
            await resp.prepare(request)
            return resp
        await resp.prepare(request)
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                data = fh.read(min(CHUNK, remaining))
                if not data:
                    break
                await resp.write(data)
                remaining -= len(data)
        await resp.write_eof()
        return resp

    # -- ffmpeg pipe serving --------------------------------------------- #

    async def _serve_pipe(self, request: web.Request) -> web.StreamResponse:
        seek = None
        t = request.query.get("t")
        if t:
            try:
                seek = float(t)
            except ValueError:
                seek = None

        cmd = build_ffmpeg_command(
            self.plan, self.info, burn_in=self.burn_in, seek=seek,
            ffmpeg_path=find_binary("ffmpeg"),
        )
        log.debug("pipe ffmpeg: %s", " ".join(cmd))

        resp = web.StreamResponse(
            status=200,
            headers=_with_cors({"Content-Type": self.plan.content_type}),
        )
        if request.method == "HEAD":
            await resp.prepare(request)
            return resp

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._procs.add(proc)
        log.info("pipe: %s connected, ffmpeg pid=%s", request.remote, proc.pid)

        # Drain stderr concurrently so a chatty ffmpeg can't deadlock on a full
        # pipe, and so we can surface the reason it died.
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_chunks.append(line)
        stderr_task = asyncio.ensure_future(_drain_stderr())

        await resp.prepare(request)
        served = 0
        try:
            assert proc.stdout is not None
            while True:
                data = await proc.stdout.read(CHUNK)
                if not data:
                    break
                served += len(data)
                await resp.write(data)
            await resp.write_eof()
        except (asyncio.CancelledError, ConnectionResetError):
            # Client (Chromecast) disconnected, e.g. on seek/stop.
            raise
        finally:
            await self._kill(proc)
            stderr_task.cancel()
            stderr = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
            # returncode -9/-15 = we killed it (normal on disconnect/seek/stop).
            if proc.returncode not in (0, None, -9, -15):
                log.warning("ffmpeg exited %s after %d bytes; stderr:\n%s",
                            proc.returncode, served, stderr or "(empty)")
            elif served == 0 and stderr:
                log.warning("ffmpeg produced no output; stderr:\n%s", stderr)
            else:
                log.debug("pipe closed: %d bytes served, ffmpeg rc=%s",
                          served, proc.returncode)
        return resp
