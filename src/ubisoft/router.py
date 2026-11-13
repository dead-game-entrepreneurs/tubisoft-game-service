"""Experimental GS router: public-key handshake and bounded metadata logs.

This service provides experimental local account, lobby and room-registration
handlers. Their wire compatibility beyond observed flows remains unverified.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import accounts, crypto, data, gsxor, nat, proxy, rooms, srp_probe
from .bootstrap import port_number
from .frame import GSHeader, HEADER_SIZE, read_frame
from .games.base import GameProfile
from .games.r6_ladder import ranking_filters
from .games.xiii import PROFILE as XIII
from .access import AccessPolicy

LOG = logging.getLogger("ubisoft.router")
KEY_EXCHANGE = 0xDB
PARENT_LOBBY = XIII.parent_lobby()  # Compatibility for existing protocol fixtures.
# Notifications are written to other players' connections, which the sending
# handler does not drain. A recipient whose unsent bytes would exceed this
# bound has stopped reading: it is disconnected instead of growing the
# shared process's memory (normal pushes are well under 1 KiB each).
NOTIFICATION_BACKLOG_LIMIT = 256 * 1024


def response_header(request: GSHeader, payload: bytes, message_type: int | None = None) -> bytes:
    return GSHeader(HEADER_SIZE + len(payload), 0, request.priority,
                    request.message_type if message_type is None else message_type,
                    request.receiver, request.sender).encode()


def make_frame(request: GSHeader, fields: list) -> bytes:
    payload = gsxor.encrypt(data.encode(fields))
    return response_header(request, payload) + payload


def queue_notification(writer, frame: bytes, peer, kind: str) -> bool:
    """Queue an unsolicited frame for a connection, bounding its backlog."""
    transport = writer.transport
    if transport is None or transport.is_closing():
        LOG.info("%s notification skipped peer=%s: connection already closed", kind, peer)
        return False
    backlog = transport.get_write_buffer_size()
    if backlog + len(frame) > NOTIFICATION_BACKLOG_LIMIT:
        LOG.warning("%s notification backlog exceeded peer=%s queued=%d limit=%d; disconnecting",
                    kind, peer, backlog, NOTIFICATION_BACKLOG_LIMIT)
        transport.abort()
        return False
    try:
        writer.write(frame)
    except (ConnectionError, OSError):
        LOG.info("%s notification skipped peer=%s: connection already closed", kind, peer)
        return False
    return True


def value_shape(value, depth: int = 0, limit: int = 16) -> str:
    # Structure of a GS value for logs: decimal strings are shown, other
    # strings and binary values only by length, so names never appear.
    if isinstance(value, list):
        if depth >= 4:
            return f"list{len(value)}"
        items = ",".join(value_shape(item, depth + 1, limit) for item in value[:limit])
        return f"[{items}{',...' if len(value) > limit else ''}]"
    if isinstance(value, bytes):
        return f"bin{len(value)}"
    text = value[1:] if value.startswith("-") else value
    if text.isascii() and text.isdigit() and len(text) <= 10:
        return value
    return f"str{len(value)}"


def room_number(text: object) -> int | None:
    """A room ID as written by the games: decimal, optionally negative."""
    if not isinstance(text, str) or not text.isascii() or len(text) > 11:
        return None
    digits = text[1:] if text.startswith("-") else text
    return int(text) if digits.isdecimal() else None


def load_key(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 512:
        raise ValueError("router requires a 512-bit RSA private key")
    return key


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 private_key: rsa.RSAPrivateKey, timeout: float, account_db: Path,
                 wait_host: str, wait_port: int,
                 pending_handoffs: dict[tuple[str, str], float],
                 authenticated: dict[tuple[str, str], float],
                 registry: rooms.RoomRegistry,
                 host_probe: srp_probe.Probe | None = None,
                 access_policy: AccessPolicy | None = None,
                 game_profile: GameProfile = XIII,
                 proxy_handoffs: proxy.Handoffs | None = None,
                 ranking_store=None, host_alias=None) -> None:
    peer = writer.get_extra_info("peername")
    if host_alias is not None:
        # Room and handoff addresses must be reachable by other players.
        peer = (host_alias(peer[0]), *peer[1:])
    local = writer.get_extra_info("sockname")
    LOG.info("connected game=%s peer=%s local=%s", game_profile.slug, peer, local)
    game_public: crypto.GSPublicKey | None = None
    client_key: bytes | None = None
    server_key: bytes | None = None
    logged_in_user: str | None = None
    wait_user: str | None = None
    lobby_logged_in = False
    joined_parent = False
    parent_info: bytes | None = None
    friends_logged_in = False
    proxy_user: str | None = None
    proxy_ready = False
    proxy_directory = proxy.Directory(game_profile, wait_host, wait_port)
    proxy_handoffs = proxy_handoffs if proxy_handoffs is not None else proxy.Handoffs()
    host_room: rooms.Room | None = None
    host_connection = object()
    joined_room: rooms.Room | None = None
    join_endpoint_sent = False
    host_member_notified = False
    closed_room_id: int | None = None

    def wait_push(message_type: int, fields: list) -> None:
        # Unsolicited wait-module message (server 1 -> client 4).
        payload = gsxor.encrypt(data.encode(fields))
        queue_notification(writer, GSHeader(HEADER_SIZE + len(payload), 0, 1, message_type, 1, 4).encode() + payload,
                           peer, "friends")

    def release_friend_channel() -> None:
        if wait_user is not None:
            channel = registry.friend_channels.get(wait_user.casefold())
            if channel is not None and channel[0] is host_connection:
                del registry.friend_channels[wait_user.casefold()]
                registry.friend_lists.pop(wait_user.casefold(), None)
                notify_presence(wait_user)

    def message_time() -> str:
        # 0x003fef10 parses fixed fields: year-1900 (3 digits), month-1,
        # day, hour, minute and second (2 digits each); 0x005146c0 shows
        # them as "Y-M-D h:mm GMT".
        now = time.gmtime()
        return (f"{now.tm_year - 1900:03d}{now.tm_mon - 1:02d}{now.tm_mday:02d}"
                f"{now.tm_hour:02d}{now.tm_min:02d}{now.tm_sec:02d}")

    def presence(name: str) -> tuple[int, str]:
        # Friends UI 0x004e0700: 0 online, 1 offline, 9 in game (with the
        # product, compared by 0x003ff1c0).
        for member in registry.lobby_members(game_profile.product):
            if member.name.casefold() == name.casefold():
                return (0, "") if member.group == "1" else (9, game_profile.product)
        if name.casefold() in registry.friend_channels:
            # Signed in to the friends service but not in the game lobby.
            return 0, ""
        return 1, ""

    def notify_presence(name: str) -> None:
        # 0x003fd0d0 drops an entry with the same name before inserting, so a
        # new 0x4a record updates a watcher's list in place.
        if not game_profile.lobby_members:
            return
        key = name.casefold()
        sent = 0
        for watcher, friends in list(registry.friend_lists.items()):
            entry = friends.get(key)
            channel = registry.friend_channels.get(watcher)
            if entry is None or channel is None:
                continue
            channel[1](0x4a, friend_record(*entry))
            sent += 1
        if sent:
            LOG.info("friend presence update game=%s peer=%s status=%d watchers=%d",
                     game_profile.slug, peer, presence(name)[0], sent)

    def friend_record(name: str, group: str) -> list:
        # Pushed 0x4a (reader 0x00413bb0 -> 0x003fdd90): name, u32 status,
        # group, two u32 values and a product string.
        status, product = presence(name)
        return [name, status.to_bytes(4, "little"), group, bytes(4), bytes(4), product]

    def parent_record(joining: bool = False) -> list:
        # Titles with a member directory report the players in the lobby.
        if not game_profile.lobby_members:
            return game_profile.parent_lobby()
        players = len(registry.lobby_members(game_profile.product))
        if joining and registry.lobby_member(host_connection) is None:
            players += 1
        return game_profile.parent_lobby(players)

    def lobby_send(event: list) -> None:
        payload = gsxor.encrypt(data.encode(event))
        queue_notification(writer, GSHeader(HEADER_SIZE + len(payload), 0, 1, 0xd1, 2, 4).encode() + payload,
                           peer, "lobby")

    def lobby_publish() -> None:
        # Announce this member's current record to the other parent members.
        member = registry.lobby_member(host_connection)
        if member is None or not member.announced:
            return
        event = game_profile.lobby_member_event(member)
        others = registry.lobby_members(game_profile.product, exclude=host_connection)
        for other in others:
            other.send(event)
        LOG.info("lobby member announced game=%s peer=%s group=%s info_bytes=%d recipients=%d",
                 game_profile.slug, peer, member.group, len(member.info), len(others))
        notify_presence(member.name)

    def lobby_set_group(group: str) -> None:
        member = registry.lobby_member(host_connection)
        if member is None or member.group == group:
            return
        if member.announced and member.group != "1":
            # Leaving a room: 0x00403220 decrements the room's player count
            # and drops the member from the others' room member lists.
            event = game_profile.lobby_member_left(member)
            for other in registry.lobby_members(game_profile.product, exclude=host_connection):
                other.send(event)
        member.group = group
        lobby_publish()

    def lobby_depart() -> None:
        member = registry.lobby_leave(host_connection)
        if member is None or not member.announced:
            return
        event = game_profile.lobby_member_left(member)
        others = registry.lobby_members(game_profile.product)
        for other in others:
            other.send(event)
        LOG.info("lobby member departure sent game=%s peer=%s group=%s recipients=%d",
                 game_profile.slug, peer, member.group, len(others))
        notify_presence(member.name)

    def room_removed(room: rooms.Room) -> None:
        nonlocal joined_room, join_endpoint_sent, host_member_notified, closed_room_id
        if joined_room is not room:
            member = registry.lobby_member(host_connection) if game_profile.lobby_members else None
            if (member is not None and room.product == game_profile.product
                    and (room.owner, room.peer_ip) != (member.name.casefold(), peer[0])):
                # Parent members drop the game from their lists (reader
                # 0x0041b820 -> 0x00403b40).
                lobby_send(["55", [str(room.room_id)]])
            return
        # 0x01d73780 reads the nested room ID and removes the cached group.
        # Ending a hosted game does not invalidate the guest's GS sign-in.
        joined_room.members.discard((peer[0], wait_user.casefold()))
        joined_room.member_info.pop((peer[0], wait_user.casefold()), None)
        joined_room = None
        closed_room_id = room.room_id
        join_endpoint_sent = False
        host_member_notified = False
        payload = gsxor.encrypt(data.encode(["55", [str(room.room_id)]]))
        if queue_notification(writer, GSHeader(HEADER_SIZE + len(payload), 0, 1, 0xd1, 2, 4).encode() + payload,
                              peer, "room-closed"):
            LOG.info("room-closed notification sent peer=%s room=%d; sign-in session retained", peer, room.room_id)
        if game_profile.lobby_members:
            lobby_set_group("1")

    async def record_match(ladder, room: rooms.Room) -> None:
        # The owner's end-of-round report finishes the room's current match:
        # its latest results update the ladder, and the room's members get
        # the final statistics with the rating and level changes.
        match_id = registry.room_matches.get(room.room_id)
        pending = registry.match_results.get(match_id) if match_id else None
        mode = registry.match_modes.get(match_id) if match_id else None
        registry.forget_match(room.room_id)
        if pending is None or pending[0] != room.room_id:
            return
        changes = await asyncio.to_thread(ladder.record, pending[1], mode)
        event = game_profile.match_results_event(match_id, pending[1], mode, changes)
        in_room = [member for member in registry.lobby_members(game_profile.product)
                   if member.group == str(room.room_id)]
        for member in in_room:
            member.send(event)
        LOG.info("match recorded game=%s peer=%s room=%d match=%s mode=%s rated=%d recipients=%d",
                 game_profile.slug, peer, room.room_id, match_id, mode, len(changes), len(in_room))

    registry.add_remove_listener(room_removed)
    async def account_operation(operation, *arguments):
        if access_policy is None:
            return await asyncio.to_thread(operation, *arguments)
        async with access_policy.hash_slots:
            return await asyncio.to_thread(operation, *arguments)
    try:
        while True:
            header, payload = await asyncio.wait_for(read_frame(reader, maximum_size=65535), timeout)
            LOG.info("frame peer=%s type=0x%02x property=%d size=%d sender=%d receiver=%d",
                     peer, header.message_type, header.property, header.size,
                     header.sender, header.receiver)
            if header.message_type == KEY_EXCHANGE and header.property == 0:
                fields = data.decode(gsxor.decrypt(payload))
                if (len(fields) != 2 or fields[0] not in ("1", "2")
                        or not isinstance(fields[1], list) or len(fields[1]) != 3
                        or not isinstance(fields[1][2], bytes)):
                    raise ValueError("unrecognized key exchange structure")
                subtype = fields[0]
                key_blob = fields[1][2]
                if subtype == "1":
                    game_public = crypto.GSPublicKey.decode(key_blob)
                    public = private_key.public_key().public_numbers()
                    public_bytes = crypto.GSPublicKey(private_key.key_size, public.n, public.e).encode()
                    writer.write(make_frame(header, ["1", ["1", str(len(public_bytes)), public_bytes]]))
                    LOG.info("key exchange peer=%s received=public bits=%d sent=public", peer, game_public.bits)
                else:
                    if game_public is None:
                        raise ValueError("cipher key received before public key")
                    client_key = private_key.decrypt(key_blob, padding.PKCS1v15())
                    if len(client_key) != 16:
                        raise ValueError("unexpected client cipher key length")
                    server_key = secrets.token_bytes(16)
                    encrypted = game_public.encrypt(server_key)
                    writer.write(make_frame(header, ["2", ["1", str(len(encrypted)), encrypted]]))
                    LOG.info("key exchange peer=%s received=cipher-key sent=cipher-key", peer)
                await writer.drain()
                continue
            if header.property == 2 and client_key is not None and server_key is not None:
                # XIII sends the first encrypted frame immediately after its key,
                # so check both possible directions without logging its contents.
                for direction, key in (("client", client_key), ("server", server_key)):
                    try:
                        decoded = crypto.decrypt_payload(key, payload)
                        fields = data.decode(decoded)
                    except ValueError:
                        continue
                    LOG.info("encrypted message peer=%s type=0x%02x key=%s decoded_fields=%d field_types=%s",
                             peer, header.message_type, direction, len(fields),
                             [type(field).__name__ for field in fields])
                    break
                else:
                    LOG.info("encrypted payload rejected peer=%s with both key directions", peer)
                    break
                if header.message_type == 1 and len(fields) == 7 and all(isinstance(field, str) for field in fields):
                    if access_policy is not None and not access_policy.allow_authentication(peer[0]):
                        LOG.info("account request throttled peer=%s", peer)
                        break
                    # Executable call chain: product version, username, password,
                    # followed by four optional fields.
                    created = await account_operation(accounts.create_account, account_db, fields[1], fields[2])
                    if created:
                        payload = gsxor.encrypt(data.encode([bytes([header.message_type])]))
                        writer.write(response_header(header, payload, 0x26) + payload)
                        await writer.drain()
                        LOG.info("account stored peer=%s; experimental success response sent", peer)
                    else:
                        LOG.info("account already exists peer=%s; duplicate response not implemented", peer)
                elif (header.message_type == 0x66 and len(fields) == 4
                      and all(isinstance(field, str) for field in fields[:3])
                      and isinstance(fields[3], bytes)):
                    if access_policy is not None and not access_policy.allow_authentication(peer[0]):
                        LOG.info("login request throttled peer=%s", peer)
                        break
                    if await account_operation(accounts.verify_account, account_db, fields[0], fields[1]):
                        payload = gsxor.encrypt(data.encode([bytes([header.message_type])]))
                        writer.write(response_header(header, payload, 0x26) + payload)
                        await writer.drain()
                        LOG.info("local login accepted peer=%s", peer)
                        logged_in_user = fields[0].casefold()
                        authenticated[(peer[0], logged_in_user)] = time.monotonic() + 120
                        continue
                    LOG.info("local login rejected peer=%s; failure response not implemented", peer)
                elif (header.message_type == 0x66 and len(fields) == 3
                      and all(isinstance(field, str) for field in fields)
                      and local[1] != wait_port):
                    if access_policy is not None and not access_policy.allow_authentication(peer[0]):
                        LOG.info("launcher login throttled peer=%s", peer)
                        break
                    # 0x00380b34 -> 0x00383f20 -> 0x01d64990 sends the
                    # same username/password as sign-in, then the version.
                    # Verify this connection's identity rather than guessing
                    # from the other accounts sharing its public IP.
                    if await account_operation(accounts.verify_account, account_db, fields[0], fields[1]):
                        logged_in_user = fields[0].casefold()
                        # Launcher callback 0x01d65440 requires a nested list
                        # containing a string in addition to the message ID.
                        payload = gsxor.encrypt(data.encode([b"\x66", [""]]))
                        writer.write(response_header(header, payload, 0x26) + payload)
                        await writer.drain()
                        LOG.info("launcher credentials accepted peer=%s", peer)
                        continue
                    LOG.info("launcher credentials rejected peer=%s", peer)
                elif (header.message_type == 0x62 and wait_user is not None
                      and game_profile.lobby_members and len(fields) == 6
                      and all(isinstance(field, str) for field in fields)):
                    # Personal-info update (sender 0x00410750, called from
                    # menu handler 0x00518af0 via 0x00406f40): an empty
                    # string and five profile strings. The fifth is the
                    # country code that 0x005140e0 looks up from the chosen
                    # "P_country" name; player info returns it as field 4. Reader 0x00411ea0 only needs the
                    # result. Only the country is stored (accounts
                    # `countries`); the other fields may be personal and are
                    # neither stored nor logged.
                    stored = await account_operation(accounts.set_country, account_db, wait_user, fields[5])
                    payload = gsxor.encrypt(data.encode([b"\x62"]))
                    writer.write(response_header(header, payload, 0x26) + payload)
                    await writer.drain()
                    LOG.info("profile update acknowledged game=%s peer=%s country_stored=%s field_bytes=%s",
                             game_profile.slug, peer, stored, [len(field.encode()) for field in fields])
                    continue
            else:
                proxy_route = (header.sender, header.receiver) == (8, 11) and bool(game_profile.proxy_services)
                if (proxy_route and header.property == 0 and header.message_type == 0x5d
                        and not payload and proxy_user is not None and not proxy_ready):
                    ticket = proxy_handoffs.issue(peer[0], proxy_user)
                    payload = gsxor.encrypt(data.encode([b"\x5d", [ticket, wait_host, str(wait_port)]]))
                    writer.write(response_header(header, payload, 0x26) + payload)
                    await writer.drain()
                    LOG.info("proxy redirect sent game=%s peer=%s", game_profile.slug, peer)
                    continue
                if (proxy_route and header.property == 0 and header.message_type == 0x3a
                        and not payload and proxy_ready):
                    writer.write(response_header(header, b""))
                    await writer.drain()
                    LOG.info("proxy heartbeat game=%s peer=%s", game_profile.slug, peer)
                    continue
                if (header.property == 0 and header.message_type == 0x5c
                        and not payload and wait_user is not None):
                    # Friends-disconnect is separate from router logout.
                    # 0x01d6cbd0 sends an empty message without awaiting a result.
                    friends_logged_in = False
                    release_friend_channel()
                    LOG.info("friends channel disconnected peer=%s; sign-in session retained", peer)
                    continue
                if (header.property == 0 and header.message_type == 0x54
                        and not payload and wait_user is not None and friends_logged_in):
                    # GR2 0x004cece0 requests the ignore list with an empty
                    # body. Its completion reader (0x004cf740/0x004cc1a0)
                    # accepts GSSUCCESS containing only the binary operation
                    # ID. With no stored ignore entries, no item events follow.
                    payload = gsxor.encrypt(data.encode([b"\x54"]))
                    writer.write(response_header(header, payload, 0x26) + payload)
                    if game_profile.lobby_members:
                        # R6 (reader 0x004157f0 -> callback 0x003fd680)
                        # completes the request, then each pushed 0x55
                        # [name] (reader 0x004156b0 -> 0x003fd4f0) adds one
                        # entry to the ignore list (friends object +0x3c).
                        listed = await account_operation(accounts.ignored, account_db, wait_user)
                        for name in listed:
                            payload = gsxor.encrypt(data.encode([name]))
                            writer.write(response_header(header, payload, 0x55) + payload)
                        await writer.drain()
                        LOG.info("ignore list sent game=%s peer=%s entries=%d", game_profile.slug, peer, len(listed))
                        continue
                    await writer.drain()
                    LOG.info("empty ignore list completed game=%s peer=%s", game_profile.slug, peer)
                    continue
                if (header.property == 0 and header.message_type == 0x3a
                        and not payload and wait_user is not None):
                    authenticated[(peer[0], wait_user.casefold())] = time.monotonic() + 120
                    # The retail TCP reader (0x01d4a7e0) times out after 180
                    # seconds without received bytes. Its outgoing STILLALIVE
                    # only updates the separate send clock. Return the empty
                    # frame on this connection, including the module route.
                    writer.write(response_header(header, b""))
                    await writer.drain()
                    LOG.info("wait module heartbeat peer=%s; empty STILLALIVE returned", peer)
                    continue
                if header.property == 0 and header.message_type == 0x5d and logged_in_user:
                    now = time.monotonic()
                    for key, expiry in list(pending_handoffs.items()):
                        if expiry <= now:
                            del pending_handoffs[key]
                    pending_handoffs[(peer[0], logged_in_user)] = now + 120
                    payload = gsxor.encrypt(data.encode([b"\x5d", [wait_host, wait_port.to_bytes(4, "little")]]))
                    writer.write(response_header(header, payload, 0x26) + payload)
                    await writer.drain()
                    LOG.info("wait module address sent peer=%s", peer)
                    continue
                if header.property == 0 and payload:
                    try:
                        fields = data.decode(gsxor.decrypt(payload))
                        LOG.info("plain message structure peer=%s count=%d field_types=%s", peer,
                                 len(fields), [type(field).__name__ for field in fields])
                        if (proxy_route and header.message_type == 0x66
                                and proxy_user is None and wait_user is None
                                and len(fields) == 2 and all(isinstance(field, str) for field in fields)):
                            if access_policy is not None and not access_policy.allow_authentication(peer[0]):
                                LOG.info("proxy login throttled game=%s peer=%s", game_profile.slug, peer)
                                break
                            # 0x004c8740 stores username at SDK+4, password at
                            # +0x14, and version at +0x25. Proxy setup passes
                            # +4/+0x25, so these are USERNAME/VERSION. Redeem
                            # the exact account's prior verified selection.
                            if not proxy_handoffs.consume_login(peer[0], fields[0], fields[1]):
                                LOG.info("proxy login grant rejected game=%s peer=%s", game_profile.slug, peer)
                                break
                            proxy_user = fields[0].casefold()
                            payload = gsxor.encrypt(data.encode([b"\x66", []]))
                            writer.write(response_header(header, payload, 0x26) + payload)
                            await writer.drain()
                            LOG.info("proxy login grant accepted game=%s peer=%s", game_profile.slug, peer)
                            continue
                        if (proxy_route and header.message_type == 0x4d
                                and proxy_user is None and wait_user is None
                                and len(fields) == 1 and isinstance(fields[0], str)):
                            proxy_user = proxy_handoffs.redeem(peer[0], fields[0])
                            if proxy_user is None:
                                LOG.info("proxy ticket rejected game=%s peer=%s", game_profile.slug, peer)
                                break
                            proxy_ready = True
                            payload = gsxor.encrypt(data.encode([b"\x4d", []]))
                            writer.write(response_header(header, payload, 0x26) + payload)
                            await writer.drain()
                            LOG.info("proxy ticket accepted game=%s peer=%s", game_profile.slug, peer)
                            continue
                        if header.message_type == 0xcc and wait_user is not None and not proxy_route:
                            reply = proxy_directory.reply(fields)
                            if fields[0] == "2":
                                proxy_handoffs.authorize_login(peer[0], wait_user, game_profile.sdk_version)
                            if reply is not None:
                                writer.write(make_frame(header, reply))
                                await writer.drain()
                            LOG.info("proxy directory handled game=%s peer=%s operation=%s",
                                     game_profile.slug, peer,
                                     fields[0][0] if isinstance(fields[0], list) else fields[0])
                            continue
                        if proxy_route and header.message_type == 0xcc and proxy_ready:
                            reply = proxy.query_reply(game_profile, fields, proxy_user, ranking_store=ranking_store,
                                                      account_db=account_db)
                            writer.write(make_frame(header, reply))
                            await writer.drain()
                            query = fields[2][1] if (len(fields[2]) == 2 and isinstance(fields[2][1], list)
                                                     and len(fields[2][1]) == 6) else None
                            if query is not None and isinstance(query[4], list) and isinstance(query[5], list):
                                # Ladder, selection (aliases only by length)
                                # and requested column names.
                                columns = [c for c in (query[5][0] if query[5] and isinstance(query[5][0], list) else [])
                                           if isinstance(c, str) and len(c) <= 32 and c.replace("_", "").isalnum()]
                                # Filters: field name, operator and a
                                # numeric value (a country ID), or the shape.
                                try:
                                    filters = [(name[:32], operator, value) for name, operator, value
                                               in ranking_filters(query[5][2] if len(query[5]) > 2 else [])]
                                except ValueError:
                                    filters = value_shape(query[5][2])
                                LOG.info("ranking result game=%s peer=%s operation=%s request=%s ladder=%s "
                                         "selection=%s columns=%s filters=%s rows=%d", game_profile.slug, peer,
                                         fields[0], fields[1], value_shape(query[3]), value_shape(query[4]), columns,
                                         filters,
                                         len(reply[2][1][1][3]) if len(reply[2][1]) == 2 and len(reply[2][1][1]) == 4 else -1)
                            else:
                                LOG.info("ranking result game=%s peer=%s operation=%s request=%s",
                                         game_profile.slug, peer, fields[0], fields[1])
                            continue
                        if (header.message_type == 0xd9 and local[1] != wait_port
                                and logged_in_user is not None and len(fields) == 2
                                and all(isinstance(field, str) for field in fields)):
                            # Launcher callback 0x01d65750 expects a list of
                            # parent group records followed by an implicit end.
                            groups = game_profile.launcher_groups()
                            payload = gsxor.encrypt(data.encode([b"\xd9", groups]))
                            writer.write(response_header(header, payload, 0x26) + payload)
                            await writer.drain()
                            LOG.info("experimental launcher parent list sent peer=%s", peer)
                            continue
                        if (header.message_type == 0xd5 and local[1] != wait_port
                                and logged_in_user is not None):
                            if len(fields) == 3 and isinstance(fields[2], list):
                                LOG.info("registration structure peer=%s field_types=%s binary_lengths=%s",
                                         peer, [type(field).__name__ for field in fields[2]],
                                         [len(field) for field in fields[2] if isinstance(field, bytes)])
                            try:
                                created_room = registry.register(logged_in_user, peer[0], fields, game_profile)
                            except ValueError as exc:
                                LOG.info("registration rejected peer=%s reason=%s", peer, exc)
                                break
                            pending_handoffs[(peer[0], logged_in_user)] = created_room.pending_until
                            payload = gsxor.encrypt(data.encode([b"\xd5", [wait_host, str(wait_port),
                                                                              str(created_room.room_id), ""]]))
                            writer.write(response_header(header, payload, 0x26) + payload)
                            await writer.drain()
                            LOG.info("local room registered peer=%s id=%d; awaiting host login", peer, created_room.room_id)
                            continue
                        if (header.message_type == 0x4d and len(fields) == 1
                                and isinstance(fields[0], str)
                                and client_key is not None and server_key is not None):
                            key = (peer[0], fields[0].casefold())
                            expiry = pending_handoffs.pop(key, 0)
                            if expiry > time.monotonic():
                                payload = gsxor.encrypt(data.encode([b"\x4d"]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                                await writer.drain()
                                wait_user = fields[0]
                                LOG.info("wait module login accepted peer=%s", peer)
                                continue
                            LOG.info("wait module login rejected peer=%s: no pending handoff", peer)
                        elif (header.message_type == 0xd2 and len(fields) >= 2
                              and isinstance(fields[0], str)
                              and isinstance(fields[1], (str, bytes))):
                            if len(fields) == 2:
                                game_profile.require_rooms()
                            else:
                                game_profile.require_discovery()
                            key = (peer[0], fields[0].casefold())
                            expiry = pending_handoffs.pop(key, 0)
                            if expiry > time.monotonic():
                                if len(fields) == 2:
                                    if not isinstance(fields[1], str) or not fields[1].isascii() or not fields[1].isdecimal():
                                        LOG.info("host lobby login rejected peer=%s: invalid room id", peer)
                                        break
                                    candidate_host_room = registry.activate(int(fields[1]), fields[0].casefold(), peer[0], game_profile.product)
                                    if candidate_host_room is None:
                                        LOG.info("host lobby login rejected peer=%s: room ownership mismatch", peer)
                                        break
                                    if host_room is not None and host_room is not candidate_host_room:
                                        registry.unbind_host(host_room, host_connection)
                                    host_room = candidate_host_room
                                    registry.bind_host(host_room, fields[0].casefold(), peer[0], host_connection)
                                payload = gsxor.encrypt(data.encode(["210", [fields[1]]]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                                await writer.drain()
                                wait_user = fields[0]
                                LOG.info("lobby server login accepted peer=%s", peer)
                                lobby_logged_in = True
                                joined_parent = False
                                parent_info = None
                                continue
                            LOG.info("lobby server login rejected peer=%s: no pending handoff", peer)
                        elif header.message_type == 0xde and wait_user is not None:
                            notices = game_profile.login_notices(fields)
                            payload = gsxor.encrypt(data.encode([b"\xde", notices]))
                            writer.write(response_header(header, payload, 0x26) + payload)
                            await writer.drain()
                            LOG.info("login notices sent game=%s peer=%s", game_profile.slug, peer)
                            continue
                        elif (header.message_type == 0x13 and wait_user is not None
                              and len(fields) == 1 and isinstance(fields[0], str)
                              and fields[0].casefold() == wait_user.casefold()):
                            profile = [wait_user, wait_user, "", "", "", "", ""]
                            if game_profile.lobby_members:
                                # Reader 0x004110a0 -> callback 0x004068e0 ->
                                # 0x00407350 files the seven fields at player
                                # object +0x228: field 0 -> +0x114 (10 bytes),
                                # 1 -> +0x0 (alias, 16), 2 -> +0x10 (33),
                                # 3 -> +0x31 (33), 4 -> +0x52 (65), 5 -> +0x93
                                # (129), 6 -> +0x11e (129); confirmed from a
                                # retail save state. Personal Settings
                                # (0x00542e40) shows +0x52, field 4, as the
                                # country (0x00514250 maps the code back to
                                # the "P_country" name).
                                profile[4] = await account_operation(accounts.country, account_db, wait_user)
                            payload = gsxor.encrypt(data.encode([b"\x13", profile]))
                            writer.write(response_header(header, payload, 0x26) + payload)
                            await writer.drain()
                            LOG.info("player info sent peer=%s field_bytes=%s", peer,
                                     [len(field.encode()) for field in profile])
                            continue
                        elif (header.message_type == 0x08 and wait_user is not None
                              and game_profile.lobby_members and len(fields) == 7
                              and all(isinstance(value, str) for value in fields)):
                            # Player search (sender 0x00412c50: five strings,
                            # a "0"/"1"/"2" character and a string). Reader
                            # 0x00413d80 takes one result per 0x26 reply,
                            # [name, u32 status, product]; callback 0x003fd3d0
                            # keeps up to 20 and treats 0x27 with code 0x30 as
                            # the end of the results. Status (UI 0x004e0700):
                            # 0 online, 1 offline, 9 in game.
                            names = await account_operation(accounts.search_accounts, account_db, fields[0])
                            sent = 0
                            for name in names:
                                if name.casefold() == wait_user.casefold():
                                    continue
                                status, product = presence(name)
                                result = [name, status.to_bytes(4, "little"), product]
                                payload = gsxor.encrypt(data.encode([b"\x08", result]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                                sent += 1
                            payload = gsxor.encrypt(data.encode([b"\x08", [(0x30).to_bytes(4, "little")]]))
                            writer.write(response_header(header, payload, 0x27) + payload)
                            await writer.drain()
                            LOG.info("player search answered game=%s peer=%s results=%d",
                                     game_profile.slug, peer, sent)
                            continue
                        elif (header.message_type == 0x13 and wait_user is not None
                              and game_profile.lobby_members and len(fields) == 1
                              and isinstance(fields[0], str)):
                            # Lobby player list (native 0x00406ec0 -> sender
                            # 0x00410610). The reply reader (0x0040fed0, via
                            # 0x004110a0) accepts 0x26 with the profile strings
                            # or 0x27 with a four-byte error code.
                            known = [member.name for member in registry.lobby_members(game_profile.product)
                                     if fields[0] and member.name.casefold() == fields[0].casefold()]
                            if not known and fields[0]:
                                # Search results and friends need not be online.
                                found = await account_operation(accounts.find_account, account_db, fields[0])
                                known = [found] if found else []
                            if known:
                                profile = [known[0], known[0], "", "",
                                           await account_operation(accounts.country, account_db, known[0]), "", ""]
                                payload = gsxor.encrypt(data.encode([b"\x13", profile]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                                LOG.info("member player info sent game=%s peer=%s field_bytes=%s", game_profile.slug,
                                         peer, [len(field.encode()) for field in profile])
                            else:
                                payload = gsxor.encrypt(data.encode([b"\x13", [bytes(4)]]))
                                writer.write(response_header(header, payload, 0x27) + payload)
                                LOG.info("player info unavailable game=%s peer=%s name_bytes=%d",
                                         game_profile.slug, peer, len(fields[0].encode()))
                            await writer.drain()
                            continue
                        elif (header.message_type == 0x4e and wait_user is not None
                              and len(fields) == 2
                              and all(isinstance(field, bytes) and len(field) == 4 for field in fields)):
                            # The shared presence channel supports login and an
                            # empty ignore list; other operations remain explicit.
                            if game_profile.lobby_members:
                                # Registered before the reply so a message sent
                                # right after this login is deliverable.
                                registry.friend_channels[wait_user.casefold()] = (host_connection, wait_push)
                            payload = gsxor.encrypt(data.encode([b"\x4e"]))
                            writer.write(response_header(header, payload, 0x26) + payload)
                            if game_profile.lobby_members:
                                listed = await account_operation(accounts.friends, account_db, wait_user)
                                for name, group in listed:
                                    payload = gsxor.encrypt(data.encode(friend_record(name, group)))
                                    writer.write(response_header(header, payload, 0x4a) + payload)
                                registry.friend_lists[wait_user.casefold()] = {
                                    name.casefold(): (name, group) for name, group in listed}
                                LOG.info("friends list sent game=%s peer=%s friends=%d",
                                         game_profile.slug, peer, len(listed))
                                notify_presence(wait_user)
                            await writer.drain()
                            friends_logged_in = True
                            LOG.info("experimental friends login accepted peer=%s", peer)
                            continue
                        elif (header.message_type == 0x4b and wait_user is not None and friends_logged_in
                              and game_profile.lobby_members and len(fields) == 3
                              and isinstance(fields[0], str) and isinstance(fields[1], str)
                              and isinstance(fields[2], bytes) and len(fields[2]) == 4):
                            # Add friend (sender 0x004127a0: name, group, u32).
                            # Reader 0x00412ea0 reads [0x4b, name] from 0x26;
                            # callback 0x003fdc70 only records the result. The
                            # list entry comes from a pushed 0x4a record.
                            added = await account_operation(accounts.add_friend, account_db,
                                                            wait_user, fields[0], fields[1])
                            if added is None:
                                payload = gsxor.encrypt(data.encode([b"\x4b", [bytes(4)]]))
                                writer.write(response_header(header, payload, 0x27) + payload)
                            else:
                                payload = gsxor.encrypt(data.encode([b"\x4b", added]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                                registry.friend_lists.setdefault(wait_user.casefold(), {})[
                                    added.casefold()] = (added, fields[1])
                                payload = gsxor.encrypt(data.encode(friend_record(added, fields[1])))
                                writer.write(response_header(header, payload, 0x4a) + payload)
                            await writer.drain()
                            LOG.info("friend add game=%s peer=%s added=%s", game_profile.slug, peer, added is not None)
                            continue
                        elif (header.message_type == 0x58 and wait_user is not None and friends_logged_in
                              and game_profile.lobby_members and len(fields) == 2
                              and isinstance(fields[0], list) and 0 < len(fields[0]) <= 20
                              and all(isinstance(name, str) for name in fields[0])
                              and isinstance(fields[1], str)):
                            # Message (sender 0x00412a40: [recipients], text).
                            # Reader 0x00413170: 0x26 [0x58, name] or 0x27
                            # [0x58, [code, name]]. 0x00466e10 requires the
                            # success value to be a string (node type 1), not
                            # a list; otherwise the callback never runs. Recipients get a pushed
                            # 0x65 [from, text, time] (reader 0x00413040 ->
                            # 0x003fca90; inbox of 10, text buffer 0x100).
                            text = fields[1].encode()[:255].decode(errors="ignore")
                            undelivered = None
                            withheld = 0
                            for name in fields[0]:
                                channel = registry.friend_channels.get(name.casefold())
                                if channel is None:
                                    undelivered = undelivered or name
                                    continue
                                if wait_user.casefold() in await account_operation(
                                        accounts.ignoring, account_db, name, [wait_user]):
                                    # The recipient ignores the sender: the
                                    # message is dropped without telling them.
                                    withheld += 1
                                    continue
                                channel[1](0x65, [wait_user, text, message_time()])
                            if undelivered is None:
                                payload = gsxor.encrypt(data.encode([b"\x58", fields[0][0]]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                            else:
                                payload = gsxor.encrypt(data.encode([b"\x58", [bytes(4), undelivered]]))
                                writer.write(response_header(header, payload, 0x27) + payload)
                            await writer.drain()
                            LOG.info("friend message game=%s peer=%s recipients=%d delivered=%s withheld=%d",
                                     game_profile.slug, peer, len(fields[0]), undelivered is None, withheld)
                            continue
                        elif (header.message_type in (0x4f, 0x50) and wait_user is not None and friends_logged_in
                              and game_profile.lobby_members and len(fields) == 1
                              and isinstance(fields[0], str)):
                            # Ignore (0x4f, sender 0x00414880) and unignore
                            # (0x50, sender 0x004149e0) a player by name.
                            # Readers 0x00414c60 / 0x00415140 take element 1
                            # as a list: success 0x26 [op, [name]], failure
                            # 0x27 [op, [code, [name]]]. Callbacks 0x003fd8b0
                            # / 0x003fd700 add or remove the entry themselves
                            # and complete the request state (+0x80 / +0x88).
                            if header.message_type == 0x4f:
                                name = await account_operation(accounts.add_ignored, account_db,
                                                               wait_user, fields[0])
                                done = name is not None
                            else:
                                done = await account_operation(accounts.remove_ignored, account_db,
                                                               wait_user, fields[0])
                                name = fields[0]
                            op = bytes([header.message_type])
                            if done:
                                payload = gsxor.encrypt(data.encode([op, [name]]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                            else:
                                payload = gsxor.encrypt(data.encode([op, [bytes(4), [fields[0]]]]))
                                writer.write(response_header(header, payload, 0x27) + payload)
                            await writer.drain()
                            LOG.info("ignore %s game=%s peer=%s done=%s",
                                     "add" if header.message_type == 0x4f else "remove",
                                     game_profile.slug, peer, done)
                            continue
                        elif (header.message_type == 0x4c and wait_user is not None and friends_logged_in
                              and game_profile.lobby_members and len(fields) == 1
                              and isinstance(fields[0], str)):
                            # Remove friend (sender 0x00412900: name). Reader
                            # 0x00412f70 reads [0x4c, name]; callback
                            # 0x003fdbe0 removes that entry (0x003fcf50).
                            removed = await account_operation(accounts.remove_friend, account_db,
                                                              wait_user, fields[0])
                            if removed:
                                registry.friend_lists.get(wait_user.casefold(), {}).pop(
                                    fields[0].casefold(), None)
                                payload = gsxor.encrypt(data.encode([b"\x4c", fields[0]]))
                                writer.write(response_header(header, payload, 0x26) + payload)
                            else:
                                payload = gsxor.encrypt(data.encode([b"\x4c", [bytes(4)]]))
                                writer.write(response_header(header, payload, 0x27) + payload)
                            await writer.drain()
                            LOG.info("friend remove game=%s peer=%s removed=%s", game_profile.slug, peer, removed)
                            continue
                        elif (header.message_type == 0xd1 and wait_user is not None
                              and len(fields) == 2 and isinstance(fields[0], str)
                              and isinstance(fields[1], list)
                              and fields[0].isascii() and fields[0].isdigit()
                              and len(fields[0]) <= 3):
                            subtype = int(fields[0])
                            LOG.info("lobby request game=%s peer=%s subtype=%d nested_types=%s",
                                     game_profile.slug, peer, subtype,
                                     [type(item).__name__ for item in fields[1]])
                            if subtype in (21, 3, 9, 8, 23) or (
                                    subtype == 42 and joined_parent and joined_room is None):
                                game_profile.require_discovery()
                            else:
                                game_profile.require_rooms()
                            if subtype == 21:
                                game_profile.discovery_login(fields[1])
                                payload = gsxor.encrypt(data.encode(["38", ["21"]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                # The title adapter supplies its tested parent record.
                                payload = gsxor.encrypt(data.encode(["53", ["1", "256", ["0"], [parent_record()]]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                await writer.drain()
                                LOG.info("discovery login response and experimental parent lobby sent peer=%s", peer)
                                continue
                            if (subtype == 3 and len(fields[1]) >= 1
                                    and isinstance(fields[1][0], str) and fields[1][0] == "1"):
                                pending_handoffs[(peer[0], wait_user.casefold())] = time.monotonic() + 120
                                payload = gsxor.encrypt(data.encode(["38", ["3", ["1", wait_host, str(wait_port)]]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                await writer.drain()
                                LOG.info("lobby server address sent peer=%s", peer)
                                continue
                            if (subtype == 9 and len(fields[1]) >= 1
                                    and isinstance(fields[1][0], str) and fields[1][0] == "1"):
                                # Group-info notification with the actual local parent
                                # and no child game rooms; the prior subtype-9 reply
                                # caused XIII to report that the group did not exist.
                                payload = gsxor.encrypt(data.encode(["53", ["1", "256", parent_record(), registry.children(game_profile.product)]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                if game_profile.lobby_members:
                                    # R6 sends this after leaving a room. The
                                    # leave callback (0x00403880 -> 0x004051c0)
                                    # first clears the client's game and member
                                    # lists, so a hosted game stays hidden until
                                    # its owner is filed again. 0x00401880 skips
                                    # members that are already known.
                                    known = [member for member in registry.lobby_members(
                                        game_profile.product, exclude=host_connection) if member.announced]
                                    for member in known:
                                        lobby_send(game_profile.lobby_member_event(member))
                                    LOG.info("lobby members resent game=%s peer=%s members=%d",
                                             game_profile.slug, peer, len(known))
                                await writer.drain()
                                LOG.info("lobby group sent peer=%s active_rooms=%d", peer, len(registry.children(game_profile.product)))
                                continue
                            if subtype == 23:
                                if not lobby_logged_in:
                                    raise ValueError("parent join requires lobby server login")
                                result = game_profile.join_parent_lobby(fields[1])
                                joined_parent = True
                                parent_info = None
                                payload = gsxor.encrypt(data.encode(["38", ["23", result]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                # The parent exists even when there are no
                                # hosted games. This is directory membership,
                                # not registration of a synthetic game room.
                                children = registry.children(game_profile.product)
                                payload = gsxor.encrypt(data.encode(["53", [
                                    "1", "256", parent_record(joining=True), children]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                if game_profile.lobby_members:
                                    # Hosted games are listed only when their
                                    # owner is a known member of the room.
                                    registry.lobby_join(host_connection, rooms.LobbyMember(
                                        wait_user, game_profile.product, peer[0], "", lobby_send))
                                    known = [member for member in registry.lobby_members(
                                        game_profile.product, exclude=host_connection) if member.announced]
                                    for member in known:
                                        lobby_send(game_profile.lobby_member_event(member))
                                    LOG.info("lobby members sent game=%s peer=%s members=%d",
                                             game_profile.slug, peer, len(known))
                                await writer.drain()
                                LOG.info("parent lobby join accepted game=%s peer=%s active_rooms=%d",
                                         game_profile.slug, peer, len(children))
                                continue
                            if subtype == 42 and joined_parent and joined_room is None:
                                parent_info = game_profile.parent_player_info(fields[1])
                                # 0x004db070 delegates to the common lobby
                                # success parser; no game endpoint belongs
                                # in this directory-level completion.
                                payload = gsxor.encrypt(data.encode(["38", ["42"]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                member = registry.lobby_member(host_connection) if game_profile.lobby_members else None
                                if member is not None:
                                    member.info = parent_info
                                    member.announced = True
                                    lobby_publish()
                                await writer.drain()
                                LOG.info("parent player data updated game=%s peer=%s bytes=%d",
                                         game_profile.slug, peer, len(parent_info))
                                continue
                            if subtype == 12 and game_profile.lobby_create:
                                # Lobby-server room creation. The creating
                                # connection becomes the room's owner binding;
                                # closing it or leaving removes the room.
                                if not lobby_logged_in or not joined_parent or host_room is not None:
                                    raise ValueError("room creation requires parent lobby membership")
                                owner = wait_user.casefold()
                                created = registry.register(owner, peer[0], fields[1], game_profile)
                                created.owner_name = wait_user
                                registry.activate(created.room_id, owner, peer[0], game_profile.product)
                                registry.bind_host(created, owner, peer[0], host_connection)
                                host_room = created
                                payload = gsxor.encrypt(data.encode(["38", ["12", game_profile.lobby_create_result(created)]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                if game_profile.lobby_members:
                                    # List the new game for the other parent
                                    # members before announcing its owner, so
                                    # 0x004034a0 can count the owner in it.
                                    listing = ["53", ["1", "256", parent_record(), [created.group_fields()]]]
                                    for other in registry.lobby_members(game_profile.product, exclude=host_connection):
                                        other.send(listing)
                                    lobby_set_group(str(created.room_id))
                                await writer.drain()
                                LOG.info("lobby room created game=%s peer=%s room=%d max_players=%s spectators=%s flags=%s "
                                         "info_bytes=%d extra_bytes=%d versions=%s/%s",
                                         game_profile.slug, peer, created.room_id, created.max_players,
                                         created.max_spectators, created.flags, len(created.info),
                                         len(created.additional_info), created.game_version[:16], created.sdk_version[:16])
                                continue
                            if (game_profile.lobby_members and subtype == 30 and fields[1]
                                    and isinstance(fields[1][0], str) and room_number(fields[1][0]) is not None):
                                # Match results (0x00401070 -> sender 0x00418ec0):
                                # [match ID (+0x20, from operation 62), 0, one
                                # entry per player]; the owner sends it about
                                # every 15 seconds during a round. Entry
                                # (0x00457370): name, highest field ID, field
                                # count, field-ID bit mask, values by field ID
                                # (set by 0x00457280). Reader 0x0041e8c0 only
                                # reads the first value back. The room's
                                # members get the values as operation 71.
                                # The log records the values (names only by
                                # length).
                                active = registry.room_matches.get(host_room.room_id) if host_room is not None else None
                                if host_room is None:
                                    # Only the room's owner reports results (every
                                    # retail run); anyone else is not answered.
                                    LOG.info("match results ignored game=%s peer=%s reason=not-room-owner",
                                             game_profile.slug, peer)
                                    continue
                                if fields[1][0] != active:
                                    # A report for a match that this room's
                                    # operation 17 did not start, or one already
                                    # finished or replaced, is acknowledged to
                                    # the owner but neither kept nor broadcast.
                                    payload = gsxor.encrypt(data.encode(["38", ["30", [fields[1][0]]]]))
                                    writer.write(response_header(header, payload, 0xd1) + payload)
                                    await writer.drain()
                                    LOG.info("match results ignored game=%s peer=%s room=%d reason=not-active-match",
                                             game_profile.slug, peer, host_room.room_id)
                                    continue
                                current = host_room
                                mode = registry.match_modes.get(fields[1][0])
                                ladder = game_profile.match_ladder(account_db)
                                in_room = [member for member in registry.lobby_members(game_profile.product)
                                           if member.group == str(current.room_id)]
                                # Only players who have been in this room since
                                # the match started are kept: an entry naming
                                # anyone else is dropped, so an owner cannot
                                # rate accounts that did not play. A player who
                                # leaves mid-round stays eligible. Duplicate
                                # names are rejected by summarize().
                                players = registry.match_players.setdefault(fields[1][0], set())
                                players.update(member.name.casefold() for member in in_room)
                                entries = [entry for entry in fields[1][2:]
                                           if not (isinstance(entry, list) and entry and isinstance(entry[0], str))
                                           or entry[0].casefold() in players]
                                dropped = len(fields[1][2:]) - len(entries)
                                changes = {}
                                if ladder is not None:
                                    # Rating changes so far; the ladder is
                                    # updated when the round ends (operation 45).
                                    changes = await asyncio.to_thread(ladder.provisional, entries, mode)
                                    registry.match_results[fields[1][0]] = (current.room_id, entries)
                                    while len(registry.match_results) > 256:
                                        del registry.match_results[next(iter(registry.match_results))]
                                event = game_profile.match_results_event(fields[1][0], entries, mode, changes)
                                payload = gsxor.encrypt(data.encode(["38", ["30", [fields[1][0]]]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                for member in in_room:
                                    member.send(event)
                                await writer.drain()
                                LOG.info("match results received game=%s peer=%s match=%s room=%d recipients=%d "
                                         "dropped_non_members=%d shape=%s",
                                         game_profile.slug, peer, fields[1][0], current.room_id,
                                         len(in_room), dropped, value_shape(fields[1][1:], limit=64))
                                continue
                            if (game_profile.lobby_create and host_room is not None
                                    and subtype in (15, 17, 30, 31, 33)):
                                reply = game_profile.room_update(host_room, subtype, fields[1])
                                payload = gsxor.encrypt(data.encode(["38", [str(subtype), reply]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                LOG.info("room update accepted game=%s peer=%s room=%d subtype=%d game_port=%s",
                                         game_profile.slug, peer, host_room.room_id, subtype, host_room.game_port)
                                if game_profile.lobby_members and subtype == 17:
                                    # A new round drops the unfinished one's results.
                                    registry.forget_match(host_room.room_id)
                                    match_id = registry.next_match_id(fields[1][1])
                                    registry.room_matches[host_room.room_id] = str(match_id)
                                    registry.match_players[str(match_id)] = {
                                        member.name.casefold() for member in registry.lobby_members(game_profile.product)
                                        if member.group == str(host_room.room_id)}
                                    event = game_profile.match_started_event(host_room, match_id)
                                    in_room = [member for member in registry.lobby_members(game_profile.product)
                                               if member.group == str(host_room.room_id)]
                                    for member in in_room:
                                        member.send(event)
                                    LOG.info("match start sent game=%s peer=%s room=%d match=%d state=%s recipients=%d",
                                             game_profile.slug, peer, host_room.room_id, match_id,
                                             value_shape(fields[1][1]), len(in_room))
                                await writer.drain()
                                continue
                            if game_profile.lobby_create and subtype in game_profile.room_notice_subtypes:
                                if joined_room is not None and registry.active_room(joined_room.room_id, game_profile.product) is joined_room:
                                    game_profile.room_notice(joined_room, subtype, fields[1])
                                    notice_reply = game_profile.room_notice_reply(joined_room, subtype)
                                    if notice_reply is not None:
                                        payload = gsxor.encrypt(data.encode(notice_reply))
                                        writer.write(response_header(header, payload, 0xd1) + payload)
                                        await writer.drain()
                                        LOG.info("room report completed game=%s peer=%s room=%d subtype=%d",
                                                 game_profile.slug, peer, joined_room.room_id, subtype)
                                        ladder = game_profile.match_ladder(account_db)
                                        if subtype == 45 and ladder is not None and host_room is joined_room:
                                            await record_match(ladder, joined_room)
                                        continue
                                    LOG.info("room report accepted game=%s peer=%s room=%d subtype=%d; no completion",
                                             game_profile.slug, peer, joined_room.room_id, subtype)
                                    continue
                                if closed_room_id is not None and fields[1] == [str(closed_room_id)]:
                                    # A report queued before the room closed.
                                    LOG.info("late room report ignored game=%s peer=%s subtype=%d room=%d",
                                             game_profile.slug, peer, subtype, closed_room_id)
                                    continue
                                raise ValueError("room report without an active room")
                            if (subtype == 24 and len(fields[1]) == 5
                                    and all(isinstance(item, str) for item in fields[1])):
                                values = fields[1]
                                if (room_number(values[0]) is None or values[1]
                                        or not all(value.isascii() and value.isdecimal() for value in values[2:4])):
                                    LOG.info("unsupported room join request peer=%s", peer)
                                    break
                                room = registry.active_room(room_number(values[0]), game_profile.product)
                                if room is None:
                                    LOG.info("room join rejected peer=%s: room unavailable", peer)
                                    break
                                member = (peer[0], wait_user.casefold())
                                if host_room is not None and host_room is not room:
                                    registry.unbind_host(host_room, host_connection)
                                    host_room = None
                                if joined_room is not None:
                                    joined_room.members.discard(member)
                                    joined_room.member_info.pop(member, None)
                                room.members.add(member)
                                joined_room = room
                                if registry.bind_host(room, wait_user.casefold(), peer[0], host_connection):
                                    host_room = room
                                join_endpoint_sent = False
                                host_member_notified = False
                                # XIII parser 0x01d72af0 expects the room ID in
                                # the inner list of this lobby-success envelope.
                                payload = gsxor.encrypt(data.encode(["38", ["24", game_profile.room_join_result(room)]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                # Populate the room record; this is separate
                                # from the GAME_STARTED join-completion event.
                                payload = gsxor.encrypt(data.encode(["53", [str(room.room_id), "64", room.group_fields()]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                if game_profile.lobby_members:
                                    lobby_set_group(str(room.room_id))
                                    # The joiner files these in its room
                                    # member list (0x00403070 -> 0x00401810).
                                    present = [member for member in registry.lobby_members(
                                        game_profile.product, exclude=host_connection)
                                        if member.announced and member.group == str(room.room_id)]
                                    for member in present:
                                        lobby_send(game_profile.lobby_member_event(member))
                                    LOG.info("room members sent game=%s peer=%s room=%d members=%d",
                                             game_profile.slug, peer, room.room_id, len(present))
                                await writer.drain()
                                LOG.info("experimental room join accepted peer=%s room=%d", peer, room.room_id)
                                continue
                            if (subtype == 46 and len(fields[1]) == 1
                                    and isinstance(fields[1][0], str)
                                    and room_number(fields[1][0]) is not None):
                                room = registry.active_room(room_number(fields[1][0]), game_profile.product)
                                if room is None:
                                    LOG.info("additional room info unavailable peer=%s", peer)
                                    continue
                                payload = gsxor.encrypt(data.encode(["38", ["46", [str(room.room_id), room.additional_info]]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                await writer.drain()
                                LOG.info("additional room info sent peer=%s room=%d", peer, room.room_id)
                                continue
                            if (subtype in (34, 70) and game_profile.slug == 'gr2'
                                    and lobby_logged_in and closed_room_id is not None
                                    and (joined_room is None or joined_room.room_id != closed_room_id)
                                    and fields[1] == [str(closed_room_id)]):
                                # Worker removal may race a previously queued
                                # one-way game-connected/round report. It grants
                                # no membership or identity and has no ACK. Keep
                                # the signed-in browser/new room usable.
                                LOG.info('late game report ignored game=%s peer=%s subtype=%d room=%d; sign-in retained',
                                         game_profile.slug, peer, subtype, closed_room_id)
                                continue
                            if subtype == 34 and joined_room is not None:
                                connected_room = joined_room
                                if registry.active_room(connected_room.room_id, game_profile.product) is not connected_room:
                                    # active_room can discover expiry and invoke
                                    # room_removed synchronously in this call.
                                    if (game_profile.slug == 'gr2' and lobby_logged_in
                                            and fields[1] == [str(connected_room.room_id)]
                                            and closed_room_id == connected_room.room_id):
                                        LOG.info('game-connected report raced worker expiry peer=%s; sign-in retained', peer)
                                        continue
                                    raise ValueError('game-connected worker unavailable')
                                game_profile.game_connected(connected_room, fields[1])
                                LOG.info('game connection reported game=%s peer=%s room=%d; GS session retained',
                                         game_profile.slug, peer, joined_room.room_id)
                                continue
                            if subtype == 70 and joined_room is not None:
                                game_profile.game_transition(joined_room, fields[1])
                                # This report does not leave the directory room
                                # or sign out. No native completion is consumed.
                                LOG.info('game transition reported game=%s peer=%s room=%d; GS session retained',
                                         game_profile.slug, peer, joined_room.room_id)
                                continue
                            if (subtype == 42 and joined_room is not None and len(fields[1]) == 1
                                    and isinstance(fields[1][0], bytes) and len(fields[1][0]) <= 16384):
                                if registry.active_room(joined_room.room_id) is not joined_room:
                                    LOG.info("player info rejected peer=%s: host room unavailable", peer)
                                    break
                                joined_room.member_info[(peer[0], wait_user.casefold())] = fields[1][0]
                                is_owner = (peer[0], wait_user.casefold()) == (joined_room.peer_ip, joined_room.owner)
                                member = registry.lobby_member(host_connection) if game_profile.lobby_members else None
                                if member is not None:
                                    # Published again by the join callback
                                    # 0x00404300 (via 0x004013a0).
                                    if member.info != fields[1][0]:
                                        member.info = fields[1][0]
                                        lobby_publish()
                                elif host_probe is not None and not is_owner and not joined_room.dedicated:
                                    guest_port = joined_room.profile.player_port(fields[1][0])
                                    notified = host_probe.member_info(joined_room, wait_user, peer[0],
                                                                      fields[1][0], new=not host_member_notified)
                                    host_member_notified |= notified
                                    LOG.info("member endpoint peer=%s game_port=%d host_notified=%s",
                                             peer, guest_port, notified)
                                payload = gsxor.encrypt(data.encode(["38", ["42"]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                # The room-join success only starts player-info
                                # setup. GAME_STARTED (56) reaches 0x01d8cdf0
                                # and the game's 0x00382670 completion callback.
                                if (game_profile.game_started and not join_endpoint_sent
                                        and (not is_owner or game_profile.owner_game_started)):
                                    payload = gsxor.encrypt(data.encode(joined_room.game_started_fields()))
                                    writer.write(response_header(header, payload, 0xd1) + payload)
                                    join_endpoint_sent = True
                                    LOG.info("host endpoint sent peer=%s room=%d host=%s port=%s",
                                             peer, joined_room.room_id, joined_room.peer_ip, joined_room.game_port)
                                await writer.drain()
                                LOG.info("player info updated peer=%s room=%d", peer, joined_room.room_id)
                                continue
                            if (subtype == 8 and len(fields[1]) == 1
                                    and isinstance(fields[1][0], str)):
                                group_id = fields[1][0]
                                # Cleanup can name the parent or reset sentinel,
                                # or repeat a leave after GROUP_REMOVE arrived.
                                allowed = {"0", "1"}
                                if joined_room is not None:
                                    allowed.add(str(joined_room.room_id))
                                if closed_room_id is not None:
                                    allowed.add(str(closed_room_id))
                                if host_room is not None:
                                    # The owner may leave a room it created
                                    # (operation 12) before its self-join (24).
                                    allowed.add(str(host_room.room_id))
                                if group_id not in allowed:
                                    LOG.info("room leave ignored peer=%s: group not associated with this session", peer)
                                    continue
                                if group_id in ("0", "1"):
                                    joined_parent = False
                                    parent_info = None
                                if joined_room is not None:
                                    joined_room.members.discard((peer[0], wait_user.casefold()))
                                    joined_room.member_info.pop((peer[0], wait_user.casefold()), None)
                                    if host_probe is not None and host_member_notified:
                                        host_probe.notify_host(joined_room, [joined_room.profile.member_left(wait_user)])
                                host_member_notified = False
                                payload = gsxor.encrypt(data.encode(["38", ["8", [group_id]]]))
                                writer.write(response_header(header, payload, 0xd1) + payload)
                                if game_profile.lobby_members:
                                    if group_id in ("0", "1"):
                                        lobby_depart()
                                    else:
                                        lobby_set_group("1")
                                await writer.drain()
                                LOG.info("room leave accepted peer=%s group=%s; sign-in session retained", peer, group_id)
                                if joined_room is not None:
                                    closed_room_id = joined_room.room_id
                                joined_room = None
                                if host_room is not None:
                                    removed = registry.unbind_host(host_room, host_connection)
                                    LOG.info("owner left room peer=%s room=%d removed=%s", peer, host_room.room_id, removed)
                                    host_room = None
                                continue
                            LOG.info("unsupported lobby request peer=%s subtype=%d nested_types=%s", peer, subtype,
                                     [type(item).__name__ for item in fields[1]])
                    except ValueError as exc:
                        LOG.info("plain message rejected game=%s peer=%s reason=%s",
                                 game_profile.slug, peer, type(exc).__name__)
                LOG.info("unhandled message peer=%s type=0x%02x", peer, header.message_type)
            # Avoid fabricating account creation or login results.
            break
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError,
            ValueError, TypeError) as exc:
        LOG.info("closed peer=%s reason=%s", peer, type(exc).__name__)
    finally:
        registry.remove_remove_listener(room_removed)
        release_friend_channel()
        if game_profile.lobby_members:
            lobby_depart()
        if joined_room is not None and wait_user is not None:
            joined_room.members.discard((peer[0], wait_user.casefold()))
            joined_room.member_info.pop((peer[0], wait_user.casefold()), None)
            if host_probe is not None and host_member_notified:
                host_probe.notify_host(joined_room, [joined_room.profile.member_left(wait_user)])
        if host_room is not None:
            removed = registry.unbind_host(host_room, host_connection)
            LOG.info("owner room connection closed peer=%s room=%d removed=%s", peer, host_room.room_id, removed)
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def serve(args: argparse.Namespace) -> None:
    key = load_key(args.private_key)
    pending_handoffs: dict[tuple[str, str], float] = {}
    authenticated: dict[tuple[str, str], float] = {}
    registry = rooms.RoomRegistry()
    game_profile = getattr(args, "game_profile", XIII)
    worker_path = getattr(args, "worker_state", None)
    if worker_path is not None:
        from .gr2_worker import read_worker
        registry.worker_provider = lambda: read_worker(worker_path, game_profile)
    proxy_handoffs = proxy.Handoffs()
    host_probe = srp_probe.Probe(pending_handoffs, registry, game_profile,
                                 host_alias=getattr(args, "host_alias", None))
    advertise_ip = getattr(args, "advertise_ip", None) or args.bind
    access_policy = getattr(args, "access_policy", None)
    async def callback(reader, writer):
        peer_ip = writer.get_extra_info("peername")[0]
        if access_policy is not None and not access_policy.acquire(peer_ip):
            writer.close()
            await writer.wait_closed()
            return
        try:
            await handle(reader, writer, key, args.timeout, args.account_db, advertise_ip, args.port,
                         pending_handoffs, authenticated, registry, host_probe, access_policy, game_profile,
                         proxy_handoffs,ranking_store=getattr(args,'ladder_store',None)if getattr(args,'native_rankings',False)else None,
                         host_alias=getattr(args, "host_alias", None))
        finally:
            if access_policy is not None:
                access_policy.release(peer_ip)
    server = await asyncio.start_server(callback, args.bind, args.port)
    launcher = None
    udp_transport = None
    nat_transport = None
    worker_task = None
    try:
        if worker_path is not None:
            async def watch_worker():
                while True:
                    registry.sync_worker()
                    await asyncio.sleep(1)
            worker_task = asyncio.create_task(watch_worker())
        udp_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: host_probe, local_addr=(args.bind, args.port))
        if args.nat_port:
            nat_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: nat.Discovery(args.nat_allowed_cidr, host_alias=getattr(args, "host_alias", None)),
                local_addr=(args.bind, args.nat_port))
        if args.launcher_port:
            launcher = await asyncio.start_server(callback, args.bind, args.launcher_port)
            LOG.info("experimental launcher listening at %s", launcher.sockets[0].getsockname())
        LOG.info("router handshake listening at %s", server.sockets[0].getsockname())
        await server.serve_forever()
    finally:
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        server.close()
        await server.wait_closed()
        if udp_transport is not None:
            udp_transport.close()
        if nat_transport is not None:
            nat_transport.close()
        if launcher is not None:
            launcher.close()
            await launcher.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--advertise-ip", help="address clients can reach; defaults to the bind address")
    parser.add_argument("--port", type=port_number, default=30000)
    parser.add_argument("--launcher-port", type=port_number)
    parser.add_argument("--nat-port", type=port_number)
    parser.add_argument("--nat-allowed-cidr", default="127.0.0.0/8")
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--account-db", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not 0 < args.timeout <= 300:
        parser.error("timeout must be between 0 and 300 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
