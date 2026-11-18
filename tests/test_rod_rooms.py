"""187 native contract fixtures and full local GS room lifecycle."""
import asyncio
from dataclasses import asdict
import ipaddress
import struct
from tempfile import TemporaryDirectory
import unittest

from ubisoft import accounts, data, gsxor, router, service
from ubisoft.frame import GSHeader, HEADER_SIZE
from ubisoft.games.registry import BIA, GR2, R6, ROD, XIII
from ubisoft.games.rod import GAME_PORT, PlayerInfo, session_port
from ubisoft.nat import Discovery
from ubisoft.rooms import LobbyMember, RoomRegistry
from ubisoft.srp_probe import HEADER, WINDOW, IDENT, SYN

from test_games import GSClient
from test_nat import Transport, fields as nat_fields
from test_r6_profile import reserve_ports


def player_info(address="198.51.100.7", port=7777, nat=True):
    # Two flag bytes, two opaque bytes, host-order address, port, padding.
    return struct.pack("<BBHIHH", 0, nat, 0, int(ipaddress.IPv4Address(address)), port, 0)


def session_info(port=7777, players=4, map_id=1):
    info = bytearray(36)
    struct.pack_into("<I", info, 0, map_id)
    struct.pack_into("<H", info, 28, players)
    struct.pack_into("<H", info, 32, port)
    return bytes(info)


def create_fields(password="", info=None):
    return ["1", "Fixture room", ROD.product, "5", "4", "0",
            session_info() if info is None else info, password, "1.0", ROD.sdk_version, b""]


class RodRoomTests(unittest.TestCase):
    def test_player_record_decodes_187_offsets_and_byte_order(self):
        info = player_info()
        parsed = PlayerInfo.decode(info)
        self.assertEqual((parsed.nat, str(parsed.address), parsed.port), (True, "198.51.100.7", 7777))
        self.assertEqual(ROD.parent_player_info([info]), info)
        self.assertEqual(ROD.room_player_info([info]), info)
        self.assertEqual(ROD.player_port(info), 7777)
        self.assertEqual(ROD.player_endpoint(info, "198.51.100.7"), "peer:7777")
        self.assertEqual(ROD.player_endpoint(info, "192.0.2.9"), "other:7777")
        self.assertFalse(PlayerInfo.decode(player_info(nat=False)).nat)
        for invalid in (b"", bytes(11), bytes(13), bytes(24), "text"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ROD.room_player_info([invalid])

    def test_parent_membership_and_catalog_are_title_scoped(self):
        self.assertEqual(ROD.join_parent_lobby(["1", "", "448"]), ["1", ""])
        self.assertTrue(ROD.self_member_event and ROD.product_room_catalog)
        ROD.room_catalog([ROD.product])
        for other in (XIII, GR2, R6, BIA):
            self.assertFalse(other.product_room_catalog)
            with self.assertRaises(ValueError):
                ROD.room_catalog([other.product])
        for fields in ([], [ROD.product, "extra"], [ROD.product.encode()]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ROD.room_catalog(fields)
        for fields in (["2", "", "0"], ["1", "secret", "0"], ["1", "", "-1"], ["1", ""]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ROD.join_parent_lobby(fields)
        self.assertEqual(ROD.group_refresh(["1", "64"]), (False, False))
        self.assertEqual(ROD.group_refresh(["1", "448"]), (True, False))
        self.assertFalse(ROD.room_join_resends_members)
        self.assertTrue(R6.room_join_resends_members and BIA.room_join_resends_members)

    def test_native_registration_and_endpoints_preserve_the_session_blob(self):
        registry = RoomRegistry()
        original = session_info()
        room = registry.register("host", "192.0.2.2", create_fields("fixture", original), ROD)
        self.assertLess(room.room_id, 0)
        self.assertEqual((room.info, room.password, room.game_port, room.flags),
                         (original, "fixture", "7777", "1"))
        self.assertEqual(ROD.registration(create_fields(), "192.0.2.2").flags, "0")
        self.assertEqual(ROD.host_endpoint(room, room.peer_ip), "peer:7777")
        self.assertEqual(session_port(session_info(port=0)), GAME_PORT)
        self.assertEqual(room.group_fields()[9:13], [ROD.product, original, "0", "4"])
        self.assertEqual(len(room.group_fields()), 20)
        self.assertEqual(ROD.lobby_create_result(room), [str(room.room_id), "", "1"])
        self.assertEqual(ROD.room_join_result(room), [str(room.room_id), ""])
        self.assertEqual(ROD.room_join_failure(room, "password"), ["11", str(room.room_id), ""])
        self.assertEqual(ROD.room_join_failure(room, "full"), ["7", str(room.room_id), ""])
        for index, invalid in ((0, "2"), (1, ""), (2, BIA.product), (3, "6"), (4, "0"),
                               (4, "65"), (5, "1"), (6, bytes(35)), (6, bytes(37)),
                               (7, "x" * 33), (8, "other"), (9, BIA.sdk_version), (10, bytes(16385))):
            values = create_fields()
            values[index] = invalid
            with self.subTest(index=index, invalid=str(invalid)[:35]), self.assertRaises(ValueError):
                ROD.registration(values, "192.0.2.2")

    def test_member_notifications_do_not_use_bia_or_r6_record_sizes(self):
        member = LobbyMember("Fixture", ROD.product, "192.0.2.2", "", lambda event: None,
                             info=player_info(), group="-1000")
        self.assertEqual(ROD.lobby_member_event(member), ["50", [
            "Fixture", "0", "-1000", "192.0.2.2", "192.0.2.2", player_info(), "0"]])
        self.assertEqual(ROD.lobby_member_left(member), ["51", ["Fixture", "-1000"]])
        self.assertEqual(ROD.lobby_player_update(member), ["66", ["Fixture", player_info()]])
        member.info = bytes(24)
        with self.assertRaises(ValueError):
            ROD.lobby_member_event(member)

    def test_masked_updates_validate_before_mutating_and_push_new_settings(self):
        room = RoomRegistry().register("host", "192.0.2.2", create_fields(), ROD)
        rid = str(room.room_id)
        info = session_info(8888, map_id=7)
        ROD.room_update(room, 31, [rid, "64", info])
        self.assertEqual((room.info, room.game_port), (info, "8888"))
        self.assertEqual(ROD.room_update_event(room, 31), ["53", [rid, "64", room.group_fields()]])
        # Full mask: flag values then limits, name, session info and extra.
        mask = 0x206 | 0x8 | 0x10 | 0x20 | 0x40 | 0x400
        ROD.room_update(room, 31, [rid, str(mask), str(0x4810), "8", "0", "Renamed", info, b"extra"])
        self.assertEqual((room.flags, room.max_players, room.name, room.additional_info),
                         (str(0x4810), "8", "Renamed", b"extra"))
        protected = RoomRegistry().register("host", "192.0.2.2", create_fields("fixture"), ROD)
        ROD.room_update(protected, 31, [str(protected.room_id), "2", "0"])
        self.assertEqual(protected.flags, "1")  # password prompt stays enabled
        saved = asdict(room)
        for values in ([rid, "64", bytes(80)], [rid, "72", "4", bytes(35)],
                       [rid, "32", ""], [rid, "1"], [rid, "64", info, "extra"],
                       [rid, "1040", "1", b"extra"], [rid, "2"], [rid, "8", "0"]):
            with self.subTest(values=str(values)[:80]), self.assertRaises(ValueError):
                ROD.room_update(room, 31, values)
            self.assertEqual(asdict(room), saved)

    def test_owner_update_and_match_reply_shapes(self):
        room = RoomRegistry().register("host", "192.0.2.2", create_fields(), ROD)
        rid = str(room.room_id)
        self.assertEqual(ROD.room_update(room, 15, [rid]), [rid])
        self.assertEqual(ROD.room_update(room, 17, [rid, "0", "0"]), [rid])
        self.assertEqual(ROD.match_started_event(room, 12), ["62", [rid, "12"]])
        self.assertEqual(ROD.room_update(room, 33, [rid, b"", "8888", "198.51.100.8"]), [rid])
        self.assertEqual((room.game_port, room.local_ip), ("8888", "198.51.100.8"))
        ROD.room_update(room, 33, [rid, b"", "0", "192.0.2.100"])
        self.assertEqual((room.game_port, room.local_ip), ("8888", "198.51.100.8"))
        for subtype, values in ((17, [rid, "0"]), (17, [rid, "0", "256"]),
                                (33, [rid, b"", "70000", ""]), (15, ["-999"])):
            with self.subTest(subtype=subtype), self.assertRaises(ValueError):
                ROD.room_update(room, subtype, values)
        for subtype in ROD.room_notice_subtypes:
            ROD.room_notice(room, subtype, [rid])
            expected = ["38", ["45", [rid]]] if subtype == 45 else None
            self.assertEqual(ROD.room_notice_reply(room, subtype), expected)
        self.assertFalse(ROD.game_started or ROD.owner_game_started)


class RodNatTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_nat_request_token_and_host_order_address(self):
        helper = Discovery("192.0.2.0/24", address_byteorder=ROD.nat_address_byteorder)
        transport = Transport()
        helper.connection_made(transport)
        self.addCleanup(helper.connection_lost, None)
        control = ("192.0.2.7", 6779)
        helper.datagram_received(HEADER.pack(0, 0, 8, IDENT | SYN, 0, 0)
                                 + WINDOW.pack(10, 0x1234, 0, 536), control)
        payload = gsxor.encrypt(data.encode(["1", ["7", "Fixture", ROD.product, ROD.sdk_version]]))
        frame = GSHeader(HEADER_SIZE + len(payload), 0, 1, 0xdd, 8, 2).encode() + payload
        helper.datagram_received(HEADER.pack(0, 0x1234, len(frame), IDENT, 0, 0) + frame, control)
        _, reply = nat_fields(transport.sent[-1][0])
        self.assertEqual((reply[0], reply[1][0], reply[1][2]), ("2", "7", "30002"))
        # 0x0043dc30 applies htonl to this number and sends those four bytes.
        token = int(reply[1][1]).to_bytes(4, "big")
        helper.datagram_received(token, (control[0], 7777))
        _, reply = nat_fields(transport.sent[-1][0])
        self.assertEqual(reply, ["3", ["7", str(int(ipaddress.IPv4Address(control[0]))), "7777"]])
        raw = struct.pack("<BBHIHH", 0, 1, 0, int(reply[1][1]), int(reply[1][2]), 0)
        self.assertEqual(str(PlayerInfo.decode(raw).address), control[0])


class RodLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.clients = []
        self.addAsyncCleanup(self.stop)
        reservations, ports = reserve_ports(7)
        self.ports = ports
        try:
            env = {"TUBISOFT_ADVERTISE_IP": "127.0.0.1", "TUBISOFT_BIND_IP": "127.0.0.1",
                   "TUBISOFT_STATE_DIR": self.directory.name, "TUBISOFT_GAMES": "xiii,rod",
                   "TUBISOFT_HTTP_PORT": str(ports[6])}
            for prefix, selected in (("TUBISOFT_", ports[:3]), ("TUBISOFT_ROD_", ports[3:6])):
                for role, port in zip(("ROUTER", "LAUNCHER", "NAT"), selected):
                    env[prefix + role + "_PORT"] = str(port)
            self.args = service.configuration(env | getattr(self, "relay_environment", {}))
        finally:
            for sock in reservations:
                sock.close()
        self.task = asyncio.create_task(service.serve(self.args))
        for attempt in range(100):
            if self.task.done():
                await self.task
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", ports[6])
                writer.write(b"GET /healthz HTTP/1.0\r\n\r\n")
                await writer.drain()
                self.assertIn(b"200 OK", await asyncio.wait_for(reader.read(), 2))
                writer.close()
                await writer.wait_closed()
                break
            except ConnectionRefusedError:
                if attempt == 99:
                    raise
                await asyncio.sleep(0.01)
        self.private = router.load_key(self.args.private_key)

    async def stop(self):
        for client in self.clients:
            client.writer.close()
        for client in self.clients:
            await client.writer.wait_closed()
        if hasattr(self, "task"):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.directory.cleanup()

    async def connect(self, title=ROD):
        port = self.ports[0 if title is XIII else 3]
        client = GSClient(*await asyncio.open_connection(getattr(self, "server_address", "127.0.0.1"), port))
        self.clients.append(client)
        await client.handshake(self.private)
        return client

    async def receive(self, client, expected):
        header, fields = await client.receive()
        self.assertEqual(header.message_type, 0xd1)
        self.assertEqual(fields, expected)
        return fields

    async def wait_login(self, name, title=ROD):
        creator = await self.connect(XIII)
        await creator.send(1, ["1.0", name, "fixture-only", "", "", "", ""], encrypted=True)
        header, fields = await creator.receive()
        self.assertEqual((header.message_type, fields), (0x26, [b"\x01"]))
        login = await self.connect(title)
        await login.send(0x66, [name, "fixture-only", title.sdk_version, b"\1"], encrypted=True)
        header, fields = await login.receive()
        self.assertEqual((header.message_type, fields), (0x26, [b"\x66"]))
        await login.send(0x5d)
        await login.receive()
        wait = await self.connect(title)
        await wait.send(0x4d, [name])
        header, fields = await wait.receive()
        self.assertEqual((header.message_type, fields), (0x26, [b"\x4d"]))
        return wait

    async def lobby_login(self, name):
        wait = await self.wait_login(name)
        await wait.send(0xd1, ["3", ["1"]])
        await self.receive(wait, ["38", ["3", ["1", "127.0.0.1", str(self.ports[3])]]])
        lobby = await self.connect()
        await lobby.send(0xd2, [name, "1", "192.0.2.100", "0.0.0.0", "0"])
        header, fields = await lobby.receive()
        self.assertEqual((header.message_type, fields), (0x26, ["210", ["1"]]))
        await lobby.send(0xd1, ["23", ["1", "", "448"]])
        await self.receive(lobby, ["38", ["23", ["1", ""]]])
        _, fields = await lobby.receive()
        self.assertEqual(fields[0], "53")
        self.assertEqual(fields[1][2][9], ROD.product)
        # Read existing members and then the self event in server order.
        while True:
            _, event = await lobby.receive()
            self.assertEqual(event[0], "50")
            if event[1][0] == name:
                break
        info = player_info()
        await lobby.send(0xd1, ["42", [info]])
        await self.receive(lobby, ["38", ["42"]])
        lobby.name, lobby.wait, lobby.info = name, wait, info
        return lobby

    async def heartbeat(self, client):
        await client.send(0x3a)
        header, fields = await client.receive()
        self.assertEqual((header.message_type, fields), (0x3a, []))

    async def operation_reply(self, client):
        # Other parent members receive asynchronous room/player notices.
        for _ in range(30):
            _, event = await client.receive()
            if event[0] not in ("50", "51", "53", "55", "66"):
                return event
        self.fail("no operation reply after room/player notices")

    async def test_account_errors_reply_without_creating_or_changing_accounts(self):
        await self.wait_login("AuthFixture")
        cases = (("AbsentFixture", "fixture-only", 4), ("AUTHFIXTURE", "incorrect", 15))
        for name, password, error in cases:
            client = await self.connect()
            client.route = (4, 1)  # retail 187's account-message route
            with self.assertLogs("ubisoft.router", level="INFO") as logs:
                await client.send(0x66, [name, password, ROD.sdk_version, b"\1"], encrypted=True)
                header, fields = await client.receive()
                self.assertEqual((header.message_type, fields),
                                 (0x27, [b"\x66", [error.to_bytes(4, "little")]]))
                self.assertEqual((header.sender, header.receiver), (1, 4))
                self.assertEqual(await asyncio.wait_for(client.reader.read(), 2), b"")
            self.assertNotIn(name, " ".join(logs.output))
            self.assertNotIn(password, " ".join(logs.output))
        self.assertIsNone(accounts.find_account(self.args.account_db, "AbsentFixture"))
        duplicate = await self.connect()
        await duplicate.send(1, ["1.0", "AUTHFIXTURE", "replacement", "", "", "", ""], encrypted=True)
        header, fields = await duplicate.receive()
        self.assertEqual((header.message_type, fields), (0x27, [b"\x01", [b"\x01\0\0\0"]]))
        self.assertTrue(accounts.verify_account(self.args.account_db, "authfixture", "fixture-only"))
        self.assertFalse(accounts.verify_account(self.args.account_db, "authfixture", "replacement"))
        # A rejected attempt neither authenticates nor grants a wait handoff.
        wrong_wait = await self.connect()
        await wrong_wait.send(0x4d, ["AbsentFixture"])
        self.assertEqual(await asyncio.wait_for(wrong_wait.reader.read(), 2), b"")
        valid = await self.connect()
        await valid.send(0x66, ["AUTHFIXTURE", "fixture-only", ROD.sdk_version, b"\1"], encrypted=True)
        header, fields = await valid.receive()
        self.assertEqual((header.message_type, fields), (0x26, [b"\x66"]))
        for title in (XIII, GR2, R6, BIA):
            self.assertIsNone(title.account_failure(0x66, "unknown-account"))

    async def test_news_keeps_wait_and_lobby_connections_usable(self):
        lobby = await self.lobby_login("NewsFixture")
        for language in ("en", "fr", "es"):
            await lobby.wait.send(0xde, [language])
            header, fields = await lobby.wait.receive()
            self.assertEqual((header.message_type, fields), (0x26, [b"\xde", list(ROD.news)]))
            await self.heartbeat(lobby.wait)
            await self.heartbeat(lobby)
        # Opening News must not discard the authenticated directory session.
        await lobby.wait.send(0xd1, ["109", [ROD.product]])
        parent = ROD.parent_lobby()
        parent[13] = "1"  # The signed-in player is still in the parent lobby.
        await self.receive(lobby.wait, ["53", ["1", "256", ["0"], [parent]]])

    async def test_full_room_rejects_extra_join_preserves_owner_slot_and_allows_retry(self):
        host = await self.lobby_login("CapacityHost")
        first = await self.lobby_login("CapacityFirst")
        second = await self.lobby_login("CapacitySecond")

        reply = self.operation_reply
        values = create_fields(info=session_info(players=2))
        values[4] = "2"
        await host.send(0xd1, ["12", values])
        rid = (await reply(host))[1][1][0]
        join = ["24", [rid, "", "448", "0", "1.0"]]
        # Both joiners race for the single non-owner slot before the owner
        # self-joins. Account names differ but all share one observed IP.
        await asyncio.gather(first.send(0xd1, join), second.send(0xd1, join))
        results = await asyncio.gather(reply(first), reply(second))
        self.assertCountEqual(results, [["38", ["24", [rid, ""]]],
                                        ["39", ["24", ["7", rid, ""]]]])
        winner, refused = ((first, second) if results[0][0] == "38" else (second, first))
        await self.heartbeat(refused)
        # A full room must still admit its reserved owner and an existing
        # member's retry; neither adds an extra occupied slot.
        for client in (host, winner):
            await client.send(0xd1, join)
            self.assertEqual(await reply(client), ["38", ["24", [rid, ""]]])
            _, own = await client.receive()
            self.assertEqual((own[0], own[1][0]), ("50", client.name))
            _, room = await client.receive()
            self.assertEqual((room[0], room[1][2][13]), ("53", "2"))
        await refused.send(0xd1, join)
        self.assertEqual(await reply(refused), ["39", ["24", ["7", rid, ""]]])
        # Leaving releases the slot immediately, without dropping the
        # refused client's sign-in/parent-lobby connection.
        await winner.send(0xd1, ["8", [rid]])
        self.assertEqual(await reply(winner), ["38", ["8", [rid]]])
        await refused.send(0xd1, join)
        self.assertEqual(await reply(refused), ["38", ["24", [rid, ""]]])
        _, own = await refused.receive()
        self.assertEqual((own[0], own[1][0]), ("50", refused.name))
        _, room = await refused.receive()
        self.assertEqual((room[0], room[1][2][13]), ("53", "2"))

    async def test_refused_switch_to_full_room_preserves_current_hosted_room(self):
        target = await self.lobby_login("SwitchTarget")
        owner = await self.lobby_login("SwitchOwner")
        reply = self.operation_reply

        async def create_one_player_room(client):
            values = create_fields(info=session_info(players=1))
            values[4] = "1"
            await client.send(0xd1, ["12", values])
            rid = (await reply(client))[1][1][0]
            await client.send(0xd1, ["24", [rid, "", "448", "0", "1.0"]])
            self.assertEqual(await reply(client), ["38", ["24", [rid, ""]]])
            return rid

        target_id = await create_one_player_room(target)
        source_id = await create_one_player_room(owner)
        await owner.send(0xd1, ["24", [target_id, "", "448", "0", "1.0"]])
        self.assertEqual(await reply(owner), ["39", ["24", ["7", target_id, ""]]])
        # Refusal must occur before unbinding/removing the owner's current
        # room, or discarding its existing joined-room membership.
        await owner.send(0xd1, ["31", [source_id, "64", session_info(players=1, map_id=2)]])
        self.assertEqual(await reply(owner), ["38", ["31", [source_id]]])
        await owner.send(0xd1, ["42", [owner.info]])
        self.assertEqual(await reply(owner), ["38", ["42"]])
        await owner.wait.send(0xd1, ["109", [ROD.product]])
        _, catalog = await owner.wait.receive()
        self.assertEqual({record[2] for record in catalog[1][3]}, {"1", source_id, target_id})

    async def test_two_players_protected_room_refresh_next_round_and_host_exit(self):
        host = await self.lobby_login("HostFixture")
        guest = await self.lobby_login("GuestFixture")
        # Guest publication is delivered to the existing host.
        _, event = await host.receive()
        self.assertEqual((event[0], event[1][0], event[1][5]), ("50", guest.name, guest.info))
        await host.send(0xd1, ["12", create_fields("fixture")])
        _, reply = await host.receive()
        self.assertEqual(reply[:1] + reply[1][:1], ["38", "12"])
        rid = reply[1][1][0]
        self.assertLess(int(rid), 0)
        # Both clients receive the created room, then the guest learns its owner.
        for client in (host, guest):
            _, event = await client.receive()
            self.assertEqual(event[0], "53")
            self.assertEqual(event[1][3][0][2], rid)
            self.assertEqual(event[1][3][0][5], "1")  # native password-prompt predicate
        _, event = await guest.receive()
        self.assertEqual(event[0:1] + event[1][:3], ["50", host.name, "0", rid])
        # The creator self-joins after its operation-12 reply.
        await host.send(0xd1, ["24", [rid, "fixture", "448", "0", "1.0"]])
        await self.receive(host, ["38", ["24", [rid, ""]]])
        _, event = await host.receive()
        self.assertEqual((event[0], event[1][0], event[1][2]), ("50", host.name, rid))
        _, event = await host.receive()
        self.assertEqual(event[:1] + event[1][:2], ["53", rid, "64"])
        self.assertEqual(event[1][2][13], "1")
        # Wrong passwords fail without closing the signed-in connection.
        await guest.send(0xd1, ["24", [rid, "wrong", "448", "0", "1.0"]])
        await self.receive(guest, ["39", ["24", ["11", rid, ""]]])
        await self.heartbeat(guest)
        await guest.send(0xd1, ["24", [rid, "fixture", "448", "0", "1.0"]])
        await self.receive(guest, ["38", ["24", [rid, ""]]])
        _, event = await guest.receive()
        self.assertEqual((event[0], event[1][0], event[1][2]), ("50", guest.name, rid))
        _, event = await guest.receive()
        self.assertEqual(event[0], "53")
        self.assertEqual(event[1][2][13], "2")
        # The parent directory already delivered the owner; the native
        # join callback opens those cached peers. Do not add the owner twice.
        _, joined = await host.receive()
        self.assertEqual((joined[0], joined[1][0], joined[1][2]), ("50", guest.name, rid))
        # Player info completion must not invent a GAME_STARTED endpoint.
        for client in (host, guest):
            await client.send(0xd1, ["42", [client.info]])
            await self.receive(client, ["38", ["42"]])
            await self.heartbeat(client)
        # A changed player record updates the existing peer, without a
        # second member-joined event (the native join callback adds peers).
        host.info = b"\1" + host.info[1:]
        await host.send(0xd1, ["42", [host.info]])
        await self.receive(host, ["38", ["42"]])
        await self.receive(guest, ["66", [host.name, host.info]])
        # Product catalog goes through the wait channel, before or after joining.
        await guest.wait.send(0xd1, ["109", [ROD.product]])
        _, catalog = await guest.wait.receive()
        records = catalog[1][3]
        self.assertEqual([record[2] for record in records], ["1", rid])
        self.assertEqual(records[1][9], ROD.product)
        self.assertEqual(records[1][13], "2")
        # Catalog refresh retains member state; replaying joins here would
        # duplicate the game's in-room peers.
        await self.heartbeat(guest.wait)
        # Update the opaque settings for the next race and push to both clients.
        next_info = session_info(8888, map_id=2)
        await host.send(0xd1, ["31", [rid, "64", next_info]])
        await self.receive(host, ["38", ["31", [rid]]])
        for client in (host, guest):
            _, event = await client.receive()
            self.assertEqual(event[:1] + event[1][:2], ["53", rid, "64"])
            self.assertEqual(event[1][2][10], next_info)
        # Start and finish a match twice on the same room/GS connections.
        for match in ("1", "2"):
            # 0x0041ef10 -> 15, whose callback sends 33 with empty data,
            # zero port and empty address. It awaits both completions.
            await host.send(0xd1, ["15", [rid]])
            await self.receive(host, ["38", ["15", [rid]]])
            await host.send(0xd1, ["33", [rid, b"", "0", ""]])
            await self.receive(host, ["38", ["33", [rid]]])
            for client in (host, guest):
                _, event = await client.receive()
                self.assertEqual(event[0], "53")
            await host.send(0xd1, ["17", [rid, "0", "0"]])
            await self.receive(host, ["38", ["17", [rid]]])
            for client in (host, guest):
                _, event = await client.receive()
                self.assertEqual(event[0], "53")
                await self.receive(client, ["62", [rid, match]])
            for subtype in (34, 44, 70):
                await guest.send(0xd1, [str(subtype), [rid]])
                await self.heartbeat(guest)
            await host.send(0xd1, ["45", [rid]])
            await self.receive(host, ["38", ["45", [rid]]])
        # A guest leaves, refreshes and rejoins without signing in again.
        await guest.send(0xd1, ["8", [rid]])
        await self.receive(guest, ["38", ["8", [rid]]])
        await self.receive(guest, ["51", [guest.name, rid]])
        _, event = await guest.receive()
        self.assertEqual((event[0], event[1][0], event[1][2]), ("50", guest.name, "1"))
        await self.receive(host, ["51", [guest.name, rid]])
        _, event = await host.receive()
        self.assertEqual((event[0], event[1][2]), ("50", "1"))
        await guest.send(0xd1, ["9", ["1", "448"]])
        _, event = await guest.receive()
        self.assertEqual([r[2] for r in event[1][3]], [rid])
        await self.heartbeat(guest)
        await guest.send(0xd1, ["24", [rid, "fixture", "448", "0", "1.0"]])
        await self.receive(guest, ["38", ["24", [rid, ""]]])
        await guest.receive()  # self room member
        await guest.receive()  # room info
        await host.receive()  # guest member
        # An owner disconnect removes its room and retains the guest's login.
        host.writer.close()
        await host.writer.wait_closed()
        events = [(await guest.receive())[1] for _ in range(4)]
        self.assertIn(["55", [rid]], events)
        self.assertIn(["51", [guest.name, rid]], events)
        self.assertTrue(any(e[0] == "50" and e[1][0] == guest.name and e[1][2] == "1" for e in events))
        self.assertTrue(any(e[0] == "51" and e[1][0] == host.name for e in events))
        await self.heartbeat(guest)
        await guest.wait.send(0xd1, ["109", [ROD.product]])
        _, catalog = await guest.wait.receive()
        self.assertEqual([r[2] for r in catalog[1][3]], ["1"])
        await self.heartbeat(guest.wait)

    async def test_oversized_room_player_data_is_never_sent_to_the_other_client(self):
        host = await self.lobby_login("HostFixture")
        guest = await self.lobby_login("GuestFixture")
        await host.receive()  # guest's valid initial publication
        await host.send(0xd1, ["12", create_fields()])
        _, reply = await host.receive()
        rid = reply[1][1][0]
        await host.receive()  # creator listing
        await guest.receive()  # room listing
        await guest.receive()  # owner member
        await guest.send(0xd1, ["24", [rid, "", "448", "0", "1.0"]])
        await guest.receive()  # join success
        await guest.receive()  # self room member
        await guest.receive()  # room info
        await host.receive()  # guest member
        await guest.send(0xd1, ["42", [bytes(13)]])
        self.assertEqual(await asyncio.wait_for(guest.reader.read(), 2), b"")
        # Only departure follows; no oversized operation-50/66 publication.
        await self.receive(host, ["51", [guest.name, rid]])
        await self.heartbeat(host)

    async def test_product_catalog_is_not_enabled_for_xiii(self):
        wait = await self.wait_login("OtherFixture", XIII)
        await wait.send(0xd1, ["109", [XIII.product]])
        self.assertEqual(await asyncio.wait_for(wait.reader.read(), 2), b"")
