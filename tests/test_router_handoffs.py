import asyncio
import socket
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from ubisoft import data, gsxor, router, srp_probe
from ubisoft.frame import GSHeader, HEADER_SIZE
from ubisoft.games.registry import GR2, XIII
from ubisoft.rooms import RoomRegistry


def frame(message_type, fields, property=0):
    payload = gsxor.encrypt(data.encode(fields))
    return GSHeader(HEADER_SIZE + len(payload), property, 1, message_type, 1, 2).encode() + payload


class Writer:
    def __init__(self, peer, local_port):
        self.peer = peer
        self.local_port = local_port
        self.output = bytearray()
        self.transport = FakeTransport()

    def get_extra_info(self, name):
        return {"peername": (self.peer, 1234), "sockname": ("192.0.2.1", self.local_port)}.get(name)

    def write(self, value):
        self.output.extend(value)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def launcher_session(self, database, username, password, authenticated, registry, pending):
        peer = "192.0.2.2"
        reader = asyncio.StreamReader()
        for fields in (["1", ["1", "0", b"key"]], ["2", ["1", "0", b"key"]]):
            reader.feed_data(frame(0xdb, fields))
        reader.feed_data(frame(0x66, [], property=2))
        reader.feed_data(frame(0xd5, ["12", "1", [
            "launcher", "1", "Same room name", "XIIIPS2US", "6", "16", "0", b"info", b"\0", "",
            "1.0", "XIIIPS21.0", "8208", peer, "7777", b""]]))
        reader.feed_eof()
        writer = Writer(peer, 30001)
        public = SimpleNamespace(bits=512, encrypt=lambda key: b"encrypted", encode=lambda: b"public")
        key = SimpleNamespace(key_size=512, decrypt=lambda blob, padding: b"x" * 16,
                              public_key=lambda: SimpleNamespace(public_numbers=lambda: SimpleNamespace(n=1, e=3)))
        with patch.object(router.crypto, "GSPublicKey") as public_class, \
             patch.object(router.crypto, "decrypt_payload", return_value=data.encode([username, password, "1.0"])):
            public_class.decode.return_value = public
            public_class.return_value = public
            await router.handle(reader, writer, key, 1, database, "192.0.2.1", 30000,
                                pending, authenticated, registry)
        return writer

    async def test_launchers_sharing_ip_register_rooms_for_their_own_verified_accounts(self):
        peer = "192.0.2.2"
        authenticated = {(peer, name): time.monotonic() + 60 for name in ("alpha", "beta", "old-session")}
        pending = {}
        registry = RoomRegistry()
        with TemporaryDirectory() as directory:
            database = Path(directory) / "accounts.sqlite3"
            for username in ("alpha", "beta"):
                self.assertTrue(router.accounts.create_account(database, username, username + "-fixture"))
                await self.launcher_session(database, username.upper(), username + "-fixture",
                                            authenticated, registry, pending)
        for room_id, username in ((1000, "alpha"), (1001, "beta")):
            self.assertIn((peer, username), pending)
            room = registry.activate(room_id, username, peer)
            self.assertIsNotNone(room)
            self.assertEqual(room.owner, username)
            self.assertEqual(room.name, "Same room name")

    async def test_launcher_does_not_borrow_another_active_accounts_identity(self):
        peer = "192.0.2.2"
        authenticated = {(peer, "alpha"): time.monotonic() + 60}
        pending = {}
        registry = RoomRegistry()
        with TemporaryDirectory() as directory:
            database = Path(directory) / "accounts.sqlite3"
            self.assertTrue(router.accounts.create_account(database, "alpha", "alpha-fixture"))
            self.assertTrue(router.accounts.create_account(database, "beta", "beta-fixture"))
            writer = await self.launcher_session(database, "beta", "wrong-fixture",
                                                 authenticated, registry, pending)
        self.assertEqual(pending, {})
        self.assertIsNone(registry.activate(1000, "alpha", peer))
        self.assertIsNone(registry.activate(1000, "beta", peer))
        headers = []
        offset = 0
        while offset < len(writer.output):
            header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
            headers.append(header.message_type)
            offset += header.size
        self.assertEqual(headers, [0xdb, 0xdb])

    async def test_launcher_retry_returns_existing_pending_room_id(self):
        registry = RoomRegistry()
        authenticated, pending = {}, {}
        with TemporaryDirectory() as directory:
            database = Path(directory) / "accounts.sqlite3"
            self.assertTrue(router.accounts.create_account(database, "host", "fixture-password"))
            for _ in range(2):
                writer = await self.launcher_session(database, "host", "fixture-password",
                                                     authenticated, registry, pending)
                offset = 0
                while offset < len(writer.output):
                    header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
                    payload = writer.output[offset + HEADER_SIZE:offset + header.size]
                    offset += header.size
                self.assertEqual(header.message_type, 0x26)
                self.assertEqual(data.decode(gsxor.decrypt(payload)),
                                 [b"\xd5", ["192.0.2.1", "30000", "1000", ""]])
        room = registry.activate(1000, "host", "192.0.2.2")
        self.assertIsNotNone(room)
        self.assertEqual(pending[(room.peer_ip, room.owner)], room.pending_until)

    async def test_owner_room_disconnect_clears_udp_registration_and_notifies_guest_without_logout(self):
        registry = RoomRegistry()
        host_ip, guest_ip = "192.0.2.2", "192.0.2.3"
        room = registry.register("host", host_ip, ["12", "1", [
            "launcher", "1", "Example", "XIIIPS2US", "6", "16", "0", b"info", b"\0", "",
            "1.0", "XIIIPS21.0", "8208", host_ip, "7777", b""]])
        registry.activate(room.room_id, room.owner, room.peer_ip)
        probe = srp_probe.Probe({}, registry)
        probe.connection_made(SimpleNamespace(sendto=lambda *args: None,
                                             get_extra_info=lambda name: ("192.0.2.1", 30000)))
        self.addCleanup(probe.connection_lost, None)
        host_peer = (host_ip, 1024)
        probe.sessions[host_peer] = srp_probe.Session(0x1234, 0, room=room)
        guest_reader = asyncio.StreamReader()
        guest_reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
        guest_reader.feed_data(frame(0xd1, ["24", [str(room.room_id), "", "0", "0", "XIIIPS2US"]]))
        guest_writer = Writer(guest_ip, 30000)
        task = asyncio.create_task(router.handle(
            guest_reader, guest_writer, None, 2, Path("/unused"), "192.0.2.1", 30000,
            {(guest_ip, "guest"): time.monotonic() + 60}, {}, registry, probe))
        try:
            async def joined():
                while (guest_ip, "guest") not in room.members:
                    await asyncio.sleep(0)
            await asyncio.wait_for(joined(), 1)
            host_reader = asyncio.StreamReader()
            host_reader.feed_data(frame(0xd2, ["host", "1", "", "", ""]))
            host_reader.feed_data(frame(0xd1, ["24", [str(room.room_id), "", "0", "0", "XIIIPS2US"]]))
            host_reader.feed_data(frame(0xd1, ["42", [b""]]))
            host_reader.feed_eof()
            await router.handle(host_reader, Writer(host_ip, 30000), None, 2, Path("/unused"),
                                "192.0.2.1", 30000, {(host_ip, "host"): time.monotonic() + 60}, {}, registry, probe)
            self.assertIsNone(registry.active_room(room.room_id))
            self.assertNotIn(host_peer, probe.sessions)
            guest_reader.feed_data(frame(0xd1, ["9", ["1"]]))
            guest_reader.feed_eof()
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        replies = []
        offset = 0
        while offset < len(guest_writer.output):
            header = GSHeader.decode(guest_writer.output[offset:offset + HEADER_SIZE])
            replies.append(data.decode(gsxor.decrypt(bytes(guest_writer.output[offset + HEADER_SIZE:offset + header.size]))))
            offset += header.size
        self.assertEqual(replies[-2:], [["55", [str(room.room_id)]], ["53", ["1", "256", router.PARENT_LOBBY, []]]])

    async def test_owner_leaving_group_removes_room_but_keeps_browser_connection(self):
        registry = RoomRegistry()
        host_ip = "192.0.2.2"
        room = registry.register("host", host_ip, ["12", "1", [
            "launcher", "1", "Example", "XIIIPS2US", "6", "16", "0", b"info", b"\0", "",
            "1.0", "XIIIPS21.0", "8208", host_ip, "7777", b""]])
        registry.activate(room.room_id, room.owner, room.peer_ip)
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xd2, ["host", "1", "", "", ""]))
        reader.feed_data(frame(0xd1, ["24", [str(room.room_id), "", "0", "0", "XIIIPS2US"]]))
        reader.feed_data(frame(0xd1, ["8", ["0"]]))
        reader.feed_data(frame(0xd1, ["9", ["1"]]))
        reader.feed_eof()
        writer = Writer(host_ip, 30000)
        await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                            {(host_ip, "host"): time.monotonic() + 60}, {}, registry)
        replies = []
        offset = 0
        while offset < len(writer.output):
            header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
            replies.append(data.decode(gsxor.decrypt(bytes(writer.output[offset + HEADER_SIZE:offset + header.size]))))
            offset += header.size
        self.assertEqual(replies[-2:], [["38", ["8", ["0"]]], ["53", ["1", "256", router.PARENT_LOBBY, []]]])
        self.assertEqual(registry.children(), [])

    async def test_owner_discovery_connection_close_does_not_remove_hosted_room(self):
        registry = RoomRegistry()
        host_ip = "192.0.2.2"
        room = registry.register("host", host_ip, ["12", "1", [
            "launcher", "1", "Example", "XIIIPS2US", "6", "16", "0", b"info", b"\0", "",
            "1.0", "XIIIPS21.0", "8208", host_ip, "7777", b""]])
        registry.activate(room.room_id, room.owner, room.peer_ip)
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xd2, ["host", "1", "", "", ""]))
        reader.feed_data(frame(0xd1, ["9", ["1"]]))
        reader.feed_eof()
        await router.handle(reader, Writer(host_ip, 30000), None, 1, Path("/unused"), "192.0.2.1", 30000,
                            {(host_ip, "host"): time.monotonic() + 60}, {}, registry)
        self.assertIs(registry.active_room(room.room_id), room)

    async def test_host_room_removal_notifies_guest_and_preserves_authenticated_lobby(self):
        registry = RoomRegistry()
        host_ip, guest_ip = "192.0.2.2", "192.0.2.3"
        room = registry.register("host", host_ip, ["12", "1", [
            "launcher", "1", "Example", "XIIIPS2US", "6", "16", "0", b"info", b"\0", "",
            "1.0", "XIIIPS21.0", "8208", host_ip, "7777", b""]])
        registry.activate(room.room_id, room.owner, room.peer_ip)
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
        reader.feed_data(frame(0xd1, ["24", [str(room.room_id), "", "0", "0", "XIIIPS2US"]]))
        reader.feed_data(frame(0xd1, ["42", [(64508).to_bytes(4, "little")]]))
        writer = Writer(guest_ip, 30000)
        authenticated = {(guest_ip, "guest"): time.monotonic() + 60}
        task = asyncio.create_task(router.handle(
            reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
            {(guest_ip, "guest"): time.monotonic() + 60}, authenticated, registry))
        async def wait_for_membership():
            while (guest_ip, "guest") not in room.member_info:
                await asyncio.sleep(0)
        try:
            await asyncio.wait_for(wait_for_membership(), 1)
            registry.remove(room.room_id, room.owner, room.peer_ip)
            # The same connection can refresh after its room disappears.
            reader.feed_data(frame(0xd1, ["8", ["0"]]))
            reader.feed_data(frame(0xd1, ["8", [str(room.room_id)]]))
            reader.feed_data(frame(0xd1, ["9", ["1"]]))
            reader.feed_eof()
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        replies = []
        offset = 0
        while offset < len(writer.output):
            header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
            replies.append(data.decode(gsxor.decrypt(bytes(writer.output[offset + HEADER_SIZE:offset + header.size]))))
            offset += header.size
        self.assertEqual(replies[-4], ["55", [str(room.room_id)]])
        self.assertEqual(replies[-3:-1], [["38", ["8", ["0"]]], ["38", ["8", [str(room.room_id)]]]])
        self.assertEqual(replies[-1], ["53", ["1", "256", router.PARENT_LOBBY, []]])
        self.assertGreater(authenticated[(guest_ip, "guest")], time.monotonic())
        self.assertEqual(room.members, set())

    async def test_host_self_join_accepts_empty_player_info_until_owner_connection_closes(self):
        registry = RoomRegistry()
        host_ip = "192.0.2.2"
        room = registry.register("host", host_ip, ["12", "1", [
            "launcher", "1", "Example", "XIIIPS2US", "6", "16", "0", b"info", b"\0", "",
            "1.0", "XIIIPS21.0", "8208", host_ip, "7777", b""]])
        registry.activate(room.room_id, "host", host_ip)
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xd2, ["host", "1", "", "", ""]))
        reader.feed_data(frame(0xd1, ["24", [str(room.room_id), "", "0", "0", "XIIIPS2US"]]))
        reader.feed_data(frame(0xd1, ["42", [b""]]))
        reader.feed_data(frame(0xd1, ["46", [str(room.room_id)]]))
        reader.feed_eof()
        writer = Writer(host_ip, 30000)
        probe = SimpleNamespace(member_info=lambda *args, **kwargs: self.fail("host must not open a peer to itself"))
        await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                            {(host_ip, "host"): time.monotonic() + 60}, {}, registry, probe)
        replies = []
        offset = 0
        while offset < len(writer.output):
            header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
            replies.append(data.decode(gsxor.decrypt(bytes(writer.output[offset + HEADER_SIZE:offset + header.size]))))
            offset += header.size
        self.assertIn(["38", ["42"]], replies)
        self.assertIn(room.game_started_fields(), replies)
        self.assertEqual(replies[-1], ["38", ["46", [str(room.room_id), b""]]])
        self.assertIsNone(registry.active_room(room.room_id))

    async def test_launcher_disconnect_does_not_discard_pending_host_room(self):
        registry = RoomRegistry()
        peer = "192.0.2.2"
        pending = {}
        authenticated = {(peer, "host"): time.monotonic() + 60}
        registration = ["12", "1", ["launcher", "1", "Example", "XIIIPS2US", "6", "16", "0",
                                    b"info", b"\0", "", "1.0", "XIIIPS21.0", "8208",
                                    peer, "7777", b""]]
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xdb, ["1", ["1", "0", b"key"]]))
        reader.feed_data(frame(0xdb, ["2", ["1", "0", b"key"]]))
        reader.feed_data(frame(0x66, [], property=2))
        reader.feed_data(frame(0xd5, registration))
        reader.feed_eof()
        writer = Writer(peer, 30001)
        public = SimpleNamespace(bits=512, encrypt=lambda key: b"encrypted", encode=lambda: b"public")
        key = SimpleNamespace(key_size=512, decrypt=lambda blob, padding: b"x" * 16,
                              public_key=lambda: SimpleNamespace(public_numbers=lambda: SimpleNamespace(n=1, e=3)))
        with patch.object(router.crypto, "GSPublicKey") as public_class, \
             patch.object(router.crypto, "decrypt_payload", return_value=data.encode(["host", "fixture-password", "1.0"])), \
             patch.object(router.accounts, "verify_account", return_value=True) as verify:
            public_class.decode.return_value = public
            public_class.return_value = public
            await router.handle(reader, writer, key, 1, Path("/unused"), "192.0.2.1", 30000,
                                pending, authenticated, registry)
        verify.assert_called_once_with(Path("/unused"), "host", "fixture-password")
        self.assertIn((peer, "host"), pending)
        self.assertIsNotNone(registry.activate(1000, "host", peer))

    async def test_friend_login_keeps_wait_connection_usable(self):
        peer = "192.0.2.3"
        pending = {(peer, "guest"): time.monotonic() + 60}
        authenticated = {}
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
        reader.feed_data(frame(0x4e, [b"1234", b"5678"]))
        reader.feed_data(GSHeader(HEADER_SIZE, 0, 1, 0x5c, 1, 2).encode())
        reader.feed_data(GSHeader(HEADER_SIZE, 0, 1, 0x3a, 1, 2).encode())
        reader.feed_eof()
        writer = Writer(peer, 30000)
        await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                            pending, authenticated, RoomRegistry())
        first = GSHeader.decode(writer.output[:HEADER_SIZE])
        second = GSHeader.decode(writer.output[first.size:first.size + HEADER_SIZE])
        self.assertEqual(second.message_type, 0x26)
        self.assertEqual(data.decode(gsxor.decrypt(bytes(writer.output[first.size + HEADER_SIZE:first.size + second.size]))), [b"\x4e"])
        heartbeat = GSHeader.decode(writer.output[first.size + second.size:])
        self.assertEqual(heartbeat, GSHeader(HEADER_SIZE, 0, 1, 0x3a, 2, 1))
        self.assertIn((peer, "guest"), authenticated)

    async def test_ignore_list_requires_active_friends_login_and_an_empty_request(self):
        for scenario in ("anonymous", "wait-only", "disconnected", "nonempty"):
            with self.subTest(scenario=scenario):
                peer = "192.0.2.3"
                reader = asyncio.StreamReader()
                if scenario != "anonymous":
                    reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
                if scenario in ("disconnected", "nonempty"):
                    reader.feed_data(frame(0x4e, [bytes(4), bytes(4)]))
                if scenario == "disconnected":
                    reader.feed_data(GSHeader(HEADER_SIZE, 0, 1, 0x5c, 1, 2).encode())
                request = (frame(0x54, ["unexpected"]) if scenario == "nonempty"
                           else GSHeader(HEADER_SIZE, 0, 1, 0x54, 1, 2).encode())
                reader.feed_data(request)
                reader.feed_eof()
                writer = Writer(peer, 30000)
                await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                                    {(peer, "guest"): time.monotonic() + 60}, {}, RoomRegistry())
                replies = []
                offset = 0
                while offset < len(writer.output):
                    header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
                    replies.append(data.decode(gsxor.decrypt(bytes(
                        writer.output[offset + HEADER_SIZE:offset + header.size]))))
                    offset += header.size
                self.assertNotIn([b"\x54"], replies)
                self.assertEqual(len(replies), {"anonymous": 0, "wait-only": 1,
                                               "disconnected": 2, "nonempty": 2}[scenario])

    async def test_login_notices_require_authentication_and_a_supported_title_and_language(self):
        for profile, authenticated, fields in ((GR2, False, ["en"]),
                                                (XIII, True, ["en"]),
                                                (GR2, True, ["unexpected"])):
            with self.subTest(profile=profile.slug, authenticated=authenticated, fields=fields):
                peer = "192.0.2.3"
                reader = asyncio.StreamReader()
                if authenticated:
                    reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
                reader.feed_data(frame(0xde, fields))
                reader.feed_eof()
                writer = Writer(peer, 30000)
                await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                                    {(peer, "guest"): time.monotonic() + 60}, {}, RoomRegistry(),
                                    game_profile=profile)
                if authenticated:
                    login = GSHeader.decode(writer.output[:HEADER_SIZE])
                    self.assertEqual(len(writer.output), login.size)
                else:
                    self.assertEqual(writer.output, b"")

    async def test_parent_player_data_requires_current_parent_membership_and_valid_record(self):
        record = bytes(28)
        for scenario in ("anonymous", "lobby-only", "left-parent", "wrong-size"):
            with self.subTest(scenario=scenario):
                peer = "192.0.2.3"
                reader = asyncio.StreamReader()
                if scenario != "anonymous":
                    reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
                if scenario in ("left-parent", "wrong-size"):
                    reader.feed_data(frame(0xd1, ["23", ["1", "", "256"]]))
                if scenario == "left-parent":
                    reader.feed_data(frame(0xd1, ["42", [record]]))
                    reader.feed_data(frame(0xd1, ["8", ["1"]]))
                reader.feed_data(frame(0xd1, ["42", [bytes(29) if scenario == "wrong-size" else record]]))
                reader.feed_eof()
                writer = Writer(peer, 30010)
                await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30010,
                                    {(peer, "guest"): time.monotonic() + 60}, {}, RoomRegistry(),
                                    game_profile=GR2)
                replies = []
                offset = 0
                while offset < len(writer.output):
                    header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
                    replies.append(data.decode(gsxor.decrypt(bytes(
                        writer.output[offset + HEADER_SIZE:offset + header.size]))))
                    offset += header.size
                self.assertEqual(replies.count(["38", ["42"]]), int(scenario == "left-parent"))
                if scenario == "left-parent":
                    self.assertEqual(replies[-1], ["38", ["8", ["1"]]])

    async def test_heartbeats_return_on_each_authenticated_module_connection(self):
        # The wait and room connections have independent receive deadlines.
        # Answering only one must not stand in for answering the other.
        for module in (1, 2):
            with self.subTest(module=module):
                peer = "192.0.2.3"
                reader = asyncio.StreamReader()
                reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
                heartbeat = GSHeader(HEADER_SIZE, 0, 1, 0x3a, 4, module)
                reader.feed_data(heartbeat.encode() * 8)
                reader.feed_eof()
                writer = Writer(peer, 30000)
                await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                                    {(peer, "guest"): time.monotonic() + 60}, {}, RoomRegistry())
                login = GSHeader.decode(writer.output[:HEADER_SIZE])
                expected = GSHeader(HEADER_SIZE, 0, 1, 0x3a, module, 4).encode()
                self.assertEqual(bytes(writer.output[login.size:]), expected * 8)

    async def test_unauthenticated_heartbeat_cannot_create_a_session(self):
        peer = "192.0.2.3"
        reader = asyncio.StreamReader()
        reader.feed_data(GSHeader(HEADER_SIZE, 0, 1, 0x3a, 4, 1).encode())
        reader.feed_eof()
        writer = Writer(peer, 30000)
        authenticated = {}
        await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                            {}, authenticated, RoomRegistry())
        self.assertEqual(writer.output, b"")
        self.assertEqual(authenticated, {})

    async def test_guest_refresh_sees_active_room_from_another_ip(self):
        registry = RoomRegistry()
        host_ip, guest_ip = "192.0.2.2", "192.0.2.3"
        registration = ["12", "1", ["launcher", "1", "Example", "XIIIPS2US", "6", "16", "0",
                                    b"description:7777", b"\0", "", "1.0", "XIIIPS21.0",
                                    "8208", host_ip, "7777", b""]]
        room = registry.register("host", host_ip, registration)
        registry.activate(room.room_id, "host", host_ip)
        pending = {(guest_ip, "guest"): time.monotonic() + 60}
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
        reader.feed_data(frame(0xd1, ["9", ["1"]]))
        reader.feed_eof()
        writer = Writer(guest_ip, 30000)
        await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                            pending, {}, registry)
        first = GSHeader.decode(writer.output[:HEADER_SIZE])
        notification = GSHeader.decode(writer.output[first.size:first.size + HEADER_SIZE])
        self.assertEqual(notification.message_type, 0xd1)
        fields = data.decode(gsxor.decrypt(bytes(writer.output[first.size + HEADER_SIZE:])))
        self.assertEqual(fields[0], "53")
        self.assertEqual(fields[1][3][0][2], str(room.room_id))
        self.assertEqual(int(fields[1][3][0][5]), 0x2018)
        self.assertEqual(fields[1][3][0][18], host_ip)

    async def test_guest_join_and_leave_use_registered_room(self):
        registry = RoomRegistry()
        host_ip, guest_ip = "192.0.2.2", "192.0.2.3"
        registration = ["12", "1", ["launcher", "1", "Example", "XIIIPS2US", "6", "16", "0",
                                    b"description:7777", b"\0", "", "1.0", "XIIIPS21.0",
                                    "8208", host_ip, "7777", b""]]
        room = registry.register("host", host_ip, registration)
        registry.activate(room.room_id, "host", host_ip)
        pending = {(guest_ip, "guest"): time.monotonic() + 60}
        reader = asyncio.StreamReader()
        reader.feed_data(frame(0xd2, ["guest", "1", "", "", ""]))
        room.additional_info = b"host=0"
        reader.feed_data(frame(0xd1, ["46", [str(room.room_id)]]))
        reader.feed_data(frame(0xd1, ["24", [str(room.room_id), "", "0", "0", "XIIIPS2US"]]))
        player_info = (64510).to_bytes(4, "little")
        updated_info = (64511).to_bytes(4, "little")
        reader.feed_data(frame(0xd1, ["42", [player_info]]))
        reader.feed_data(frame(0xd1, ["42", [updated_info]]))
        reader.feed_data(frame(0xd1, ["8", [str(room.room_id)]]))
        reader.feed_eof()
        writer = Writer(guest_ip, 30000)
        host_packets = []
        host_probe = srp_probe.Probe({}, registry)
        host_probe.transport = SimpleNamespace(sendto=lambda packet, peer: host_packets.append((packet, peer)))
        host_peer = (host_ip, 1024)
        host_probe.sessions[host_peer] = srp_probe.Session(0x1234, 0, next_segment=19, room=room)
        await router.handle(reader, writer, None, 1, Path("/unused"), "192.0.2.1", 30000,
                            pending, {}, registry, host_probe)
        offset = 0
        replies = []
        while offset < len(writer.output):
            header = GSHeader.decode(writer.output[offset:offset + HEADER_SIZE])
            payload = bytes(writer.output[offset + HEADER_SIZE:offset + header.size])
            replies.append((header.message_type, data.decode(gsxor.decrypt(payload))))
            offset += header.size
        self.assertEqual(replies[1], (0xd1, ["38", ["46", [str(room.room_id), b"host=0"]]]))
        self.assertEqual(replies[2], (0xd1, ["38", ["24", [str(room.room_id)]]]))
        self.assertEqual(replies[3][1][:1], ["53"])
        self.assertEqual(replies[3][1][1][:2], [str(room.room_id), "64"])
        self.assertEqual(replies[3][1][1][2][13], "2")
        self.assertEqual(replies[4:], [(0xd1, ["38", ["42"]]),
                                        (0xd1, ["56", [str(room.room_id), b"", "7777", host_ip, host_ip]]),
                                        (0xd1, ["38", ["42"]]),
                                        (0xd1, ["38", ["8", [str(room.room_id)]]])])
        self.assertEqual(room.members, set())
        self.assertEqual(room.member_info, {})
        # The guest TCP update reaches only the owning host's UDP channel.
        self.assertEqual([peer for _, peer in host_packets], [host_peer] * 3)
        self.assertEqual([srp_probe.HEADER.unpack_from(packet)[4] for packet, _ in host_packets], [19, 0, 1])
        events = []
        for packet, _ in host_packets:
            offset = srp_probe.HEADER.size
            payload_end = offset + srp_probe.HEADER.unpack_from(packet)[2]
            self.assertEqual(len(packet) % 2, 0)
            while offset < payload_end:
                header = GSHeader.decode(packet[offset:offset + HEADER_SIZE])
                self.assertEqual((header.message_type, header.sender, header.receiver), (0xd1, 2, 4))
                events.append(data.decode(gsxor.decrypt(packet[offset + HEADER_SIZE:offset + header.size])))
                offset += header.size
        self.assertEqual(events, [["50", ["guest", "0", str(room.room_id), guest_ip, "", b"", "0"]],
                                  ["66", ["guest", player_info]], ["66", ["guest", updated_info]],
                                  ["51", ["guest"]]])


class FakeTransport:
    def __init__(self, queued=0):
        self.queued, self.aborted, self.closing = queued, False, False

    def get_write_buffer_size(self):
        return self.queued

    def is_closing(self):
        return self.closing

    def abort(self):
        self.aborted = self.closing = True


class NotificationBacklogTests(unittest.IsolatedAsyncioTestCase):
    def test_a_recipient_over_the_bound_is_disconnected(self):
        transport = FakeTransport()
        written = []
        writer = SimpleNamespace(transport=transport, write=written.append)
        self.assertTrue(router.queue_notification(writer, b"x" * 100, "peer", "lobby"))
        transport.queued = router.NOTIFICATION_BACKLOG_LIMIT - 100
        self.assertTrue(router.queue_notification(writer, b"x" * 100, "peer", "lobby"))
        transport.queued = router.NOTIFICATION_BACKLOG_LIMIT
        self.assertFalse(router.queue_notification(writer, b"x", "peer", "lobby"))
        self.assertTrue(transport.aborted)
        # Nothing more is queued once the connection is closing.
        self.assertFalse(router.queue_notification(writer, b"x", "peer", "lobby"))
        self.assertEqual(len(written), 2)

    async def test_a_stalled_loopback_reader_stays_bounded(self):
        accepted = asyncio.get_running_loop().create_future()

        async def handler(reader, writer):
            accepted.set_result(writer)
            await reader.read()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
        try:
            writer = await asyncio.wait_for(accepted, 5)
            writer.transport.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            frame = bytes(1024)
            for count in range(router.NOTIFICATION_BACKLOG_LIMIT // len(frame) * 64):
                # The client never reads; the sender keeps publishing.
                if not router.queue_notification(writer, frame, "stalled", "lobby"):
                    break
                self.assertLessEqual(writer.transport.get_write_buffer_size(), router.NOTIFICATION_BACKLOG_LIMIT)
                if count % 64 == 0:
                    await asyncio.sleep(0)
            else:
                self.fail("a stalled reader was never disconnected")
            self.assertTrue(writer.transport.is_closing())
        finally:
            sock.close()
            server.close()
            await server.wait_closed()
