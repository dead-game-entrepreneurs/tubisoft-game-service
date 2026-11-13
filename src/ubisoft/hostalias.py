"""Report the container host's own clients at the advertised address.

When the service runs in a Docker bridge network, a game client on the Docker
host itself reaches the published ports through the bridge gateway
address. That address is meaningless to other players: it was
embedded in host advertisements and NAT replies, so a second PC could not
reach a game hosted on the server machine. Connections from the gateway are
the host machine, which other players reach at the advertised address.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path


def default_gateway(route_table: str) -> str | None:
    """Parse /proc/net/route (little-endian hex) for the IPv4 default gateway."""
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "00000000":
            try:
                gateway = ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, "little"))
            except ValueError:
                continue
            if not gateway.is_unspecified:
                return str(gateway)
    return None


class HostAlias:
    def __init__(self, gateway: str | None, advertised: str) -> None:
        self.gateway = gateway
        self.advertised = advertised

    def __call__(self, address: str) -> str:
        return self.advertised if self.gateway is not None and address == self.gateway else address


def configure(setting: str, advertised: str, *, route_path: str = "/proc/net/route",
              container_marker: str = "/.dockerenv") -> HostAlias | None:
    """`auto` aliases only inside a container; `off` disables; an IPv4 is explicit."""
    setting = setting.strip().lower()
    if setting == "off":
        return None
    if setting == "auto":
        if not os.path.exists(container_marker):
            return None
        try:
            gateway = default_gateway(Path(route_path).read_text())
        except OSError:
            return None
        return HostAlias(gateway, advertised) if gateway else None
    return HostAlias(str(ipaddress.IPv4Address(setting)), advertised)
