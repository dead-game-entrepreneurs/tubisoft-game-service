"""Opt-in opaque UDP forwarding for authenticated 187 room members.

Each UDP socket represents one player to the others. Source mappings are
proved through GS NAT tokens and then confirmed on that player's signed-in
lobby connection. Native game datagrams are never decoded or rewritten.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import logging
import struct
import time

from .games.rod import PlayerInfo

LOG = logging.getLogger("ubisoft.rod_relay")
MAX_PACKET = 2048
PENDING_SECONDS = 30
REUSE_SECONDS = 5
PACKETS_PER_SECOND = 600
STARTUP_SECONDS = 5
STARTUP_PACKETS = 8
PRODUCT = "RODPS2US"
SDK = "RODPS21.0"


@dataclass(frozen=True)
class Config:
    bind: str
    address: str
    first: int
    last: int


def configuration(env, address, bindings, reserved):
    mode = env.get("TUBISOFT_ROD_RELAY_MODE", "direct")
    if mode == "direct":
        return None
    if mode != "relay" or not any(b.profile.slug == "rod" for b in bindings):
        raise ValueError("187 relay requires mode relay and enabled rod profile")
    bind = str(ipaddress.IPv4Address(env.get("TUBISOFT_ROD_RELAY_BIND_IP", env.get("TUBISOFT_BIND_IP", "0.0.0.0"))))
    public = ipaddress.IPv4Address(env.get("TUBISOFT_ROD_RELAY_ADVERTISE_IP", address))
    first = int(env.get("TUBISOFT_ROD_RELAY_FIRST_PORT", 30400))
    last = int(env.get("TUBISOFT_ROD_RELAY_LAST_PORT", 30431))
    if public.is_unspecified or public.is_multicast or public.is_loopback or int(public) == 0xffffffff:
        raise ValueError("187 relay requires a reachable unicast address")
    if not 1024 <= first < last <= 65535 or last - first + 1 > 128:
        raise ValueError("187 relay pool must contain 2 to 128 unprivileged ports")
    used = set(reserved)
    for binding in bindings:
        used.update((binding.router_port, binding.launcher_port, binding.nat_port))
    if any(first <= port <= last for port in used):
        raise ValueError("187 relay pool overlaps a service port")
    return Config(bind, str(public), first, last)


@dataclass
class Lease:
    member: object
    endpoint: tuple[str, int]
    port: int
    created: float
    confirmed: bool = False
    tokens: float = PACKETS_PER_SECOND
    budget_at: float = 0
    packets: int = 0
    bytes: int = 0


class Port(asyncio.DatagramProtocol):
    def __init__(self, relay, port):
        self.relay, self.port = relay, port

    def datagram_received(self, packet, peer):
        self.relay.forward(self.port, packet, peer)


class Relay:
    def __init__(self, config, registry, host_alias=None, *, now=time.monotonic):
        self.config, self.registry, self.host_alias, self.now = config, registry, host_alias, now
        self.transports = {}
        self.leases = {}
        self.reuse_after = {}
        self.opened = set()
        self.pending = {}
        self.cleanup = None
        self.closed = False

    async def start(self):
        try:
            loop = asyncio.get_running_loop()
            for port in range(self.config.first, self.config.last + 1):
                transport, _ = await loop.create_datagram_endpoint(
                    lambda port=port: Port(self, port), local_addr=(self.config.bind, port))
                self.transports[port] = transport
            self.cleanup = loop.call_later(5, self.sweep)
        except BaseException:
            self.close()
            raise
        LOG.info("187 forced relay listening address=%s ports=%d-%d", self.config.address,
                 self.config.first, self.config.last)

    def close(self):
        self.closed = True
        if self.cleanup is not None:
            self.cleanup.cancel()
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()
        self.leases.clear()
        self.opened.clear()
        self.pending.clear()

    def _forget_paths(self, port):
        self.opened = {path for path in self.opened if port not in path}
        self.pending.pop(port, None)
        for source, held in list(self.pending.items()):
            self.pending[source] = [item for item in held if item[0] != port]

    def membership_changed(self, member):
        for port, lease in self.leases.items():
            if lease.member is member:
                self._forget_paths(port)

    def _live(self, member):
        return any(candidate is member for candidate in self.registry.lobby_members(PRODUCT))

    def _retire(self, port):
        lease = self.leases.pop(port)
        self._forget_paths(port)
        self.reuse_after[port] = self.now() + REUSE_SECONDS
        LOG.info("187 relay lease released port=%d packets=%d bytes=%d", port, lease.packets, lease.bytes)

    def sweep(self):
        now = self.now()
        for port, held in list(self.pending.items()):
            remaining = [item for item in held if item[2] > now]
            if remaining:
                self.pending[port] = remaining
            else:
                del self.pending[port]
        for port, lease in list(self.leases.items()):
            if not self._live(lease.member) or (not lease.confirmed and now - lease.created > PENDING_SECONDS):
                self._retire(port)
        if not self.closed and self.transports:
            if self.cleanup is not None:
                self.cleanup.cancel()
            self.cleanup = asyncio.get_running_loop().call_later(5, self.sweep)

    def observe(self, request, endpoint):
        """NAT-token proof creates a pending lease; TCP publication admits it."""
        if self.closed or request.product != PRODUCT or request.sdk != SDK:
            return None
        address = self.host_alias(endpoint[0]) if self.host_alias is not None else endpoint[0]
        members = [m for m in self.registry.lobby_members(PRODUCT)
                   if m.address == address and m.name.casefold() == request.username.casefold()]
        if len(members) != 1 or address == self.config.address:
            # Equal public addresses trigger the native same-NAT/3658 branch.
            # Local operators need a separate reachable relay IP alias.
            return None
        member = members[0]
        self.sweep()
        for lease in self.leases.values():
            if lease.member is member and lease.endpoint == endpoint:
                return self.config.address, lease.port
        now = self.now()
        available = next((p for p in self.transports
                          if p not in self.leases and self.reuse_after.get(p, 0) <= now), None)
        if available is None:
            LOG.info("187 relay capacity exhausted; mapping declined")
            return None
        self.leases[available] = Lease(member, endpoint, available, now, budget_at=now)
        return self.config.address, available

    def confirm(self, member, info):
        parsed = PlayerInfo.decode(info)
        lease = self.leases.get(parsed.port)
        self_view = (lease is not None and lease.confirmed
                     and str(parsed.address) == member.address)
        if (lease is None or lease.member is not member or not self._live(member)
                or not parsed.nat or (str(parsed.address) != self.config.address and not self_view)
                or (not lease.confirmed and self.now() - lease.created > PENDING_SECONDS)):
            raise ValueError("187 relay endpoint has no matching NAT proof")
        for port, previous in list(self.leases.items()):
            if previous.member is member and previous is not lease and previous.confirmed:
                self._retire(port)
        if not lease.confirmed:
            lease.confirmed = True
            LOG.info("187 relay mapping confirmed port=%d", lease.port)
        canonical = bytearray(info)
        canonical[4:8] = int(ipaddress.IPv4Address(self.config.address)).to_bytes(4, "little")
        return bytes(canonical)

    def room(self, room):
        lease = next((l for l in self.leases.values() if l.confirmed and self._live(l.member)
                      and (l.member.address, l.member.name.casefold()) == (room.peer_ip, room.owner)), None)
        if lease is None:
            raise ValueError("187 relay room owner has no confirmed endpoint")
        info = bytearray(room.info)
        struct.pack_into("<H", info, 32, lease.port)
        room.info, room.game_port = bytes(info), str(lease.port)
        room.public_address = room.local_ip = self.config.address

    def member_event(self, event, viewer):
        # Each client keeps its real self address for the game's same-NAT
        # comparison. Other members appear at the relay, matching UDP sources
        # and the native member-to-address callback 0x0041f270.
        if event[0] != "50":
            return event
        fields = list(event[1])
        if fields[0].casefold() == viewer.casefold():
            if not fields[5]:
                return event
            parsed = PlayerInfo.decode(fields[5])
            lease = self.leases.get(parsed.port)
            if lease is None or not lease.confirmed or lease.member.name.casefold() != viewer.casefold():
                return event
            # 0x00463560 compares the SDK's NAT words, including self. Equal
            # relay IPs make it announce a direct local-IP:3658 endpoint after
            # joining. Give self its real address for that comparison; other
            # members keep the canonical relay mapping. Preserve the relay
            # port used by native room creation and later player publication.
            info = bytearray(fields[5])
            info[4:8] = int(ipaddress.IPv4Address(lease.member.address)).to_bytes(4, "little")
            fields[5] = bytes(info)
            return ["50", fields]
        fields[3] = fields[4] = self.config.address
        return ["50", fields]

    def forward(self, port, packet, peer):
        destination = self.leases.get(port)
        if self.closed or destination is None or not destination.confirmed or len(packet) > MAX_PACKET:
            return
        live = {id(m) for m in self.registry.lobby_members(PRODUCT)}
        if id(destination.member) not in live:
            return
        sources = [l for l in self.leases.values() if l.confirmed and l.endpoint == peer
                   and l is not destination and id(l.member) in live]
        if len(sources) != 1:
            return
        source = sources[0]
        if not self._same_room(source, destination):
            return
        now = self.now()
        source.tokens = min(PACKETS_PER_SECOND, source.tokens + max(0, now - source.budget_at) * PACKETS_PER_SECOND)
        source.budget_at = now
        if source.tokens < 1:
            return
        source.tokens -= 1
        # The NAT probe opened a path to the discovery server, not to this
        # peer's relay socket. PCSX2's sockets backend (and filtering NATs)
        # admit that relay source only after a native outbound peer packet.
        # Hold a bounded initial burst until the reverse path is open.
        path = (source.port, destination.port)
        newly_opened = path not in self.opened
        self.opened.add(path)
        if (destination.port, source.port) in self.opened:
            self._send(source, destination, packet)
        else:
            held = [item for item in self.pending.get(source.port, []) if item[2] > now]
            if len(held) < STARTUP_PACKETS:
                held.append((destination.port, packet, now + STARTUP_SECONDS))
            self.pending[source.port] = held
        held = self.pending.pop(destination.port, [])
        remaining = []
        flushed = 0
        for target, payload, expires in held:
            if expires <= now:
                continue
            if target == source.port:
                self._send(destination, source, payload)
                flushed += 1
            else:
                remaining.append((target, payload, expires))
        if remaining:
            self.pending[destination.port] = remaining
        if newly_opened and (destination.port, source.port) in self.opened:
            LOG.info("187 relay peer paths open ports=%d,%d queued=%d", source.port, destination.port, flushed)

    def _same_room(self, source, destination):
        group = source.member.group
        if group != destination.member.group or not group.startswith("-"):
            return False
        room = self.registry.active_room(int(group), PRODUCT)
        identities = {(l.member.address, l.member.name.casefold()) for l in (source, destination)}
        return room is not None and identities <= room.members

    def _send(self, source, destination, packet):
        # Socket i represents player i. Sending from the source's socket
        # preserves the endpoint the receiver opened through the GS member data.
        self.transports[source.port].sendto(packet, destination.endpoint)
        source.packets += 1
        source.bytes += len(packet)
