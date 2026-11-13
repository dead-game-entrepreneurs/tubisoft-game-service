"""Experimental GS NAT discovery through the client's real game socket.

XIII requests a token over the reliable control socket, sends that token from
the game socket, then receives its observed game endpoint on the control
socket. Only observed source endpoints are reported; this is not a relay.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import logging
import secrets
import time

from . import data, gsxor
from .frame import GSHeader, HEADER_SIZE
from .access import networks
from .srp_probe import HEADER, WINDOW, ACK, FIN, IDENT, SYN, SEQUENCE_MODULUS, finish_datagram, syn_reply

LOG = logging.getLogger("ubisoft.nat")
SESSION_SECONDS = 300
MAX_SESSIONS = 256
RETRY_SECONDS = 1
MAX_RETRIES = 10


@dataclass
class Request:
    peer: tuple[str, int]
    request_id: str
    token: bytes
    segment: int
    header: GSHeader
    port_sequence: int | None = None
    port_acknowledged: bool = False
    port_reply: bytes | None = None
    port_retry_at: float = 0
    port_retries: int = 0
    address_reply: bytes | None = None
    address_sequence: int | None = None
    acknowledged: bool = False
    address_retry_at: float = 0
    address_retries: int = 0


@dataclass
class Session:
    signature: int
    seed: int
    last_seen: float
    next_sequence: int = 1
    requests: dict[str, Request] = field(default_factory=dict)
    replies: dict[int, list[bytes]] = field(default_factory=dict)
    logged_packets: int = 0


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, allowed_cidr: str = "127.0.0.0/8", host_alias=None) -> None:
        self.allowed = networks(allowed_cidr)
        # Replies still go to the real source; only the reported address changes.
        self.host_alias = host_alias
        self.transport: asyncio.DatagramTransport | None = None
        self.sessions: dict[tuple[str, int], Session] = {}
        self.tokens: dict[bytes, Request] = {}
        self.cleanup: asyncio.TimerHandle | None = None
        self.retries: asyncio.TimerHandle | None = None
        self.port = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.port = transport.get_extra_info("sockname")[1]
        self.cleanup = asyncio.get_running_loop().call_later(30, self.expire)
        self.retries = asyncio.get_running_loop().call_later(RETRY_SECONDS, self.retry_pending)
        LOG.info("experimental NAT discovery listening at %s", transport.get_extra_info("sockname"))

    def connection_lost(self, exc: Exception | None) -> None:
        if self.cleanup is not None:
            self.cleanup.cancel()
        if self.retries is not None:
            self.retries.cancel()
        self.sessions.clear()
        self.tokens.clear()

    def remove(self, peer: tuple[str, int], *, reason: str = "closed") -> None:
        session = self.sessions.pop(peer, None)
        if session is not None:
            requests = list(session.requests.values())
            LOG.info("NAT control session ended peer=%s reason=%s requests=%d port_acks=%d game_endpoints=%d address_acks=%d",
                     peer, reason, len(requests), sum(r.port_acknowledged for r in requests),
                     sum(r.address_reply is not None for r in requests), sum(r.acknowledged for r in requests))
            for request in session.requests.values():
                self.tokens.pop(request.token, None)

    def expire(self) -> None:
        now = time.monotonic()
        for peer, session in list(self.sessions.items()):
            if now - session.last_seen > SESSION_SECONDS:
                self.remove(peer, reason="idle-timeout")
        self.cleanup = asyncio.get_running_loop().call_later(30, self.expire)

    def send(self, packet: bytes, peer: tuple[str, int]) -> None:
        if self.transport is not None:
            self.transport.sendto(packet, peer)

    def retry_pending(self) -> None:
        """Replay identical unacknowledged replies without extending session life."""
        if self.retries is not None:
            self.retries.cancel()
        now = time.monotonic()
        for peer, session in self.sessions.items():
            if now - session.last_seen > SESSION_SECONDS:
                continue
            for request in session.requests.values():
                # A correlated game probe proves the client consumed the port
                # reply, even when its separate transport ACK was lost.
                pending = (("port", "2", request.port_reply,
                            request.port_acknowledged or request.address_reply is not None,
                            request.port_sequence),
                           ("address", "3", request.address_reply,
                            request.acknowledged, request.address_sequence))
                for prefix, subtype, packet, acknowledged, sequence in pending:
                    attempts = getattr(request, prefix + "_retries")
                    deadline = getattr(request, prefix + "_retry_at")
                    if packet is None or acknowledged or attempts >= MAX_RETRIES or now < deadline:
                        continue
                    self.send(packet, peer)
                    attempts += 1
                    setattr(request, prefix + "_retries", attempts)
                    setattr(request, prefix + "_retry_at", now + RETRY_SECONDS)
                    LOG.info("NAT control reply retried peer=%s subtype=%s request=%s sequence=%d retry=%d",
                             peer, subtype, request.request_id, sequence, attempts)
        self.retries = asyncio.get_running_loop().call_later(RETRY_SECONDS, self.retry_pending)

    def reply(self, session: Session, request: Request, subtype: str, address: int, port: int) -> tuple[int, bytes]:
        payload = gsxor.encrypt(data.encode([subtype, [request.request_id, str(address), str(port)]]))
        header = request.header
        frame = GSHeader(HEADER_SIZE + len(payload), 0, header.priority, 0xdd,
                         header.receiver, header.sender).encode() + payload
        sequence = session.next_sequence
        session.next_sequence = (sequence + 1) % SEQUENCE_MODULUS
        packet = HEADER.pack(session.seed, session.signature, len(frame), IDENT | ACK,
                             sequence, request.segment) + frame
        packet = finish_datagram(packet)
        session.replies.setdefault(request.segment, []).append(packet)
        LOG.info("NAT control reply sent peer=%s subtype=%s request=%s bytes=%d sequence=%d ack=%d",
                 request.peer, subtype, request.request_id, len(packet), sequence, request.segment)
        return sequence, packet

    def probe(self, packet: bytes, peer: tuple[str, int]) -> None:
        request = self.tokens.get(packet)
        if request is None or request.peer[0] != peer[0]:
            LOG.info("NAT unmatched game probe peer=%s", peer)
            return
        session = self.sessions.get(request.peer)
        if session is None or time.monotonic() - session.last_seen > SESSION_SECONDS:
            return
        session.last_seen = time.monotonic()
        # Preserve the game's periodic probe/echo exchange, keeping its UDP
        # source port mapped even after the address lookup has completed.
        self.send(packet, peer)
        if request.address_reply is None:
            # The callback compares this native s_addr with inet_aton output.
            observed = self.host_alias(peer[0]) if self.host_alias is not None else peer[0]
            address = int.from_bytes(ipaddress.IPv4Address(observed).packed, "little")
            sequence, reply = self.reply(session, request, "3", address, peer[1])
            request.address_sequence = sequence
            request.address_reply = reply
            request.address_retry_at = time.monotonic() + RETRY_SECONDS
            LOG.info("game endpoint observed control_peer=%s game_peer=%s request=%s",
                     request.peer, peer, request.request_id)
        if not request.acknowledged:
            self.send(request.address_reply, request.peer)

    def datagram_received(self, packet: bytes, peer: tuple[str, int]) -> None:
        try:
            if not any(ipaddress.IPv4Address(peer[0]) in network for network in self.allowed):
                return
        except ipaddress.AddressValueError:
            return
        if len(packet) == 4:
            self.probe(packet, peer)
            return
        if not HEADER.size <= len(packet) <= 2048:
            return
        _, _, size, flags, segment, ack = HEADER.unpack_from(packet)
        if flags & IDENT != IDENT or size > len(packet) - HEADER.size:
            return
        if flags & FIN:
            self.remove(peer, reason="peer-fin")
            return
        if flags & SYN:
            if peer not in self.sessions and len(self.sessions) >= MAX_SESSIONS:
                return
            try:
                response = syn_reply(packet)
            except ValueError:
                return
            self.remove(peer, reason="new-handshake")
            _, signature, seed, _ = WINDOW.unpack_from(packet, HEADER.size)
            self.sessions[peer] = Session(signature, seed, time.monotonic())
            self.send(response, peer)
            LOG.info("NAT control handshake peer=%s", peer)
            return
        session = self.sessions.get(peer)
        if session is None:
            return
        if session.logged_packets < 16:
            LOG.info("NAT control packet peer=%s bytes=%d payload=%d flags=0x%04x sequence=%d ack=%d",
                     peer, len(packet), size, flags, segment, ack)
            session.logged_packets += 1
        session.last_seen = time.monotonic()
        if flags & ACK:
            for request in session.requests.values():
                if request.port_sequence == ack and not request.port_acknowledged:
                    request.port_acknowledged = True
                    LOG.info("NAT port reply acknowledged peer=%s request=%s sequence=%d", peer, request.request_id, ack)
                if request.address_sequence == ack and not request.acknowledged:
                    request.acknowledged = True
                    LOG.info("NAT address reply acknowledged peer=%s request=%s sequence=%d", peer, request.request_id, ack)
        if size == 0:
            if not flags & ACK:
                response = HEADER.pack(session.seed, session.signature, 0, IDENT | ACK, 0xffff, segment)
                self.send(finish_datagram(response), peer)
            return
        if segment in session.replies:
            for response in session.replies[segment]:
                self.send(response, peer)
            return
        if len(session.replies) >= 256:
            return
        try:
            frame = packet[HEADER.size:HEADER.size + size]
            header = GSHeader.decode(frame[:HEADER_SIZE])
            if header.message_type != 0xdd or header.property != 0 or header.size != len(frame):
                return
            fields = data.decode(gsxor.decrypt(frame[HEADER_SIZE:]))
            if (len(fields) != 2 or fields[0] != "1" or not isinstance(fields[1], list)
                    or len(fields[1]) != 4
                    or not all(isinstance(item, str) and len(item) <= 132 for item in fields[1])):
                return
            request_id = fields[1][0]
            if not request_id.isascii() or not request_id.isdecimal() or not 0 <= int(request_id) <= 255:
                return
        except (ValueError, IndexError):
            return
        previous = session.requests.get(request_id)
        if previous is not None:
            self.tokens.pop(previous.token, None)
        token = secrets.token_bytes(4)
        while token in self.tokens:
            token = secrets.token_bytes(4)
        request = Request(peer, request_id, token, segment, header)
        session.requests[request_id] = request
        self.tokens[token] = request
        # 0x01d8ef90 converts this value to network bytes and asks the game
        # socket to send those four bytes to the advertised NAT service port.
        sequence, response = self.reply(session, request, "2", int.from_bytes(token, "big"), self.port)
        request.port_sequence = sequence
        request.port_reply = response
        request.port_retry_at = time.monotonic() + RETRY_SECONDS
        self.send(response, peer)
        LOG.info("NAT game-socket probe requested peer=%s request=%s", peer, request_id)
