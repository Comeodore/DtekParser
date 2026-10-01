"""Wake-on-LAN for Home Assistant's rest_command.wake_pc (POST /api/v1/wol).

HA's script.wake_pc and rest_command.wake_pc live in its YAML config, which
can't be changed through the HA API, so the endpoint stays here. The
container runs on the host network so the broadcast reaches the LAN.
"""
from __future__ import annotations

import re
import socket


def magic_packet(mac: str) -> bytes:
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(digits) != 12:
        raise ValueError(f"invalid MAC address {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(digits) * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
