"""Chromecast discovery and selection (thin wrapper over pychromecast)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from .config import log
from .errors import DeviceError

DEFAULT_CAST_PORT = 8009


def looks_like_host(selector: str | None) -> bool:
    """True if the selector is an IP address we can connect to directly."""
    if not selector:
        return False
    try:
        ipaddress.ip_address(selector)
        return True
    except ValueError:
        return False


@dataclass
class Device:
    name: str
    model: str
    host: str
    uuid: str
    cast: Any = None        # underlying pychromecast.Chromecast (None in tests)

    def as_dict(self) -> dict:
        return {"name": self.name, "model": self.model,
                "host": self.host, "uuid": self.uuid}

    @classmethod
    def from_cast(cls, cc: Any) -> "Device":
        ci = cc.cast_info
        return cls(
            name=getattr(ci, "friendly_name", "") or "",
            model=getattr(ci, "model_name", "") or "",
            host=str(getattr(ci, "host", "") or ""),
            uuid=str(getattr(ci, "uuid", "") or ""),
            cast=cc,
        )


def discover(timeout: float = 8.0) -> list[Device]:
    """Discover Chromecasts on the LAN. Returns [] if none are found."""
    import pychromecast

    log.debug("discovering chromecasts (timeout=%.1fs)", timeout)
    chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
    try:
        return [Device.from_cast(cc) for cc in chromecasts]
    finally:
        try:
            pychromecast.discovery.stop_discovery(browser)
        except Exception:  # pragma: no cover - best effort cleanup
            pass


def connect_host(host: str, *, port: int = DEFAULT_CAST_PORT,
                 timeout: float = 10.0) -> Device:
    """Connect directly to a Chromecast by IP, bypassing mDNS discovery.

    This is the reliable path on hosts where multicast is unavailable (many
    virtual interfaces, restrictive networks). The friendly name/model are
    filled in from the device once connected, falling back to the host.
    """
    import pychromecast

    log.debug("connecting directly to %s:%d", host, port)
    cc = pychromecast.get_chromecast_from_host(
        (host, port, None, None, None), timeout=timeout,
    )
    cc.wait(timeout=timeout)
    ci = cc.cast_info
    return Device(
        name=getattr(ci, "friendly_name", None) or host,
        model=getattr(ci, "model_name", None) or "",
        host=host,
        uuid=str(getattr(ci, "uuid", "") or ""),
        cast=cc,
    )


def select_device(found: list[Device], selector: str | None) -> Device:
    """Pick a device by name/IP, or the sole device when selector is None.

    Raises DeviceError (exit 4) when nothing matches or the choice is ambiguous.
    """
    if not found:
        raise DeviceError("no Chromecast devices found")

    if selector is None:
        if len(found) == 1:
            return found[0]
        names = ", ".join(d.name for d in found)
        raise DeviceError(
            f"multiple devices found ({names}); choose one with --device"
        )

    sel = selector.lower()
    for d in found:
        if d.name.lower() == sel or d.host == selector or d.uuid.lower() == sel:
            return d
    # Substring match on name as a convenience.
    matches = [d for d in found if sel in d.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise DeviceError(f"no device matching '{selector}'")
