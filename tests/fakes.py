"""Test doubles for the Chromecast control plane (no hardware needed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vidstreamer.discovery import Device


@dataclass
class FakeStatus:
    player_state: str = "PLAYING"
    current_time: float = 0.0


class FakeMediaController:
    def __init__(self) -> None:
        self.play_media_calls: list[dict] = []
        self.enabled_subtitles: list[int] = []
        self.control_calls: list[tuple] = []
        self.status = FakeStatus()
        self.active = False

    def play_media(self, url, content_type, **kwargs):
        self.play_media_calls.append(
            {"url": url, "content_type": content_type, **kwargs}
        )
        self.active = True

    def block_until_active(self, timeout=None):
        self.active = True

    def update_status(self):
        self.control_calls.append(("update_status",))

    def enable_subtitle(self, track_id):
        self.enabled_subtitles.append(track_id)

    def pause(self):
        self.control_calls.append(("pause",))

    def play(self):
        self.control_calls.append(("play",))

    def stop(self):
        self.control_calls.append(("stop",))

    def seek(self, position):
        self.control_calls.append(("seek", position))


@dataclass
class FakeCastInfo:
    friendly_name: str
    model_name: str
    host: str
    uuid: str


class FakeChromecast:
    def __init__(self, name="Living Room", model="Chromecast Ultra",
                 host="192.168.1.50", uuid="uuid-1234"):
        self.cast_info = FakeCastInfo(name, model, host, uuid)
        self.media_controller = FakeMediaController()
        self.volume = 1.0
        self.waited = False
        self.disconnected = False
        self.quit_called = False

    def wait(self, timeout=None):
        self.waited = True

    def set_volume(self, level):
        self.volume = level

    def quit_app(self):
        self.quit_called = True

    def disconnect(self):
        self.disconnected = True


def fake_device(cc: FakeChromecast | None = None) -> Device:
    cc = cc or FakeChromecast()
    return Device.from_cast(cc)
