"""Chat (IRC) service for Brothers in Arms: Earned in Blood.

The game's IRC client sits at 0x005dd000-0x005e1000 in MP.SOZ. Each packet is
one RFC 1459 line in a GS Blowfish payload (cipher selector 0, the fixed key
at 0x00670220, created by 0x005e01c0) after a 2-byte big-endian length
(0x0043aaf0). The sender (0x005e03b0) puts one line per packet; the reader
(0x005e08a0) splits the decrypted bytes on CRLF. It copies only the trailer's
length and adds no terminator, so every server packet ends with a NUL.

The client logs in with NICK (its IRCID, field 0 of the 0x13 player info)
and USER <SDK version> +i 0 :<username>. Numeric 001 (0x005de900) answers
with MODE <nick> +i and calls the game's welcome (0x00502750), which lets it
join #LobbyGrp<lobby>.<lobby server>. Lobby invitations are private messages
to the friend's IRCID, "%s%%%d%%%d%%%d%%%d%%%s%%%s" (0x005de2b0). The
PRIVMSG handler (0x005de460) only accepts those addressed to its own nick
and hands them to the game (0x00502540). The client answers PING itself.

Only players signed in to the friends service, from the same address, may
log in, and only with their own IRCID. Every later command is checked
again, and a chat session is closed when its friends session ends
(RoomRegistry.end_chat). Logs never carry names or text.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import re
import time

from . import accounts, crypto

LOG = logging.getLogger("ubisoft.irc")

KEY = bytes.fromhex("06e2c8460190557c3ca1cda3e3a1106c")
SERVER_NAME = "tubisoft"
MAX_LINE = 510
# A 510-character line with CRLF, padded to eight bytes, plus the trailer.
MAX_PACKET = 514
MAX_TEXT = 400
MAX_CHANNELS = 4
MAX_CLIENTS = 256
WRITE_BACKLOG_LIMIT = 64 * 1024
RATE_LINES, RATE_SECONDS = 30, 10
PING_AFTER = 120
NICK = re.compile(r"[A-Za-z][A-Za-z0-9]{0,8}")
CHANNEL = re.compile(r"#[A-Za-z][A-Za-z0-9.]{0,39}")


def encode_packet(line: str) -> bytes:
    """One server line: CRLF and a NUL, encrypted, after a big-endian length."""
    payload = crypto.encrypt_payload(KEY, line.encode("latin-1")[:MAX_LINE] + b"\r\n\x00")
    return len(payload).to_bytes(2, "big") + payload


def decode_payload(payload: bytes) -> list[str]:
    text = crypto.decrypt_payload(KEY, payload).rstrip(b"\x00")
    return [line.decode("latin-1") for line in text.split(b"\r\n") if line]


def parse(line: str) -> tuple[str, list[str]]:
    if line.startswith(":"):
        line = line.partition(" ")[2]
    head, colon, trailing = line.partition(" :")
    words = head.split()
    if not words:
        return "", []
    params = words[1:]
    if colon:
        params.append(trailing)
    return words[0].upper(), params


@dataclass(eq=False)
class Client:
    writer: asyncio.StreamWriter
    peer: object
    nick: str | None = None
    user: str | None = None
    registered: bool = False
    channels: set[str] = field(default_factory=set)

    @property
    def prefix(self) -> str:
        return f":{self.nick}!{self.nick}@{SERVER_NAME}"

    def send(self, line: str) -> bool:
        transport = self.writer.transport
        if transport is None or transport.is_closing():
            return False
        packet = encode_packet(line)
        if transport.get_write_buffer_size() + len(packet) > WRITE_BACKLOG_LIMIT:
            LOG.warning("chat backlog exceeded peer=%s; disconnecting", self.peer)
            transport.abort()
            return False
        self.writer.write(packet)
        return True


class ChatServer:
    def __init__(self, registry, game_profile, account_db, access_policy=None) -> None:
        self.registry = registry
        self.game_profile = game_profile
        self.account_db = account_db
        self.access_policy = access_policy
        self.clients: dict[str, Client] = {}
        self.channels: dict[str, set[Client]] = {}
        self.connections = 0
        registry.chat_revokers.append(self.revoke)

    async def account_operation(self, operation, *arguments):
        if self.access_policy is None:
            return await asyncio.to_thread(operation, *arguments)
        async with self.access_policy.hash_slots:
            return await asyncio.to_thread(operation, *arguments)

    def signed_in(self, user: str, address: str) -> bool:
        key = user.casefold()
        return key in self.registry.friend_channels and self.registry.chat_hosts.get(key) == address

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client = Client(writer, peer)
        LOG.info("chat connected game=%s peer=%s", self.game_profile.slug, peer)
        reason = "closed"
        recent: list[float] = []
        idle = 0
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readexactly(2), PING_AFTER)
                except asyncio.TimeoutError:
                    idle += 1
                    if idle > 1:
                        reason = "idle"
                        break
                    client.send(f"PING :{SERVER_NAME}")
                    continue
                idle = 0
                size = int.from_bytes(head, "big")
                if not 10 <= size <= MAX_PACKET:
                    reason = "packet size"
                    break
                payload = await asyncio.wait_for(reader.readexactly(size), 30)
                try:
                    lines = decode_payload(payload)
                except ValueError:
                    reason = "packet"
                    break
                now = time.monotonic()
                recent = [moment for moment in recent if moment > now - RATE_SECONDS] + [now] * len(lines)
                if len(recent) > RATE_LINES:
                    reason = "rate"
                    break
                for line in lines:
                    if len(line) > MAX_LINE:
                        reason = "line length"
                        break
                    command, params = parse(line)
                    if not await self.dispatch(client, command, params):
                        reason = command.lower() or "empty"
                        break
                else:
                    await writer.drain()
                    continue
                break
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            reason = "disconnected"
        finally:
            self.depart(client)
            writer.close()
            LOG.info("chat closed game=%s peer=%s reason=%s", self.game_profile.slug, peer, reason)

    async def dispatch(self, client: Client, command: str, params: list[str]) -> bool:
        """Handle one line; False closes the connection."""
        if client.registered and command != "QUIT" and not self.signed_in(
                client.user or "", client.peer[0] if client.peer else ""):
            # The friends session ended after this chat login.
            client.send(f"ERROR :Closing link ({SERVER_NAME}: signed out)")
            LOG.info("chat session revoked game=%s peer=%s reason=signed-out", self.game_profile.slug, client.peer)
            return False
        if command == "PING":
            client.send(f":{SERVER_NAME} PONG {SERVER_NAME} :{params[-1] if params else SERVER_NAME}")
            return True
        if command in ("PONG", "PASS", "MODE"):
            # The client sends MODE <nick> +i after 001; there is nothing to set.
            return True
        if command == "QUIT":
            return False
        if not client.registered:
            if command == "NICK" and params and client.nick is None:
                client.nick = params[0]
            elif command == "USER" and len(params) >= 4 and client.user is None:
                client.user = params[3]
            else:
                LOG.info("chat command before login game=%s peer=%s command=%s", self.game_profile.slug,
                         client.peer, command if command.isalpha() and len(command) <= 10 else "other")
                return True
            if client.nick is not None and client.user is not None:
                return self.register(client)
            return True
        if command == "JOIN" and params:
            return self.join(client, params[0])
        if command == "PART" and params:
            self.part(client, params[0], params[1] if len(params) > 1 else "")
            return True
        if command == "PRIVMSG" and len(params) >= 2:
            await self.private_message(client, params[0], params[1])
            return True
        LOG.info("chat command ignored game=%s peer=%s command=%s params=%d", self.game_profile.slug,
                 client.peer, command if command.isalpha() and len(command) <= 10 else "other", len(params))
        return True

    def register(self, client: Client) -> bool:
        address = client.peer[0] if client.peer else ""
        user = client.user or ""
        expected = self.game_profile.player_info_id(user) if user else ""
        if (not NICK.fullmatch(client.nick or "") or client.nick.casefold() != expected.casefold()
                or not self.signed_in(user, address)):
            client.send(f"ERROR :Closing link ({SERVER_NAME}: not signed in)")
            LOG.info("chat login refused game=%s peer=%s nick_bytes=%d user_bytes=%d", self.game_profile.slug,
                     client.peer, len((client.nick or "").encode()), len(user.encode()))
            return False
        key = client.nick.casefold()
        previous = self.clients.get(key)
        if previous is not None:
            # The same player again (a reconnect): the newer session wins.
            self.depart(previous)
            previous.writer.close()
        elif len(self.clients) >= MAX_CLIENTS:
            client.send(f"ERROR :Closing link ({SERVER_NAME}: full)")
            return False
        client.registered = True
        self.clients[key] = client
        client.send(f":{SERVER_NAME} 001 {client.nick} :Welcome to Tubisoft chat")
        LOG.info("chat login accepted game=%s peer=%s clients=%d", self.game_profile.slug, client.peer,
                 len(self.clients))
        return True

    def join(self, client: Client, names: str) -> bool:
        for name in names.split(",")[:MAX_CHANNELS]:
            if not CHANNEL.fullmatch(name):
                LOG.info("chat join refused game=%s peer=%s channel_bytes=%d", self.game_profile.slug,
                         client.peer, len(name.encode()))
                continue
            key = name.casefold()
            if key in client.channels:
                continue
            if len(client.channels) >= MAX_CHANNELS:
                client.send(f":{SERVER_NAME} 405 {client.nick} {name} :You have joined too many channels")
                continue
            members = self.channels.setdefault(key, set())
            members.add(client)
            client.channels.add(key)
            for member in list(members):
                member.send(f"{client.prefix} JOIN :{name}")
            LOG.info("chat channel joined game=%s peer=%s members=%d", self.game_profile.slug, client.peer,
                     len(members))
        return True

    def part(self, client: Client, names: str, message: str) -> None:
        for name in names.split(",")[:MAX_CHANNELS]:
            key = name.casefold()
            members = self.channels.get(key)
            if members is None or client not in members:
                continue
            for member in list(members):
                member.send(f"{client.prefix} PART {name} :{message[:MAX_TEXT]}")
            self.leave(client, key)
            LOG.info("chat channel left game=%s peer=%s", self.game_profile.slug, client.peer)

    def leave(self, client: Client, key: str) -> None:
        client.channels.discard(key)
        members = self.channels.get(key)
        if members is not None:
            members.discard(client)
            if not members:
                del self.channels[key]

    async def private_message(self, client: Client, target: str, text: str) -> None:
        text = text[:MAX_TEXT]
        if target.startswith("#"):
            members = self.channels.get(target.casefold())
            delivered = 0
            if members is not None and client in members:
                for member in list(members):
                    if member is not client and member.send(f"{client.prefix} PRIVMSG {target} :{text}"):
                        delivered += 1
            LOG.info("chat channel message game=%s peer=%s recipients=%d text_bytes=%d", self.game_profile.slug,
                     client.peer, delivered, len(text.encode("latin-1")))
            return
        recipient = self.clients.get(target.casefold())
        outcome = "offline"
        if recipient is not None and recipient.user is not None and self.signed_in(
                recipient.user, recipient.peer[0] if recipient.peer else ""):
            ignored = await self.account_operation(accounts.ignoring, self.account_db, recipient.user,
                                                   [client.user])
            if (client.user or "").casefold() in ignored:
                # Dropped without telling the sender, as friend messages are.
                outcome = "withheld"
            elif recipient.send(f"{client.prefix} PRIVMSG {recipient.nick} :{text}"):
                outcome = "delivered"
        if outcome == "offline":
            client.send(f":{SERVER_NAME} 401 {client.nick} {target} :No such nick")
        LOG.info("chat private message game=%s peer=%s outcome=%s text_bytes=%d fields=%d",
                 self.game_profile.slug, client.peer, outcome, len(text.encode("latin-1")), text.count("%") + 1)

    def revoke(self, key: str) -> None:
        """Close the chat sessions of a player whose friends session ended."""
        for client in [c for c in self.clients.values() if (c.user or "").casefold() == key]:
            client.send(f"ERROR :Closing link ({SERVER_NAME}: signed out)")
            self.depart(client)
            client.writer.close()
            LOG.info("chat session revoked game=%s peer=%s reason=friends-session-ended",
                     self.game_profile.slug, client.peer)

    def depart(self, client: Client) -> None:
        if client.registered and client.nick is not None and self.clients.get(client.nick.casefold()) is client:
            del self.clients[client.nick.casefold()]
        notified: set[Client] = set()
        for key in list(client.channels):
            for member in self.channels.get(key, ()):
                if member is not client and member not in notified:
                    member.send(f"{client.prefix} QUIT :Quit")
                    notified.add(member)
            self.leave(client, key)
        client.registered = False


async def serve(bind: str, port: int, server: ChatServer) -> None:
    async def callback(reader, writer):
        peer_ip = writer.get_extra_info("peername")[0]
        policy = server.access_policy
        if policy is not None and not policy.acquire(peer_ip):
            writer.close()
            return
        try:
            await server.handle(reader, writer)
        finally:
            if policy is not None:
                policy.release(peer_ip)
    listener = await asyncio.start_server(callback, bind, port)
    LOG.info("chat listening game=%s at %s", server.game_profile.slug, listener.sockets[0].getsockname())
    try:
        await listener.serve_forever()
    finally:
        listener.close()
        await listener.wait_closed()
