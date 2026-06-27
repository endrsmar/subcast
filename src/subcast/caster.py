"""Drive a Chromecast's MediaController: load media, subtitles, and controls."""

from __future__ import annotations

from typing import Any

from .config import log

STREAM_BUFFERED = "BUFFERED"
STREAM_LIVE = "LIVE"

SUBTITLE_TRACK_ID = 1


class Caster:
    """Orchestrates playback on a single device.

    Accepts either a :class:`~subcast.discovery.Device` or a raw cast object
    (anything exposing ``media_controller``, ``set_volume``, ``wait``,
    ``disconnect``), which keeps it unit-testable with a FakeChromecast.
    """

    def __init__(self, device: Any) -> None:
        self.cast = getattr(device, "cast", None) or device

    @property
    def mc(self):
        return self.cast.media_controller

    def connect(self, timeout: float = 10.0) -> None:
        wait = getattr(self.cast, "wait", None)
        if callable(wait):
            wait(timeout=timeout)

    def play(
        self,
        url: str,
        content_type: str,
        *,
        title: str = "subcast",
        subtitles: str | None = None,
        subtitles_lang: str = "und",
        stream_type: str = STREAM_BUFFERED,
        current_time: float = 0,
    ) -> None:
        log.info("loading media: %s (%s) stream=%s subs=%s",
                 url, content_type, stream_type, bool(subtitles))
        self.mc.play_media(
            url,
            content_type,
            title=title,
            subtitles=subtitles,
            subtitles_lang=subtitles_lang,
            subtitles_mime="text/vtt",
            stream_type=stream_type,
            current_time=current_time,
        )
        block = getattr(self.mc, "block_until_active", None)
        if callable(block):
            block(timeout=10)
        if subtitles:
            # Refresh status then activate the side-loaded track.
            update = getattr(self.mc, "update_status", None)
            if callable(update):
                update()
            self.mc.enable_subtitle(SUBTITLE_TRACK_ID)

    def disable_subtitles(self) -> None:
        """Turn captions off on the receiver, clearing any cue currently painted.

        Called before a track-swapping reload: a cue that is on screen at reload
        time would otherwise stick (the receiver never sees its end) and later
        cues pile on top of it.
        """
        fn = getattr(self.mc, "disable_subtitle", None)
        if callable(fn):
            try:
                fn()
            except Exception as exc:  # best-effort; not all states accept it
                log.debug("disable_subtitle failed: %s", exc)

    # -- controls --------------------------------------------------------- #

    def pause(self) -> None:
        self.mc.pause()

    def resume(self) -> None:
        self.mc.play()

    def stop(self) -> None:
        try:
            self.mc.stop()
        finally:
            quit_app = getattr(self.cast, "quit_app", None)
            if callable(quit_app):
                quit_app()

    def seek(self, position: float) -> None:
        self.mc.seek(position)

    def set_volume(self, level: float) -> None:
        self.cast.set_volume(max(0.0, min(1.0, level)))

    def disconnect(self) -> None:
        disc = getattr(self.cast, "disconnect", None)
        if callable(disc):
            disc()

    @property
    def status(self):
        return getattr(self.mc, "status", None)

    def status_line(self) -> str:
        """A short human description of the current media status."""
        st = self.status
        if st is None:
            return "no status"
        state = getattr(st, "player_state", "?")
        idle = getattr(st, "idle_reason", None)
        ct = getattr(st, "current_time", None)
        parts = [str(state)]
        if idle:
            parts.append(f"idle_reason={idle}")
        if ct is not None:
            parts.append(f"t={ct}")
        return " ".join(parts)
