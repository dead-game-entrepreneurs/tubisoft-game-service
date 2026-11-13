"""Rainbow Six 3 profile: static wire shapes and a real-socket sign-in path."""
import asyncio
from types import SimpleNamespace
import socket
from tempfile import TemporaryDirectory
import unittest

from ubisoft import router, service
from ubisoft.games.base import UnsupportedOperation
from ubisoft.games.registry import GR2, R6, XIII, bindings
from ubisoft.rooms import LobbyMember, RoomRegistry

from test_games import GSClient, registration


def r6_create(name="Fixture room", password="", players="8", group_type="5"):
    # 0x00417dd0 via 0x00404080: parent, name, product, group type, player
    # limit, a constant 0, game data, password, versions, extra data.
    return ["1", name, R6.product, group_type, players, "0", b"game-data", password, "1.0", "RSPS21.0", b""]


def r6_query(app_id, *, product=R6.product, selection="2", columns=("ALIAS", "LEVEL")):
    values = ["0", "10"] if selection in ("0", "3") else ["SharedFixture"]
    if selection == "1":
        values.append("1")
    return ["1", [str(app_id), product, "0", "1", [selection, values], [list(columns), [], []]]]


class R6ProfileTests(unittest.TestCase):
    def test_identity_and_sectioned_bootstrap(self):
        self.assertEqual((R6.product, R6.sdk_version, R6.game_version),
                         ("RAINBOWSIX3PS2US", "RSPS21.0", "1.0"))
        body = R6.bootstrap("192.0.2.1", 30020, 30021, "192.0.2.1", 30022)
        self.assertEqual(body, b"[Servers]\r\nRouterIP0=192.0.2.1\r\nRouterPort0=30020\r\n"
                         b"RouterLauncherPort0=30021\r\nNATServerIP0=192.0.2.1\r\nNATServerPort0=30022\r\n")
        self.assertLess(len(body), 1460)

    def test_discovery_uses_its_own_product_and_fourteen_field_folder(self):
        R6.discovery_login([R6.product])
        for fields in ([], [GR2.product], [XIII.product], [R6.product, "extra"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                R6.discovery_login(fields)
        folder = R6.parent_lobby()
        self.assertEqual(len(folder), 14)
        self.assertEqual((folder[1], folder[9], folder[10]), ("Rainbow Six 3", R6.product, b""))
        self.assertLessEqual(len(folder[1]), 32)

    def test_news_defaults_and_overrides(self):
        # "Latest News" shows the two notices; both have default text.
        self.assertTrue(all(0 < len(item) <= 512 and item.isascii() for item in R6.news))
        base = {"TUBISOFT_GAMES": "r6"}
        (binding,) = bindings(base | {"TUBISOFT_R6_NEWS_1": "Server maintenance on Friday."})
        self.assertEqual(binding.profile.login_notices(["en"]), ["Server maintenance on Friday.", R6.news[1]])
        (binding,) = bindings(base)
        self.assertIs(binding.profile, R6)
        for bad in ("x" * 513, "tab\there", "caf\u00e9"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bindings(base | {"TUBISOFT_R6_NEWS_2": bad})

    def test_login_notices_accept_both_german_codes_used_by_the_disc(self):
        for language in ("en", "ge", "de", "fr", "es", "it"):
            self.assertEqual(R6.login_notices([language]), list(R6.news))
        for fields in ([], ["en", "extra"], [b"en"], [["en"]], ["jp"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                R6.login_notices(fields)
        with self.assertRaises(ValueError):
            GR2.login_notices(["ge"])

    def test_ranking_table_keeps_the_application_id_and_checks_the_product(self):
        # Type-2 self queries need a parsable ALIAS/LEVEL row to pass sign-in.
        self.assertEqual(R6.ranking_table(r6_query(77), "sharedfixture"), ["1", [
            "77", "1", [["ALIAS", "ALIAS"], ["LEVEL", "LEVEL"]], [["SharedFixture", "0"]]]])
        # Around-player and top-list queries stay empty (native status -10).
        top = r6_query(78, selection="3", columns=("ALIAS", "GLOBAL_RANK", "RATING"))
        around = r6_query(79, selection="1", columns=())
        self.assertEqual(R6.ranking_table(top, "sharedfixture"), ["1", ["78", "0", [], []]])
        self.assertEqual(R6.ranking_table(around, "sharedfixture"), ["1", ["79", "0", [], []]])
        self.assertEqual(R6.ranking_table(r6_query(80, columns=()), "x"), ["1", ["80", "0", [], []]])
        for bad in (r6_query(1, columns=("ALIAS", "ALIAS")), r6_query(1, columns=("A" * 40,))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                R6.ranking_table(bad, "SharedFixture")
        for fields in (r6_query(1, product=GR2.product), r6_query(1, selection="4"),
                       ["2", r6_query(1)[1]], ["1", r6_query(1)[1][:5]], ["1", "x"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                R6.ranking_table(fields, "SharedFixture")

    def test_lobby_room_creation_uses_operation_12_not_the_xiii_launcher(self):
        self.assertTrue(R6.supports_rooms and R6.lobby_create)
        self.assertFalse(R6.owner_game_started)
        registry = RoomRegistry()
        with self.assertRaises(ValueError):
            registry.register("shared", "192.0.2.2", registration(R6.product), R6)
        with self.assertRaises(UnsupportedOperation):
            R6.launcher_groups()
        room = registry.register("shared", "192.0.2.2", r6_create(), R6)
        self.assertEqual((room.group_type, room.parent_id, room.max_players, room.max_spectators,
                          room.flags, room.info, room.game_port),
                         ("5", "1", "8", "0", "0", b"game-data", "0"))
        # Game rooms have non-positive IDs; the list shows +0xcc/+0xc8.
        self.assertLess(room.room_id, 0)
        fields = room.group_fields()
        self.assertEqual((fields[0], fields[2], fields[12], fields[14]), ("5", str(room.room_id), "8", "0"))
        self.assertFalse(R6.game_started)
        self.assertEqual(R6.lobby_create_result(room), [str(room.room_id), "", "1"])
        self.assertEqual(R6.room_join_result(room), [str(room.room_id), ""])
        self.assertEqual(len(room.group_fields()), 20)
        for fields in (r6_create(password="secret"), r6_create(name=""), r6_create(players="0"), r6_create(group_type="0"),
                       r6_create()[:10], ["2"] + r6_create()[1:],
                       r6_create()[:2] + [GR2.product] + r6_create()[3:]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                R6.registration(fields, "192.0.2.2")

    def test_owner_room_updates_reply_with_the_room_id(self):
        room = RoomRegistry().register("shared", "192.0.2.2", r6_create(), R6)
        rid = str(room.room_id)
        self.assertEqual(R6.room_update(room, 15, [rid]), [rid])
        self.assertEqual(R6.room_update(room, 17, [rid, "3"]), [rid])
        self.assertEqual(R6.room_update(room, 33, [rid, b"", "3658", "198.51.100.7"]), [rid])
        self.assertEqual((room.game_port, room.local_ip), ("3658", "198.51.100.7"))
        # PCSX2's emulated 192.0.2.100 is not kept as a reachable address.
        self.assertEqual(R6.room_update(room, 33, [rid, b"", "7777", "192.0.2.100"]), [rid])
        self.assertEqual((room.game_port, room.local_ip), ("7777", "198.51.100.7"))
        self.assertEqual(R6.room_update(room, 31, [rid, "64", b"new-data"]), [rid])
        self.assertEqual(room.info, b"new-data")
        for subtype, values in ((15, ["999"]), (15, []), (33, [rid, b"", "70000", ""]), (17, [rid])):
            with self.subTest(subtype=subtype, values=values), self.assertRaises(ValueError):
                R6.room_update(room, subtype, values)
        self.assertIsNone(R6.room_notice(room, 34, [rid]))
        for values in ([], ["999"], [rid, "x"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                R6.room_notice(room, 34, values)

    def test_parent_join_and_its_24_byte_player_record(self):
        for option in ("0", "256", "4294967295"):
            self.assertEqual(R6.join_parent_lobby(["1", "", option]), ["1", ""])
        for fields in ([], ["1", ""], ["2", "", "0"], ["1", "password", "0"], ["1", "", b"0"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                R6.join_parent_lobby(fields)
        record = bytes(range(24))
        self.assertEqual(R6.parent_player_info([record]), record)
        for fields in ([], [bytes(28)], [bytes(23)], ["text"], [record, record]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                R6.parent_player_info(fields)

    def test_parent_member_notifications(self):
        member = LobbyMember("HostFixture", R6.product, "192.0.2.7", "", lambda event: None,
                             info=bytes(24), group="1000")
        self.assertTrue(R6.lobby_members)
        self.assertFalse(XIII.lobby_members)
        self.assertEqual(R6.lobby_member_event(member), ["50", [
            "HostFixture", "0", "1000", "192.0.2.7", "192.0.2.7", bytes(24), "0"]])
        self.assertEqual(R6.lobby_member_left(member), ["51", ["HostFixture", "1000"]])
        with self.assertRaises(UnsupportedOperation):
            XIII.lobby_member_event(member)

    def test_match_start_names_the_room_and_match(self):
        room = SimpleNamespace(room_id=-1000)
        self.assertEqual(R6.match_started_event(room, 7), ["62", ["-1000", "7"]])
        with self.assertRaises(UnsupportedOperation):
            XIII.match_started_event(room, 7)
        entry = ["HostFixture", "19", "20", "1048575", ["6", "1"]]
        self.assertEqual(R6.match_results_event("7", [entry]), ["71", ["7", "38", [[
            "HostFixture", ["0", "0", "1", "1", "0", "0", "0", "1", "0", "0", "0", "0"]]]]])
        # Fields: 0 team, 1-16 kills by victim level, 17 deaths, 18 team
        # kills (suicides included), 19 seconds. In Team Survival the only
        # team with a player still standing wins.
        winner = ["HostFixture", "19", "20", "1048575",
                  ["2", "1", "0", "1"] + ["0"] * 13 + ["0", "0", "25"]]
        loser = ["GuestFixture", "19", "20", "1048575",
                 ["3"] + ["0"] * 16 + ["1", "1", "25"]]
        _, (_, _, records) = R6.match_results_event("8", [winner, loser], "2")
        self.assertEqual(records, [
            ["HostFixture", ["0", "0", "2", "2", "0", "0", "0", "0", "1", "1", "0", "25"]],
            ["GuestFixture", ["0", "0", "-1", "0", "1", "0", "1", "0", "1", "0", "0", "25"]]])
        _, (_, _, records) = R6.match_results_event("9", [winner, loser], "1")
        self.assertEqual([record[1][7:10] for record in records], [["1", "0", "0"], ["1", "0", "0"]])
        for bad in (["HostFixture"], ["", "19", "20", "1", []], ["x" * 33, "1", "1", "1", []],
                    ["HostFixture", "19", "20", "1", ["one"]]):
            with self.assertRaises(ValueError):
                R6.match_results_event("7", [bad])
        self.assertEqual(R6.room_notice_reply(room, 45), ["38", ["45", ["-1000"]]])
        self.assertIsNone(R6.room_notice_reply(room, 70))

    def test_configuration_uses_a_slug_specific_port_prefix(self):
        env = {"TUBISOFT_ADVERTISE_IP": "192.0.2.1", "TUBISOFT_GAMES": "xiii,gr2,r6"}
        games = service.configuration(env).games
        self.assertEqual([g.profile for g in games], [XIII, GR2, R6])
        self.assertEqual((games[2].router_port, games[2].launcher_port, games[2].nat_port),
                         (30020, 30021, 30022))
        with self.assertRaises(ValueError):
            service.configuration(env | {"TUBISOFT_R6_ROUTER_PORT": "30010"})


def reserve_ports(count):
    reservations, ports = [], []
    for _ in range(count):
        for attempt in range(100):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            datagram = socket.socket(type=socket.SOCK_DGRAM)
            try:
                datagram.bind(sock.getsockname())
            except OSError:
                sock.close()
                datagram.close()
                if attempt == 99:
                    raise
                continue
            ports.append(sock.getsockname()[1])
            reservations.extend((sock, datagram))
            break
    return reservations, ports


class R6SignInTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_sign_in_sequence_reaches_the_lobby_server(self):
        with TemporaryDirectory() as directory:
            reservations, ports = reserve_ports(7)
            try:
                env = {"TUBISOFT_ADVERTISE_IP": "127.0.0.1", "TUBISOFT_BIND_IP": "127.0.0.1",
                       "TUBISOFT_STATE_DIR": directory, "TUBISOFT_GAMES": "xiii,r6",
                       "TUBISOFT_HTTP_PORT": str(ports[6])}
                for prefix, selected in (("TUBISOFT_", ports[:3]), ("TUBISOFT_R6_", ports[3:6])):
                    for role, port in zip(("ROUTER", "LAUNCHER", "NAT"), selected):
                        env[prefix + role + "_PORT"] = str(port)
                args = service.configuration(env)
            finally:
                for sock in reservations:
                    sock.close()
            task = asyncio.create_task(service.serve(args))
            clients = []
            try:
                for attempt in range(100):
                    if task.done():
                        await task
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", ports[6])
                        writer.write(b"GET /gsinit.php?dp=RAINBOWSIX3PS2US&user=private-fixture HTTP/1.1\r\n"
                                     b"HOST: gsconnect.ubisoft.com\r\n\r\n")
                        await writer.drain()
                        response = await asyncio.wait_for(reader.read(), 2)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except ConnectionRefusedError:
                        if attempt == 99:
                            raise
                        await asyncio.sleep(0.01)
                self.assertIn(b"200 OK", response)
                self.assertIn(f"[Servers]\r\nRouterIP0=127.0.0.1\r\nRouterPort0={ports[3]}\r\n".encode(), response)
                private = router.load_key(args.private_key)

                async def connect(port, route=(1, 2)):
                    client = GSClient(*await asyncio.open_connection("127.0.0.1", port))
                    client.route = route
                    clients.append(client)
                    if route != (8, 11):
                        await client.handshake(private)
                    return client

                # Create the shared account through XIII, then sign in to R6
                # with 0x00410220's three strings plus a one-byte binary.
                xiii = await connect(ports[0])
                await xiii.send(1, ["1.0", "SharedFixture", "fixture-only", "", "", "", ""], encrypted=True)
                header, fields = await xiii.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x01"]))
                r6 = await connect(ports[3])
                await r6.send(0x66, ["SharedFixture", "fixture-only", R6.sdk_version, b"\1"], encrypted=True)
                header, fields = await r6.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x66"]))
                await r6.send(0x5d)
                _, fields = await r6.receive()
                self.assertEqual(fields, [b"\x5d", ["127.0.0.1", ports[3].to_bytes(4, "little")]])
                wait = await connect(ports[3])
                await wait.send(0x4d, ["SharedFixture"])
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4d"]))
                # State machine 0x00541c70: lobby discovery, friends, player
                # info, language notices, ladder, then the lobby address.
                await wait.send(0xd1, ["21", [R6.product]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["21"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["53", ["1", "256", ["0"], [R6.parent_lobby()]]])
                await wait.send(0x4e, [bytes(4), bytes(4)])
                _, fields = await wait.receive()
                self.assertEqual(fields, [b"\x4e"])
                await wait.send(0x54)
                _, fields = await wait.receive()
                self.assertEqual(fields, [b"\x54"])
                await wait.send(0x13, ["SharedFixture"])
                _, fields = await wait.receive()
                self.assertEqual(fields[0], b"\x13")
                await wait.send(0xde, ["ge"])
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\xde", list(R6.news)]))
                await wait.send(0xcc, ["1", ["ladderquery", "0", "0"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["1", ["ladderquery", "0", "0",
                                                      [["1", "127.0.0.1", str(ports[3])]]]]])
                await wait.send(0xcc, ["2", ["1"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["2", ["1"]]])
                login = await connect(ports[3], (8, 11))
                await login.send(0x66, ["SharedFixture", R6.sdk_version])
                header, fields = await login.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x66", []]))
                await login.send(0x5d)
                _, fields = await login.receive()
                ticket = fields[1][0]
                ranking = await connect(ports[3], (8, 11))
                await ranking.send(0x4d, [ticket])
                _, fields = await ranking.receive()
                self.assertEqual(fields, [b"\x4d", []])
                await wait.send(0xcc, [["3", "1"]])
                await ranking.send(0xcc, ["1281", "7", r6_query(41)])
                header, fields = await ranking.receive()
                # The stored ladder answers with every column, the requested
                # ones first; an unplayed account has zeros.
                self.assertEqual((header.message_type, fields[:2], fields[2][0]), (0xcc, ["1281", "7"], "38"))
                table = fields[2][1][1]
                self.assertEqual(table[:2], ["41", "1"])
                self.assertEqual(table[2][:2], [["ALIAS", "ALIAS"], ["LEVEL", "LEVEL"]])
                self.assertEqual(len(table[2]), 15)
                self.assertEqual(table[3], [["SharedFixture"] + ["0"] * 14])
                # A ticket issued for R6 cannot be redeemed on XIII.
                await login.send(0x5d)
                _, fields = await login.receive()
                wrong_title = await connect(ports[0], (8, 11))
                await wrong_title.send(0x4d, [fields[1][0]])
                self.assertEqual(await asyncio.wait_for(wrong_title.reader.read(), 2), b"")
                await wait.send(0xd1, ["3", ["1"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["3", ["1", "127.0.0.1", str(ports[3])]]])
                # 0x00428f30 sends five lobby-login fields (str, num, str, str, num).
                lobby = await connect(ports[3])
                await lobby.send(0xd2, ["SharedFixture", "1", "192.0.2.5", "0.0.0.0", "0"])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["210", ["1"]])
                await lobby.send(0x3a)
                header, _ = await lobby.receive()
                self.assertEqual(header.message_type, 0x3a)
                # Selecting "Rainbow Six 3" joins the parent (0x004177e0),
                # then publishes the 24-byte player record (operation 42).
                await lobby.send(0xd1, ["23", ["1", "", "256"]])
                header, fields = await lobby.receive()
                self.assertEqual((header.message_type, fields), (0xd1, ["38", ["23", ["1", ""]]]))
                _, fields = await lobby.receive()
                # The parent record counts the players in the lobby (field
                # 13), the joining player included.
                self.assertEqual(fields, ["53", ["1", "256", R6.parent_lobby(1), []]])
                await lobby.send(0xd1, ["42", [bytes(range(24))]])
                header, fields = await lobby.receive()
                self.assertEqual((header.message_type, fields), (0xd1, ["38", ["42"]]))
                await lobby.send(0xd1, ["9", ["1"]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["53", ["1", "256", R6.parent_lobby(1), []]])
                # Create Game (0x00417dd0) through the lobby server.
                await lobby.send(0xd1, ["12", r6_create()])
                header, fields = await lobby.receive()
                self.assertEqual(header.message_type, 0xd1)
                self.assertEqual(fields[:1] + fields[1][:1], ["38", "12"])
                room_id, _, parent = fields[1][1]
                self.assertEqual(parent, "1")
                await lobby.send(0xd1, ["9", ["1", "256"]])
                _, fields = await lobby.receive()
                children = fields[1][3]
                self.assertEqual([child[2] for child in children], [room_id])
                self.assertEqual(children[0][1], "Fixture room")
                # The owner joins its room, publishes player data (no
                # GAME_STARTED for the owner) and updates its address.
                await lobby.send(0xd1, ["24", [room_id, "", "0", "0", "1.0"]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["38", ["24", [room_id, ""]]])
                _, fields = await lobby.receive()
                self.assertEqual(fields[:1] + fields[1][:2], ["53", room_id, "64"])
                await lobby.send(0xd1, ["42", [bytes(24)]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["38", ["42"]])
                await lobby.send(0xd1, ["33", [room_id, b"", "3658", "192.0.2.5"]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["38", ["33", [room_id]]])
                await lobby.send(0xd1, ["15", [room_id]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["38", ["15", [room_id]]])
                # Room reports must not close the owner's lobby connection;
                # only the owner's end-of-round report (45) is completed.
                for subtype in ("34", "70", "45"):
                    await lobby.send(0xd1, [subtype, [room_id]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["38", ["45", [room_id]]])
                await lobby.send(0x3a)
                header, _ = await lobby.receive()
                self.assertEqual(header.message_type, 0x3a)
                await lobby.send(0xd1, ["9", ["1", "256"]])
                _, fields = await lobby.receive()
                self.assertEqual([child[2] for child in fields[1][3]], [room_id])
                # Leaving removes the owner's room.
                await lobby.send(0xd1, ["8", [room_id]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["38", ["8", [room_id]]])
                await lobby.send(0xd1, ["9", ["1", "256"]])
                _, fields = await lobby.receive()
                self.assertEqual(fields[1][3], [])
                await lobby.send(0xd1, ["24", [room_id, "", "0", "0", "1.0"]])
                self.assertEqual(await asyncio.wait_for(lobby.reader.read(), 2), b"")
            finally:
                for client in clients:
                    client.writer.close()
                    await client.writer.wait_closed()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class R6ParentMemberTests(unittest.IsolatedAsyncioTestCase):
    async def test_guests_learn_the_room_owner_and_the_owner_learns_guests(self):
        with TemporaryDirectory() as directory:
            reservations, ports = reserve_ports(7)
            try:
                env = {"TUBISOFT_ADVERTISE_IP": "127.0.0.1", "TUBISOFT_BIND_IP": "127.0.0.1",
                       "TUBISOFT_STATE_DIR": directory, "TUBISOFT_GAMES": "xiii,r6",
                       "TUBISOFT_HTTP_PORT": str(ports[6])}
                for prefix, selected in (("TUBISOFT_", ports[:3]), ("TUBISOFT_R6_", ports[3:6])):
                    for role, port in zip(("ROUTER", "LAUNCHER", "NAT"), selected):
                        env[prefix + role + "_PORT"] = str(port)
                args = service.configuration(env)
            finally:
                for sock in reservations:
                    sock.close()
            task = asyncio.create_task(service.serve(args))
            clients = []
            try:
                private = None

                async def connect(port):
                    nonlocal private
                    for attempt in range(100):
                        if task.done():
                            await task
                        try:
                            client = GSClient(*await asyncio.open_connection("127.0.0.1", port))
                            break
                        except ConnectionRefusedError:
                            if attempt == 99:
                                raise
                            await asyncio.sleep(0.01)
                    client.route = (1, 2)
                    clients.append(client)
                    if private is None:
                        private = router.load_key(args.private_key)
                    await client.handshake(private)
                    return client

                async def lobby_member(user):
                    xiii = await connect(ports[0])
                    await xiii.send(1, ["1.0", user, "fixture-only", "", "", "", ""], encrypted=True)
                    header, fields = await xiii.receive()
                    self.assertEqual((header.message_type, fields), (0x26, [b"\x01"]))
                    r6 = await connect(ports[3])
                    await r6.send(0x66, [user, "fixture-only", R6.sdk_version, b"\1"], encrypted=True)
                    header, _ = await r6.receive()
                    self.assertEqual(header.message_type, 0x26)
                    await r6.send(0x5d)
                    await r6.receive()
                    wait = await connect(ports[3])
                    await wait.send(0x4d, [user])
                    header, _ = await wait.receive()
                    self.assertEqual(header.message_type, 0x26)
                    await wait.send(0xd1, ["3", ["1"]])
                    _, fields = await wait.receive()
                    self.assertEqual(fields, ["38", ["3", ["1", "127.0.0.1", str(ports[3])]]])
                    lobby = await connect(ports[3])
                    await lobby.send(0xd2, [user, "1", "192.0.2.5", "0.0.0.0", "0"])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields, ["210", ["1"]])
                    await lobby.send(0xd1, ["23", ["1", "", "256"]])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields, ["38", ["23", ["1", ""]]])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields[0], "53")
                    lobby.wait = wait
                    lobby.players = int(fields[1][2][13])
                    return lobby, fields[1][3]

                host_info, guest_info = bytes(range(24)), bytes(range(24, 48))
                host, children = await lobby_member("HostFixture")
                self.assertEqual(children, [])
                await host.send(0xd1, ["42", [host_info]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["42"]])
                await host.send(0xd1, ["12", r6_create()])
                _, fields = await host.receive()
                room_id = fields[1][1][0]
                self.assertTrue(room_id.startswith("-"))
                await host.send(0xd1, ["24", [room_id, "", "0", "0", "1.0"]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["24", [room_id, ""]]])
                _, fields = await host.receive()
                self.assertEqual(fields[1][2][7], "HostFixture")

                # The child record carries the owner's exact sign-in name,
                # and the joining guest receives the owner as a room member.
                self.assertEqual(host.players, 1)
                guest, children = await lobby_member("GuestFixture")
                self.assertEqual(guest.players, 2)
                self.assertEqual([(child[2], child[7]) for child in children], [(room_id, "HostFixture")])
                _, fields = await guest.receive()
                self.assertEqual(fields, ["50", ["HostFixture", "0", room_id, "127.0.0.1", "127.0.0.1",
                                                 host_info, "0"]])
                # The guest's player data reaches the owner (PERMIT PING).
                await guest.send(0xd1, ["42", [guest_info]])
                _, fields = await guest.receive()
                self.assertEqual(fields, ["38", ["42"]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["50", ["GuestFixture", "0", "1", "127.0.0.1", "127.0.0.1",
                                                 guest_info, "0"]])
                # The lobby player list asks for other members' player info;
                # an empty or unknown name is a GS failure, not a disconnect.
                await host.wait.send(0x13, ["guestfixture"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields),
                                 (0x26, [b"\x13", ["GuestFixture", "GuestFixture", "", "", "", "", ""]]))
                for name in ("", "NobodyFixture"):
                    await host.wait.send(0x13, [name])
                    header, fields = await host.wait.receive()
                    self.assertEqual((header.message_type, fields), (0x27, [b"\x13", [bytes(4)]]))
                # Player search: one 0x26 per match (not the searcher), then
                # 0x27 with code 0x30 to end the results.
                await host.wait.send(0x08, ["fixture", "", "", "", "", "0", ""])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields),
                                 (0x26, [b"\x08", ["GuestFixture", bytes(4), ""]]))
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x27, [b"\x08", [b"\x30\0\0\0"]]))
                await host.wait.send(0x08, ["nobody", "", "", "", "", "0", ""])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x27, [b"\x08", [b"\x30\0\0\0"]]))
                # Friends: login lists none; adding replies with the name and
                # pushes a 0x4a record (online in the parent lobby).
                await host.wait.send(0x4e, [bytes(4), bytes(4)])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4e"]))
                await host.wait.send(0x4b, ["guestfixture", "group", bytes(4)])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4b", "GuestFixture"]))
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields),
                                 (0x4a, ["GuestFixture", bytes(4), "group", bytes(4), bytes(4), ""]))
                for name in ("NobodyFixture", "hostfixture"):
                    await host.wait.send(0x4b, [name, "group", bytes(4)])
                    header, fields = await host.wait.receive()
                    self.assertEqual((header.message_type, fields), (0x27, [b"\x4b", [bytes(4)]]))
                # A second friends login on the same account lists the friend.
                await host.wait.send(0x4e, [bytes(4), bytes(4)])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4e"]))
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields[0], fields[2]), (0x4a, "GuestFixture", "group"))
                # Messages reach a recipient with a friends channel as 0x65.
                await guest.wait.send(0x4e, [bytes(4), bytes(4)])
                header, fields = await guest.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4e"]))
                # Watchers get a fresh 0x4a when a friend's presence changes.
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields[:2]), (0x4a, ["GuestFixture", bytes(4)]))
                await host.wait.send(0x58, [["guestfixture"], "fixture text"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x58", "guestfixture"]))
                header, fields = await guest.wait.receive()
                self.assertEqual((header.message_type, fields[:2]), (0x65, ["HostFixture", "fixture text"]))
                # Time as parsed by 0x003fef10: YYY (year-1900) MM (month-1) DD hh mm ss.
                self.assertRegex(fields[2], r"^1[0-9]{2}(0[0-9]|1[01])[0-3][0-9][0-2][0-9][0-5][0-9][0-5][0-9]$")
                await host.wait.send(0x58, [["NobodyFixture"], "fixture text"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x27, [b"\x58", [bytes(4), "NobodyFixture"]]))
                # Ignore list: add (0x4f) replies [op, [name]]; a message from
                # an ignored player is dropped without telling the sender; the
                # list (0x54) is followed by one pushed 0x55 [name] per entry;
                # remove (0x50) replies the same way; failures are 0x27.
                await host.wait.send(0x4f, ["guestfixture"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4f", ["GuestFixture"]]))
                await guest.wait.send(0x58, [["hostfixture"], "ignored text"])
                header, fields = await guest.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x58", "hostfixture"]))
                await host.wait.send(0x54)
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x54"]))
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x55, ["GuestFixture"]))
                for name in ("NobodyFixture", "hostfixture"):
                    await host.wait.send(0x4f, [name])
                    header, fields = await host.wait.receive()
                    self.assertEqual((header.message_type, fields), (0x27, [b"\x4f", [bytes(4), [name]]]))
                await host.wait.send(0x50, ["guestfixture"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x50", ["guestfixture"]]))
                await host.wait.send(0x50, ["guestfixture"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x27, [b"\x50", [bytes(4), ["guestfixture"]]]))
                await guest.wait.send(0x58, [["hostfixture"], "after unignore"])
                header, fields = await guest.wait.receive()
                self.assertEqual(header.message_type, 0x26)
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields[:2]), (0x65, ["GuestFixture", "after unignore"]))
                # Personal-info updates store only the country (field 5); it
                # comes back as field 4 of player info (0x13), own or other.
                await host.wait.send(0x62, ["", "a", "b", "c", "d", "CA"], encrypted=True)
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x62"]))
                for client in (host.wait, guest.wait):
                    await client.send(0x13, ["hostfixture" if client is guest.wait else "HostFixture"])
                    header, fields = await client.receive()
                    self.assertEqual((header.message_type, fields),
                                     (0x26, [b"\x13", ["HostFixture", "HostFixture", "", "", "CA", "", ""]]))
                # Joining files the guest under the room (counted by the
                # others) and gives the guest the room's members.
                await guest.send(0xd1, ["24", [room_id, "", "0", "0", "1.0"]])
                _, fields = await guest.receive()
                self.assertEqual(fields, ["38", ["24", [room_id, ""]]])
                _, fields = await guest.receive()
                self.assertEqual(fields[:1] + fields[1][:2], ["53", room_id, "64"])
                self.assertEqual(fields[1][2][12], "8")
                _, fields = await guest.receive()
                self.assertEqual(fields, ["50", ["HostFixture", "0", room_id, "127.0.0.1", "127.0.0.1",
                                                 host_info, "0"]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["50", ["GuestFixture", "0", room_id, "127.0.0.1", "127.0.0.1",
                                                 guest_info, "0"]])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields),
                                 (0x4a, ["GuestFixture", (9).to_bytes(4, "little"), "group",
                                         bytes(4), bytes(4), R6.product]))
                await host.wait.send(0x4c, ["guestfixture"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4c", "guestfixture"]))
                await host.wait.send(0x4c, ["guestfixture"])
                header, fields = await host.wait.receive()
                self.assertEqual((header.message_type, fields), (0x27, [b"\x4c", [bytes(4)]]))
                # In-room player data (0x00404300 -> 0x004013a0) is accepted;
                # R6 has no GAME_STARTED.
                await guest.send(0xd1, ["42", [guest_info]])
                _, fields = await guest.receive()
                self.assertEqual(fields, ["38", ["42"]])
                await guest.send(0x3a)
                header, _ = await guest.receive()
                self.assertEqual(header.message_type, 0x3a)
                # The owner's operation 17 starts a match: every member gets
                # operation 62 with a match ID, which enables results (30).
                await host.send(0xd1, ["17", [room_id, "2"]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["17", [room_id]]])
                match_ids = set()
                for member in (host, guest):
                    _, fields = await member.receive()
                    self.assertEqual(fields[0], "62")
                    self.assertEqual(fields[1][0], room_id)
                    self.assertTrue(fields[1][1].isdigit())
                    match_ids.add(fields[1][1])
                self.assertEqual(len(match_ids), 1)
                match_id = match_ids.pop()
                # Results name the match: [match ID, 0, entries], each entry
                # [name, highest field ID, count, mask, values by field ID].
                # Every room member gets the match statistics as operation
                # 71, in ladder-column order.
                values = ["6", "1"] + ["0"] * 15 + ["2", "0", "272"]
                entry = ["HostFixture", "19", "20", "1048575", values]
                # An entry for an account that is not in the room is dropped:
                # it is neither rated nor broadcast.
                outsider = ["OutsiderFixture", "19", "20", "1048575", ["7", "9"] + ["0"] * 18]
                await host.send(0xd1, ["30", [match_id, "0", entry, outsider]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["30", [match_id]]])
                for member in (host, guest):
                    _, fields = await member.receive()
                    # Ladder order: LEVEL change, RATING, FRAGSCORE, KILLS,
                    # DEATHS, RATIO, TEAMKILLS, ROUND, TEAMROUND, TEAMWIN,
                    # TEAMRATIO, TIMEPLAYED (state 2 is Team Survival).
                    self.assertEqual(fields, ["71", [match_id, "38", [["HostFixture", [
                        "0", "0", "1", "1", "2", "0", "0", "0", "1", "0", "0", "272"]]]]])
                # Only the owner reports results, and only for the room's
                # active match: a guest's report gets no reply and is not
                # broadcast; the owner's report for another match ID is
                # acknowledged but neither kept nor broadcast.
                forged = ["GuestFixture", "19", "20", "1048575", ["6", "99"] + ["0"] * 18]
                await guest.send(0xd1, ["30", [match_id, "0", forged]])
                await guest.send(0x3a)
                header, _ = await guest.receive()
                self.assertEqual(header.message_type, 0x3a)
                stale = str(int(match_id) + 100)
                await host.send(0xd1, ["30", [stale, "0", forged]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["30", [stale]]])
                # The owner's end-of-round report (45) has a completion.
                await host.send(0xd1, ["45", [room_id]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["45", [room_id]]])
                # It also finishes the match: the ladder is updated and the
                # members get the final statistics (one rated player: no
                # rating or level change).
                for member in (host, guest):
                    _, fields = await member.receive()
                    self.assertEqual(fields, ["71", [match_id, "38", [["HostFixture", [
                        "0", "0", "1", "1", "2", "0", "0", "0", "1", "0", "0", "272"]]]]])
                await guest.send(0xd1, ["70", [room_id]])
                # Leaving the room: removed from the room, back in the parent.
                await guest.send(0xd1, ["8", [room_id]])
                _, fields = await guest.receive()
                self.assertEqual(fields, ["38", ["8", [room_id]]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["51", ["GuestFixture", room_id]])
                _, fields = await host.receive()
                self.assertEqual(fields[:1] + fields[1][:3], ["50", "GuestFixture", "0", "1"])
                # The leave callback clears the guest's game and member lists
                # before it asks for the group again (operation 9), so the
                # owner is filed under the room again with the listing.
                await guest.send(0xd1, ["9", ["1", "448"]])
                _, fields = await guest.receive()
                self.assertEqual(fields[0], "53")
                self.assertEqual([child[2] for child in fields[1][3]], [room_id])
                _, fields = await guest.receive()
                self.assertEqual(fields, ["50", ["HostFixture", "0", room_id, "127.0.0.1", "127.0.0.1",
                                                 host_info, "0"]])
                # A guest disconnect is a departure from the parent.
                guest.writer.close()
                await guest.writer.wait_closed()
                _, fields = await host.receive()
                self.assertEqual(fields, ["51", ["GuestFixture", "1"]])
                # Leaving the room returns the owner to the parent group.
                await host.send(0xd1, ["8", [room_id]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["8", [room_id]]])
                await host.send(0x3a)
                header, _ = await host.receive()
                self.assertEqual(header.message_type, 0x3a)

                # A member already in the parent sees a new game listed
                # before its owner is filed under it, and its removal.
                watcher, children = await lobby_member("WatcherFixture")
                self.assertEqual(children, [])
                _, fields = await watcher.receive()
                self.assertEqual(fields[:1] + fields[1][:3], ["50", "HostFixture", "0", "1"])
                await watcher.send(0xd1, ["42", [guest_info]])
                _, fields = await watcher.receive()
                self.assertEqual(fields, ["38", ["42"]])
                _, fields = await host.receive()
                self.assertEqual(fields[:1] + fields[1][:3], ["50", "WatcherFixture", "0", "1"])
                await host.send(0xd1, ["12", r6_create(players="6")])
                _, fields = await host.receive()
                second = fields[1][1][0]
                self.assertNotEqual(second, room_id)
                _, fields = await watcher.receive()
                self.assertEqual(fields[:1] + fields[1][:2], ["53", "1", "256"])
                self.assertEqual([(child[0], child[2], child[12]) for child in fields[1][3]],
                                 [("5", second, "6")])
                _, fields = await watcher.receive()
                self.assertEqual(fields[:1] + fields[1][:3], ["50", "HostFixture", "0", second])
                await host.send(0xd1, ["24", [second, "", "0", "0", "1.0"]])
                await host.receive()
                await host.receive()
                await host.send(0xd1, ["8", [second]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["8", [second]]])
                _, fields = await watcher.receive()
                self.assertEqual(fields, ["51", ["HostFixture", second]])
                _, fields = await watcher.receive()
                self.assertEqual(fields[:1] + fields[1][:3], ["50", "HostFixture", "0", "1"])
                _, fields = await watcher.receive()
                self.assertEqual(fields, ["55", [second]])

                # The owner may leave a room it created before its self-join
                # (operation 24): the game is removed and it can create again.
                await host.send(0xd1, ["12", r6_create()])
                _, fields = await host.receive()
                third = fields[1][1][0]
                _, fields = await watcher.receive()
                self.assertEqual([child[2] for child in fields[1][3]], [third])
                _, fields = await watcher.receive()
                self.assertEqual(fields[:1] + fields[1][:3], ["50", "HostFixture", "0", third])
                await host.send(0xd1, ["8", [third]])
                _, fields = await host.receive()
                self.assertEqual(fields, ["38", ["8", [third]]])
                _, fields = await watcher.receive()
                self.assertEqual(fields, ["51", ["HostFixture", third]])
                _, fields = await watcher.receive()
                self.assertEqual(fields[:1] + fields[1][:3], ["50", "HostFixture", "0", "1"])
                _, fields = await watcher.receive()
                self.assertEqual(fields, ["55", [third]])

                # An owner disconnect is a departure, then the game's removal.
                await host.send(0xd1, ["12", r6_create()])
                _, fields = await host.receive()
                fourth = fields[1][1][0]
                self.assertNotEqual(fourth, third)
                await watcher.receive()
                await watcher.receive()
                host.writer.close()
                await host.writer.wait_closed()
                _, fields = await watcher.receive()
                self.assertEqual(fields, ["51", ["HostFixture", fourth]])
                _, fields = await watcher.receive()
                self.assertEqual(fields, ["55", [fourth]])
            finally:
                for client in clients:
                    client.writer.close()
                    await client.writer.wait_closed()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
