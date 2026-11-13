"""Experimental bounded GS UDP handshake probe.

The 12-byte header and SYN response are comparison-based candidates, not a
verified implementation of XIII's complete reliable datagram transport.
See https://github.com/michal-kapala/ubi-gs/blob/master/srp.py.
"""
from __future__ import annotations

from .games.base import GameProfile
from .games.xiii import PROFILE as XIII

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field

from . import data, gsxor, rooms
from .frame import GSHeader, HEADER_SIZE

LOG = logging.getLogger("ubisoft.srp_probe")
HEADER = struct.Struct("<6H")
WINDOW = struct.Struct("<4H")
SYN, ACK, FIN, IDENT = 2, 4, 1, 0x3040
# The client may be quiet while a match loads. This is still only a fallback;
# proper room liveness must be confirmed from update/heartbeat behavior.
ACTIVE_IDLE_SECONDS = 900
SEQUENCE_MODULUS = 20  # 0x01d52ee0 doubles the advertised ten-packet window.


def checksum(packet: bytes) -> int:
    """Match 0x01d520a0, including its signed first byte on odd packets."""
    total = int.from_bytes(packet[:1], "little", signed=True) if len(packet) & 1 else 0
    start = len(packet) & 1
    for index in range(start, len(packet), 2):
        total += int.from_bytes(packet[index:index + 2], "little")
    total &= 0xffffffff
    total = (total & 0xffff) + (total >> 16)
    total += total >> 16
    return (~total) & 0xffff


def finish_datagram(packet: bytes) -> bytes:
    """Pad outside the declared payload before checksumming for the PS2 EE.

    The retail checksum's odd-length branch advances its pointer by one,
    then executes LHU at 0x01d520f4. With an aligned receive buffer this is
    an unaligned load. The receiver copies only the header's declared data
    length (0x01d52dd0), so an external zero byte preserves the GS message.
    """
    if len(packet) < HEADER.size:
        raise ValueError("SRP datagram is shorter than its header")
    if len(packet) & 1:
        packet += b"\0"
    return struct.pack("<H", checksum(packet)) + packet[2:]


def syn_reply(packet: bytes) -> bytes:
    if len(packet) != HEADER.size + WINDOW.size:
        raise ValueError("unsupported SYN size")
    _, _, size, flags, segment, _ = HEADER.unpack_from(packet)
    if size != WINDOW.size or flags & (SYN | IDENT) != (SYN | IDENT):
        raise ValueError("not a supported SYN")
    _, sender_signature, seed, _ = WINDOW.unpack_from(packet, HEADER.size)
    body = WINDOW.pack(10, 2, 0, 0x218)
    reply = HEADER.pack(seed, sender_signature, len(body), IDENT | SYN | ACK, 0, segment) + body
    return finish_datagram(reply)


@dataclass
class Session:
    signature: int
    seed: int
    next_segment: int = 1
    last_segment: int | None = None
    last_reply: bytes | None = None
    last_seen: float = 0
    room: rooms.Room | None = None
    notifications: dict[int, tuple[bytes, int, float]] = field(default_factory=dict)


class Probe(asyncio.DatagramProtocol):
    def __init__(self, pending_handoffs: dict[tuple[str, str], float], registry: rooms.RoomRegistry,
                 profile: GameProfile = XIII, host_alias=None):
        self.pending_handoffs = pending_handoffs
        # TCP handoffs and rooms use the aliased address; UDP replies still
        # go to the datagram's real source.
        self.host_alias = host_alias
        self.registry = registry
        self.profile = profile
        self.transport: asyncio.DatagramTransport | None = None
        self.sessions: dict[tuple[str, int], Session] = {}
        self.cleanup: asyncio.TimerHandle | None = None
        self.retries: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.registry.add_remove_listener(self.room_removed)
        LOG.info("experimental UDP probe listening at %s", transport.get_extra_info("sockname"))
        self.cleanup = asyncio.get_running_loop().call_later(30, self.expire_sessions)
        self.retries = asyncio.get_running_loop().call_later(1, self.retry_notifications)

    def connection_lost(self, exc: Exception | None) -> None:
        self.registry.remove_remove_listener(self.room_removed)
        if self.cleanup is not None:
            self.cleanup.cancel()
        if self.retries is not None:
            self.retries.cancel()
        for peer in list(self.sessions):
            self.remove_session(peer)

    def room_removed(self, room: rooms.Room) -> None:
        # A room can close through its owner's TCP membership connection,
        # independently of UDP FIN. Discard its transport and retry state too.
        for peer, session in list(self.sessions.items()):
            if session.room is room:
                self.sessions.pop(peer, None)
                LOG.info("UDP host transport cleared for closed room peer=%s room=%d", peer, room.room_id)

    def remove_session(self, peer: tuple[str, int]) -> None:
        session = self.sessions.pop(peer, None)
        if session is not None and session.room is not None:
            room = session.room
            self.registry.remove(room.room_id, room.owner, room.peer_ip)
            LOG.info("UDP host session ended; room removed id=%d", room.room_id)

    def expire_sessions(self) -> None:
        now = time.monotonic()
        self.registry.expire_pending(now)
        for peer, session in list(self.sessions.items()):
            if now - session.last_seen > ACTIVE_IDLE_SECONDS:
                self.remove_session(peer)
        self.cleanup = asyncio.get_running_loop().call_later(30, self.expire_sessions)

    def send_message(self, peer: tuple[str, int], session: Session, ack: int, frame: bytes) -> None:
        if session.next_segment in session.notifications:
            return  # A retried request can resume once the ring slot is ACKed.
        packet = HEADER.pack(session.seed, session.signature, len(frame), IDENT | ACK,
                             session.next_segment, ack) + frame
        packet = finish_datagram(packet)
        session.next_segment = (session.next_segment + 1) % SEQUENCE_MODULUS
        session.last_segment = ack
        session.last_reply = packet
        if self.transport is not None:
            self.transport.sendto(packet, peer)

    def retry_notifications(self) -> None:
        now = time.monotonic()
        for peer, session in list(self.sessions.items()):
            for sequence, (packet, attempts, sent_at) in list(session.notifications.items()):
                if now - sent_at < 1:
                    continue
                if attempts >= 8:
                    session.notifications.pop(sequence, None)
                    LOG.info("UDP host notification expired peer=%s sequence=%d", peer, sequence)
                    continue
                if self.transport is not None:
                    self.transport.sendto(packet, peer)
                session.notifications[sequence] = (packet, attempts + 1, now)
        self.retries = asyncio.get_running_loop().call_later(1, self.retry_notifications)

    def notify_host(self, room: rooms.Room, events: list[list]) -> bool:
        """Send ordered GS events on the owning host's reliable UDP channel."""
        if room.product != self.profile.product:
            return False
        for peer, session in self.sessions.items():
            if session.room is not room or self.registry.active_room(room.room_id) is not room:
                continue
            frames = bytearray()
            for event in events:
                payload = gsxor.encrypt(data.encode(event))
                frames.extend(GSHeader(HEADER_SIZE + len(payload), 0, 1, 0xd1, 2, 4).encode())
                frames.extend(payload)
            # Fragmentation is not implemented; never send oversized GS data.
            if len(frames) > 536 or session.next_segment in session.notifications:
                return False
            sequence = session.next_segment
            packet = HEADER.pack(session.seed, session.signature, len(frames), IDENT,
                                 sequence, 0xffff) + frames
            packet = finish_datagram(packet)
            session.next_segment = (sequence + 1) % SEQUENCE_MODULUS
            session.notifications[sequence] = (packet, 1, time.monotonic())
            if self.transport is not None:
                self.transport.sendto(packet, peer)
            LOG.info("UDP host notifications sent peer=%s room=%d subtypes=%s sequence=%d",
                     peer, room.room_id, [event[0] for event in events], sequence)
            return True
        return False

    def member_info(self, room: rooms.Room, username: str, ip: str, info: bytes, *, new: bool) -> bool:
        if (ip, username.casefold()) == (room.peer_ip, room.owner):
            return False
        return self.notify_host(room, room.profile.member_events(room, username, ip, info, new=new))

    def send_ack(self, peer: tuple[str, int], session: Session, segment: int) -> None:
        # XIII's 0x01d52140 constructs this pure-ACK form: empty body,
        # flags 0x3044, sequence 0xffff, acknowledged peer sequence at +10.
        # Control ACKs do not consume a server data sequence or replace the
        # cached GS reply needed when a data segment is retransmitted.
        packet = HEADER.pack(session.seed, session.signature, 0, IDENT | ACK, 0xffff, segment)
        packet = finish_datagram(packet)
        if self.transport is not None:
            self.transport.sendto(packet, peer)

    def host_operation(self, session: Session, fields: list) -> list | None:
        if session.room is None or session.room.product != self.profile.product:
            return None
        return session.room.profile.host_operation(session.room, fields)

    def datagram_received(self, packet: bytes, peer: tuple[str, int]) -> None:
        session = self.sessions.get(peer)
        source_ip = self.host_alias(peer[0]) if self.host_alias is not None else peer[0]
        if session is None and not any(ip == source_ip and expiry > time.monotonic()
                                       for (ip, _), expiry in self.pending_handoffs.items()):
            return
        if not HEADER.size <= len(packet) <= 2048:
            LOG.info("UDP ignored peer=%s size=%d", peer, len(packet))
            return
        _, _, size, flags, segment, ack = HEADER.unpack_from(packet)
        LOG.info("UDP peer=%s size=%d declared=%d flags=0x%04x segment=%d ack=%d",
                 peer, len(packet), size, flags, segment, ack)
        if size > len(packet) - HEADER.size:
            LOG.info("UDP rejected peer=%s: unexpected length", peer)
            return
        if flags & SYN:
            try:
                reply = syn_reply(packet)
            except ValueError:
                LOG.info("UDP SYN peer=%s did not match candidate layout", peer)
                return
            if self.transport is not None:
                self.transport.sendto(reply, peer)
            _, signature, seed, _ = WINDOW.unpack_from(packet, HEADER.size)
            self.sessions[peer] = Session(signature, seed, last_seen=time.monotonic())
            LOG.info("candidate UDP SYN-ACK sent peer=%s", peer)
            return
        if session is None:
            return
        session.last_seen = time.monotonic()
        if flags & ACK and ack in session.notifications:
            session.notifications.pop(ack)
            LOG.info("UDP host notification acknowledged peer=%s sequence=%d", peer, ack)
        if flags & FIN:
            self.send_ack(peer, session, segment)
            self.remove_session(peer)
            return
        if size == 0:
            if flags & IDENT == IDENT and not flags & ACK:
                self.send_ack(peer, session, segment)
                LOG.info("UDP empty segment acknowledged peer=%s segment=%d", peer, segment)
            return
        if size >= HEADER_SIZE:
            try:
                frame = packet[HEADER.size:HEADER.size + size]
                header = GSHeader.decode(frame[:HEADER_SIZE])
                LOG.info("UDP GS peer=%s type=0x%02x property=%d size=%d", peer,
                         header.message_type, header.property, header.size)
                if header.property == 0 and header.size == len(frame):
                    fields = data.decode(gsxor.decrypt(frame[HEADER_SIZE:]))
                    LOG.info("UDP GS structure peer=%s count=%d field_types=%s", peer,
                             len(fields), [type(field).__name__ for field in fields])
                    if session.last_segment == segment and session.last_reply is not None:
                        if self.transport is not None:
                            self.transport.sendto(session.last_reply, peer)
                        return
                    if (header.message_type == 0xd2 and len(fields) == 2
                            and all(isinstance(field, str) for field in fields)
                            and fields[1].isascii() and fields[1].isdecimal()):
                        owner = fields[0].casefold()
                        key = (source_ip, owner)
                        if self.pending_handoffs.get(key, 0) <= time.monotonic():
                            LOG.info("UDP host login rejected peer=%s: no pending handoff", peer)
                            return
                        room = self.registry.activate(int(fields[1]), owner, source_ip, self.profile.product)
                        if room is None:
                            LOG.info("UDP host login rejected peer=%s: room ownership mismatch", peer)
                            return
                        self.pending_handoffs.pop(key, None)
                        session.room = room
                        response = gsxor.encrypt(data.encode(["210", ["1", str(room.room_id)]]))
                        response_header = GSHeader(HEADER_SIZE + len(response), 0, header.priority,
                                                   0x26, header.receiver, header.sender).encode()
                        self.send_message(peer, session, segment, response_header + response)
                        LOG.info("experimental UDP host login accepted peer=%s room=%d", peer, room.room_id)
                    elif header.message_type == 0xd1 and session.room is not None:
                        response_fields = self.host_operation(session, fields)
                        subtype = fields[0] if fields and isinstance(fields[0], str) else "invalid"
                        if response_fields is None:
                            LOG.info("unsupported UDP host operation peer=%s subtype=%s", peer, subtype[:16])
                            return
                        response = gsxor.encrypt(data.encode(response_fields))
                        response_header = GSHeader(HEADER_SIZE + len(response), 0, header.priority,
                                                   0x26, header.receiver, header.sender).encode()
                        self.send_message(peer, session, segment, response_header + response)
                        LOG.info("experimental UDP host operation accepted peer=%s room=%d subtype=%s",
                                 peer, session.room.room_id, subtype)
            except (ValueError, IndexError):
                LOG.info("UDP payload peer=%s not decoded", peer)
