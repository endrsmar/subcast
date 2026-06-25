"""Small networking helpers."""

from __future__ import annotations

import socket


def detect_lan_ip(target: str = "8.8.8.8") -> str:
    """Return the local IP the OS would use to reach ``target``.

    Uses a connected UDP socket (no packets are actually sent) so we get the
    address on the interface that routes toward the Chromecast, not loopback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip
