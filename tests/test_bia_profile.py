"""Brothers in Arms profile: static wire shapes and a real-socket sign-in path."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from ubisoft import router, service
from ubisoft.games.base import UnsupportedOperation
from ubisoft import accounts
from ubisoft.games import bia, bia_ladder
from ubisoft.games.registry import BIA, GR2, R6, XIII, bindings
from ubisoft.rooms import LobbyMember, RoomRegistry

from test_games import GSClient
from test_r6_profile import reserve_ports


SESSION = bytes(range(0x50))


def bia_create(name="Fixture match", password="", players="4", info=SESSION, product=None):
    # Sender 0x004f7b90 via CreateRoom 0x0051e960: parent, name, product,
    # group type 5, player limit, 0 spectators, 0x50 bytes of session info,
    # password, versions and additional data.
    return ["1", name, product or BIA.product, "5", players, "0", info, password,
            BIA.sdk_version, BIA.game_version, b""]


def bia_query(app_id, *, product=BIA.product, selection="3", columns=("GLOBAL_RANK", "ALIAS", "SCORE")):
    # Thunks 0x0050b630-0x0050b670: product, a selection, a sort column and
    # the column table. Type 1 is [alias, count]; type 3 is [start, count].
    values = ["SharedFixture", "1"] if selection == "1" else ["0", "10"]
    return ["1", [str(app_id), product, "0", "0", [selection, values], [list(columns), ["GLOBAL_RANK"], []]]]


class BIAProfileTests(unittest.TestCase):
    def test_identity_and_sectioned_bootstrap(self):
        self.assertEqual((BIA.slug, BIA.product, BIA.sdk_version, BIA.game_version),
                         ("bia", "BROTHERSINARMSPS2US", "BIAPS2US1.0", "1.0"))
        body = BIA.bootstrap("192.0.2.1", 30030, 30031, "192.0.2.1", 30032)
        self.assertEqual(body, b"[Servers]\r\nRouterIP0=192.0.2.1\r\nRouterPort0=30030\r\n"
                         b"RouterLauncherPort0=30031\r\nNATServerIP0=192.0.2.1\r\nNATServerPort0=30032\r\n")

    def test_capabilities(self):
        self.assertTrue(BIA.supports_discovery and BIA.lobby_members and BIA.supports_rooms and BIA.lobby_create)
        self.assertTrue(BIA.creator_room_listing)
        self.assertFalse(BIA.profile_country_update or BIA.game_started or BIA.owner_game_started)
        # JoinFriend's location callback files groups by sign.
        self.assertTrue(BIA.negative_room_ids and BIA.friend_joining)
        self.assertFalse(R6.friend_joining or XIII.friend_joining)
        self.assertFalse(R6.creator_room_listing)
        self.assertEqual((BIA.nat_address_byteorder, R6.nat_address_byteorder, XIII.nat_address_byteorder),
                         ("big", "little", "little"))
        self.assertIsNone(R6.room_join_failure(None, "password"))
        self.assertTrue(R6.profile_country_update)
        self.assertEqual(BIA.proxy_services, ("ladderquery",))

    def test_match_results_are_validated_and_acknowledged(self):
        # Retail run 8's shape (names by length): [match, 0, entries].
        entries = [["Winner", "7", "8", "255", ["1", "0", "5", "35", "2", "600", "1", "0"]],
                   ["Loser", "7", "8", "255", ["-1", "1", "5", "27", "3", "600", "0", "0"]]]
        self.assertIsNone(BIA.match_results_event("1", entries))
        self.assertEqual(bia.summarize(entries)[1], ("Loser", [-1, 1, 5, 27, 3, 600, 0, 0]))
        for bad in ([["Winner", "7", "8", "255", ["2", "0", "5", "35", "2", "600", "1", "0"]]],
                    [["Winner", "7", "8", "255", ["1", "-3", "5", "35", "2", "600", "1", "0"]]],
                    [["Winner", "7", "8", "255", ["1", "0", "5"]]],
                    [["Winner", "x", "8", "255", ["1", "0", "5", "35", "2", "600", "1", "0"]]],
                    entries + [["winner", "7", "8", "255", ["1", "0", "0", "0", "0", "0", "0", "0"]]]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                BIA.match_results_event("1", bad)
        room = RoomRegistry().register("shared", "192.0.2.2", bia_create(), BIA)
        self.assertEqual(BIA.room_notice_reply(room, 45), ["38", ["45", [str(room.room_id), "1"]]])
        self.assertIsNone(BIA.room_notice_reply(room, 44))

    def test_room_creation_needs_80_bytes_of_session_info(self):
        room = RoomRegistry().register("shared", "192.0.2.2", bia_create(password="pw"), BIA)
        self.assertLess(room.room_id, 0)
        self.assertEqual((room.group_type, room.max_players, room.info, room.password),
                         ("5", "4", SESSION, "pw"))
        self.assertNotIn("pw", repr(room))
        fields = room.group_fields()
        self.assertEqual((len(fields), fields[10], fields[12]), (20, SESSION, "4"))
        self.assertEqual(BIA.lobby_create_result(room), [str(room.room_id), "", "1"])
        # A host-order word stored little-endian: 198.51.100.7 is 07 64 33 c6.
        info = bytes(4) + bytes([7, 100, 51, 198]) + (7777).to_bytes(4, "little") + bytes(68)
        room.info = info
        self.assertEqual(BIA.host_endpoint(room, "198.51.100.7"), "peer:7777")
        self.assertEqual(R6.host_endpoint(room, "198.51.100.7"), "unknown")
        self.assertEqual(BIA.room_join_result(room), [str(room.room_id), ""])
        self.assertEqual(BIA.room_join_failure(room, "password"), ["11", str(room.room_id), ""])
        self.assertEqual(BIA.registration(bia_create(players="2"), "192.0.2.2").max_players, "2")
        for fields in (bia_create(info=b""), bia_create(info=bytes(81)), bia_create(players="5"),
                       bia_create(players="1"), bia_create(name=""), bia_create(password="x" * 33),
                       bia_create(product=R6.product), bia_create()[:10], ["2"] + bia_create()[1:]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                BIA.registration(fields, "192.0.2.2")

    def test_owner_room_updates_and_reports(self):
        room = RoomRegistry().register("shared", "192.0.2.2", bia_create(), BIA)
        rid = str(room.room_id)
        self.assertEqual(BIA.room_update(room, 15, [rid]), [rid])
        self.assertEqual(BIA.room_update(room, 17, [rid, "1"]), [rid])
        self.assertEqual(BIA.room_update(room, 33, [rid, b"", "7777", "198.51.100.7"]), [rid])
        self.assertEqual((room.game_port, room.local_ip), ("7777", "198.51.100.7"))
        # Operation 31: a mask names the fields that follow. Every field
        # (retail run 8), then Play Next Game's session info alone (run 10).
        self.assertEqual(BIA.room_update(room, 31, [rid, str(0x67e), "16", "2", "0", "Next",
                                                    bytes(range(80)), b""]), [rid])
        self.assertEqual((room.info, room.max_players), (bytes(range(80)), "2"))
        self.assertEqual(BIA.room_update(room, 31, [rid, "64", bytes(range(1, 81))]), [rid])
        self.assertEqual(room.info, bytes(range(1, 81)))
        BIA.room_update(room, 31, [rid, "64", b"short"])
        BIA.room_update(room, 31, [rid, "8", "9"])
        self.assertEqual((room.info, room.max_players), (bytes(range(1, 81)), "2"))
        for values in ([rid], [rid, "1"], [rid, "64"], [rid, "64", b"", b""], [rid, "8", b""],
                       [rid, "32", "\x01"], [rid, "2"], [rid, "x", b""]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BIA.room_update(room, 31, values)
        for subtype, values in ((15, ["999"]), (17, [rid]), (31, [rid]), (30, [rid]), (33, [rid, b"", "x", ""])):
            with self.subTest(subtype=subtype), self.assertRaises(ValueError):
                BIA.room_update(room, subtype, values)
        self.assertEqual(BIA.room_notice_subtypes, (34, 35, 44, 45, 70))
        self.assertIsNone(BIA.room_notice(room, 34, [rid]))
        with self.assertRaises(ValueError):
            BIA.room_notice(room, 34, ["999"])
        self.assertEqual(BIA.match_started_event(room, 7), ["62", [rid, "7"]])

    def test_discovery_uses_its_own_product_and_fourteen_field_folder(self):
        BIA.discovery_login([BIA.product])
        for fields in ([], [R6.product], [XIII.product], [BIA.product, "extra"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                BIA.discovery_login(fields)
        folder = BIA.parent_lobby(3)
        self.assertEqual(len(folder), 14)
        self.assertEqual((folder[1], folder[9], folder[10], folder[12], folder[13]),
                         ("Brothers in Arms", BIA.product, b"", "100", "3"))
        self.assertEqual(R6.parent_lobby(3)[12], "8")
        self.assertLess(len(folder[1].encode()), 33)

    def test_motd_languages_and_overrides(self):
        for language in ("en", "fr", "de", "it", "es"):
            self.assertEqual(BIA.login_notices([language]), list(BIA.news))
        for fields in ([], ["ge"], ["en", "extra"], [b"en"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                BIA.login_notices(fields)
        self.assertTrue(all(0 < len(item) <= 512 and item.isascii() for item in BIA.news))
        (binding,) = bindings({"TUBISOFT_GAMES": "bia", "TUBISOFT_BIA_NEWS_2": "Maintenance tonight."})
        self.assertEqual(binding.profile.login_notices(["en"]), [BIA.news[0], "Maintenance tonight."])

    def test_parent_join_and_its_12_byte_player_record(self):
        self.assertEqual(BIA.join_parent_lobby(["1", "", "0"]), ["1", ""])
        for fields in ([], ["2", "", "0"], ["1", "secret", "0"], ["1", "", b"0"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                BIA.join_parent_lobby(fields)
        record = bytes(range(12))
        self.assertEqual(BIA.parent_player_info([record]), record)
        for fields in ([bytes(24)], [bytes(28)], ["x" * 12], []):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                BIA.parent_player_info(fields)

    def test_join_flags(self):
        self.assertTrue(BIA.self_member_event)
        self.assertEqual(BIA.group_refresh(["1", "64"]), (False, False))
        self.assertFalse(R6.self_member_event)
        self.assertTrue(R6.refresh_resends_members)

    def test_player_data_endpoint_is_logged_without_the_address(self):
        port = (7777).to_bytes(4, "little")
        self.assertEqual(router.endpoint_shape(bytes([198, 51, 100, 7]) + port + bytes(4), "198.51.100.7"),
                         "peer:7777")
        self.assertEqual(router.endpoint_shape(bytes([192, 0, 2, 100]) + port, "198.51.100.7"), "emulated:7777")
        self.assertEqual(router.endpoint_shape(bytes([172, 17, 0, 1]) + port, "198.51.100.7"), "lan-172:7777")
        self.assertEqual(router.endpoint_shape(bytes([192, 168, 0, 1]) + port, "198.51.100.7"), "lan-192:7777")
        self.assertEqual(router.endpoint_shape(bytes([203, 0, 113, 9]) + port, "peer-x"), "other:7777")
        self.assertEqual(router.endpoint_shape(b"", "198.51.100.7"), "none")
        self.assertEqual(router.endpoint_shape(bytes([7, 100, 51, 198]) + port, "198.51.100.7", "big"),
                         "peer:7777")
        self.assertEqual(router.endpoint_shape(bytes([198, 51, 100, 7]) + port, "198.51.100.7", "big"),
                         "other:7777")

    def test_room_data_change_announces_the_room(self):
        room = RoomRegistry().register("shared", "192.0.2.2", bia_create(), BIA)
        rid = str(room.room_id)
        BIA.room_update(room, 31, [rid, "64", bytes(range(1, 81))])
        event = BIA.room_update_event(room, 31)
        self.assertEqual(event[:1] + event[1][:2], ["53", rid, "64"])
        self.assertEqual(event[1][2], room.group_fields())
        for subtype in (15, 17, 33):
            self.assertIsNone(BIA.room_update_event(room, subtype))
        self.assertIsNone(R6.room_update_event(room, 31))

    def test_friend_presence_allows_joining(self):
        # GS 8 maps to BiA 5 (lobby joinable), 9 to 7 (game joinable).
        self.assertEqual(BIA.friend_presence(False), (8, BIA.product))
        self.assertEqual(BIA.friend_presence(True), (9, BIA.product))
        self.assertEqual(R6.friend_presence(False), (0, ""))
        self.assertEqual(R6.friend_presence(True), (9, R6.product))

    def test_group_refresh_and_friend_locations(self):
        # Tick's timed refresh gets the lobby record only; JoinLobbyReply's
        # refresh (0x1c0) gets the rooms followed by the members.
        self.assertEqual(BIA.group_refresh(["1", "64"]), (False, False))
        self.assertEqual(BIA.group_refresh(["1", "448"]), (True, True))
        self.assertEqual(R6.group_refresh(["1", "64"]), (True, True))
        self.assertEqual(XIII.group_refresh(["1"]), (True, XIII.refresh_resends_members))
        in_lobby = LobbyMember("Fixture", BIA.product, "192.0.2.1", "", print)
        in_room = LobbyMember("Fixture", BIA.product, "192.0.2.1", "", print, group="-1000")
        self.assertEqual(BIA.friend_location(in_lobby), [["1", "1"]])
        self.assertEqual(BIA.friend_location(in_room), [["1", "1"], ["1", "-1000"]])
        with self.assertRaises(UnsupportedOperation):
            R6.friend_location(in_lobby)

    def test_member_events_match_the_readers(self):
        member = LobbyMember("Fixture", BIA.product, "192.0.2.5", "", None)
        member.info = bytes(12)
        self.assertEqual(BIA.lobby_member_event(member),
                         ["50", ["Fixture", "0", "1", "192.0.2.5", "192.0.2.5", bytes(12), "0"]])
        self.assertEqual(BIA.lobby_member_left(member), ["51", ["Fixture", "1"]])

    def test_ranking_queries_get_the_empty_table(self):
        self.assertEqual(BIA.ranking_table(bia_query(5), "x"), ["1", ["5", "0", [], []]])
        around = bia_query(6, selection="1", columns=("GLOBAL_RANK", "ALIAS", "SCORE", "ACCURACY"))
        self.assertEqual(BIA.ranking_table(around, "x"), ["1", ["6", "0", [], []]])
        for fields in (bia_query(1, product=R6.product), bia_query(1, selection="4"),
                       ["2", bia_query(1)[1]], ["1", bia_query(1)[1][:5]], ["1", "x"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                BIA.ranking_table(fields, "x")

    def test_configuration_uses_the_next_port_block(self):
        env = {"TUBISOFT_ADVERTISE_IP": "192.0.2.1", "TUBISOFT_GAMES": "xiii,gr2,r6,bia"}
        games = service.configuration(env).games
        self.assertEqual([g.profile for g in games], [XIII, GR2, R6, BIA])
        self.assertEqual((games[3].router_port, games[3].launcher_port, games[3].nat_port),
                         (30030, 30031, 30032))
        with self.assertRaises(ValueError):
            service.configuration(env | {"TUBISOFT_BIA_NAT_PORT": "30022"})

    def test_compose_publishes_the_ports_without_an_overlay(self):
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
        for name, port in (("ROUTER", 30030), ("LAUNCHER", 30031), ("NAT", 30032)):
            self.assertIn(f"TUBISOFT_BIA_{name}_PORT: ${{TUBISOFT_BIA_{name}_PORT:-{port}}}", compose)
        self.assertIn('"${TUBISOFT_BIA_NAT_PORT:-30032}:${TUBISOFT_BIA_NAT_PORT:-30032}/udp"', compose)
        self.assertIn("TUBISOFT_BIA_NEWS_1: ${TUBISOFT_BIA_NEWS_1:-}", compose)


class BIALadderTests(unittest.TestCase):
    def test_increments_follow_type_team_and_status(self):
        friendly_win = bia_ladder.increments([1, 2, 1, 40, 9, 300, 1, 0])
        self.assertEqual((friendly_win["score"], friendly_win["friend_rnds"], friendly_win["friend_wins"],
                          friendly_win["ranked_rnds"], friendly_win["ally_matches"], friendly_win["axis_matches"]),
                         (0, 1, 1, 0, 0, 1))
        ladder_win = bia_ladder.increments([1, 0, 0, 0, 0, 60, 0, 1])
        ladder_loss = bia_ladder.increments([-1, 0, 0, 0, 0, 60, 0, 1])
        self.assertEqual((ladder_win["score"], ladder_win["ranked_wins"], ladder_win["ally_matches"]), (3, 1, 1))
        self.assertEqual((ladder_loss["score"], ladder_loss["ranked_rnds"], ladder_loss["ranked_wins"]), (1, 1, 0))
        totals = dict.fromkeys(bia_ladder.COUNTERS, 0) | {"fired": 40, "hits": 9}
        self.assertEqual(dict(zip(bia_ladder.VALUES, bia_ladder.final_values(totals)))["ACCURACY"], 22)
        self.assertEqual(len(bia_ladder.final_values(dict.fromkeys(bia_ladder.COUNTERS, 0))), 13)

    def test_matches_accumulate_and_rank_by_score(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.sqlite3"
            accounts.initialize_database(path)
            for name in ("Able", "Baker"):
                self.assertTrue(accounts.create_account(path, name, "fixture-only"))
            ladder = bia_ladder.BIALadder(path)
            entries = [["Able", "7", "8", "255", ["1", "3", "1", "20", "10", "300", "1", "1"]],
                       ["Baker", "7", "8", "255", ["-1", "1", "3", "10", "2", "300", "0", "1"]],
                       ["NoAccount", "7", "8", "255", ["0", "0", "0", "0", "0", "300", "0", "1"]]]
            self.assertIsNone(ladder.provisional(entries, None))
            first = ladder.record(entries, None)
            self.assertEqual(set(first), {"able", "baker"})
            second = ladder.record(entries, None)
            able = dict(zip(bia_ladder.VALUES, second["able"]))
            self.assertEqual((able["SCORE"], able["KILLS"], able["RANKED_RNDS"], able["RANKED_WINS"],
                              able["ACCURACY"], able["PLAY_TIME"]), (6, 6, 2, 2, 50, 600))
            self.assertEqual([row[:2] for row in ladder.board()], [(1, "Able"), (2, "Baker")])
            # Top list (type 3) and around-me (type 1) answer with every
            # column, the requested ones first; an unranked player keeps the
            # empty table.
            top = bia_ladder.ranking_table(BIA, bia_query(5, columns=("GLOBAL_RANK", "ALIAS", "SCORE")), "x", path)
            self.assertEqual(top[1][:2], ["5", "2"])
            self.assertEqual(top[1][2][:3], [["GLOBAL_RANK", "GLOBAL_RANK"], ["ALIAS", "ALIAS"], ["SCORE", "SCORE"]])
            self.assertEqual([row[:3] for row in top[1][3]], [["1", "Able", "6"], ["2", "Baker", "2"]])
            self.assertTrue(all(cell.lstrip("-").isdecimal() for row in top[1][3] for cell in row[:1] + row[2:]))
            around = ["1", [part if i != 4 else ["1", ["able", "1"]] for i, part in enumerate(bia_query(6)[1])]]
            mine = bia_ladder.ranking_table(BIA, around, "x", path)
            self.assertEqual([row[:3] for row in mine[1][3]], [["1", "Able", "6"]])
            unranked = ["1", [query if i != 4 else ["1", ["Nobody", "1"]] for i, query in enumerate(bia_query(7)[1])]]
            self.assertEqual(bia_ladder.ranking_table(BIA, unranked, "x", path), ["1", ["7", "0", [], []]])


class BIASignInTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_sign_in_sequence_reaches_the_parent_lobby(self):
        with TemporaryDirectory() as directory:
            reservations, ports = reserve_ports(7)
            try:
                env = {"TUBISOFT_ADVERTISE_IP": "127.0.0.1", "TUBISOFT_BIND_IP": "127.0.0.1",
                       "TUBISOFT_STATE_DIR": directory, "TUBISOFT_GAMES": "xiii,bia",
                       "TUBISOFT_HTTP_PORT": str(ports[6])}
                for prefix, selected in (("TUBISOFT_", ports[:3]), ("TUBISOFT_BIA_", ports[3:6])):
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
                        # 0x00530410 formats "GET /gsinit.php?dp=%s&user=%s".
                        writer.write(b"GET /gsinit.php?dp=BROTHERSINARMSPS2US&user=private-fixture HTTP/1.1\r\n"
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

                # Account creation (0x004f0dc0) sends seven strings: the SDK
                # version, the username, the password and four empty strings.
                creator = await connect(ports[3])
                await creator.send(1, [BIA.sdk_version, "SharedFixture", "fixture-only", "", "", "", ""], encrypted=True)
                header, fields = await creator.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x01"]))
                bia = await connect(ports[3])
                # 0x004f07b0: username, password, SDK version, one-byte binary.
                await bia.send(0x66, ["SharedFixture", "fixture-only", BIA.sdk_version, b"\1"], encrypted=True)
                header, fields = await bia.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x66"]))
                await bia.send(0x5d)
                _, fields = await bia.receive()
                self.assertEqual(fields, [b"\x5d", ["127.0.0.1", ports[3].to_bytes(4, "little")]])
                wait = await connect(ports[3])
                await wait.send(0x4d, ["SharedFixture"])
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4d"]))
                await wait.send(0xd1, ["21", [BIA.product]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["21"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["53", ["1", "256", ["0"], [BIA.parent_lobby()]]])
                # Friends login 0x004f2780 sends two 4-byte binaries.
                await wait.send(0x4e, [bytes(4), bytes(4)])
                _, fields = await wait.receive()
                self.assertEqual(fields, [b"\x4e"])
                await wait.send(0x54)
                _, fields = await wait.receive()
                self.assertEqual(fields, [b"\x54"])
                await wait.send(0x13, ["SharedFixture"])
                _, fields = await wait.receive()
                self.assertEqual(fields[0], b"\x13")
                self.assertEqual(len(fields[1]), 7)
                await wait.send(0xde, ["de"])
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\xde", list(BIA.news)]))
                # A password change (0x004f0c40) is declined, not stored, and
                # the wait connection stays open.
                await wait.send(0x62, ["", "new-fixture", "SharedFixture", "", "", ""], encrypted=True)
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x27, [b"\x62"]))
                # Ladder initialization: lookup, select and proxy login.
                await wait.send(0xcc, ["1", ["ladderquery", "0", "0"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["1", ["ladderquery", "0", "0",
                                                      [["1", "127.0.0.1", str(ports[3])]]]]])
                await wait.send(0xcc, ["2", ["1"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["2", ["1"]]])
                login = await connect(ports[3], (8, 11))
                await login.send(0x66, ["SharedFixture", BIA.sdk_version])
                header, fields = await login.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x66", []]))
                await login.send(0x5d)
                _, fields = await login.receive()
                ranking = await connect(ports[3], (8, 11))
                await ranking.send(0x4d, [fields[1][0]])
                _, fields = await ranking.receive()
                self.assertEqual(fields, [b"\x4d", []])
                await ranking.send(0xcc, ["1281", "7", bia_query(41)])
                header, fields = await ranking.receive()
                self.assertEqual((header.message_type, fields), (0xcc, ["1281", "7", ["38", ["1", ["41", "0", [], []]]]]))
                # Lobby address (0x005075e0, operation 3) and the five-field
                # lobby login (0x00507230).
                await wait.send(0xd1, ["3", ["1"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["3", ["1", "127.0.0.1", str(ports[3])]]])
                lobby = await connect(ports[3])
                await lobby.send(0xd2, ["SharedFixture", "1", "192.0.2.5", "0.0.0.0", "0"])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["210", ["1"]])
                await lobby.send(0xd1, ["23", ["1", "", "256"]])
                header, fields = await lobby.receive()
                self.assertEqual((header.message_type, fields), (0xd1, ["38", ["23", ["1", ""]]]))
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["53", ["1", "256", BIA.parent_lobby(1), []]])
                # LobbyRcv_MemberJoined (0x0052bf00) sends operation 42 only
                # when it sees the player's own name (retail run 1).
                header, fields = await lobby.receive()
                self.assertEqual((header.message_type, fields),
                                 (0xd1, ["50", ["SharedFixture", "0", "1", "127.0.0.1", "127.0.0.1", b"", "0"]]))
                await lobby.send(0xd1, ["42", [bytes(range(12))]])
                header, fields = await lobby.receive()
                self.assertEqual((header.message_type, fields), (0xd1, ["38", ["42"]]))
                await lobby.send(0xd1, ["9", ["1"]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["53", ["1", "256", BIA.parent_lobby(1), []]])
                # Session info other than 0x50 bytes is refused without
                # inventing a room.
                await lobby.send(0xd1, ["12", ["1", "Fixture", BIA.product, "5", "4", "0", b"",
                                               "", "1.0", BIA.sdk_version, b""]])
                self.assertEqual(await asyncio.wait_for(lobby.reader.read(), 2), b"")
            finally:
                for client in clients:
                    client.writer.close()
                    await client.writer.wait_closed()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class BIALobbyMemberTests(unittest.IsolatedAsyncioTestCase):
    async def test_players_see_themselves_and_each_other_once(self):
        with TemporaryDirectory() as directory:
            reservations, ports = reserve_ports(7)
            try:
                env = {"TUBISOFT_ADVERTISE_IP": "127.0.0.1", "TUBISOFT_BIND_IP": "127.0.0.1",
                       "TUBISOFT_STATE_DIR": directory, "TUBISOFT_GAMES": "xiii,bia",
                       "TUBISOFT_HTTP_PORT": str(ports[6])}
                for prefix, selected in (("TUBISOFT_", ports[:3]), ("TUBISOFT_BIA_", ports[3:6])):
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
                    creator = await connect(ports[3])
                    await creator.send(1, [BIA.sdk_version, user, "fixture-only", "", "", "", ""], encrypted=True)
                    header, _ = await creator.receive()
                    self.assertEqual(header.message_type, 0x26)
                    bia = await connect(ports[3])
                    await bia.send(0x66, [user, "fixture-only", BIA.sdk_version, b"\1"], encrypted=True)
                    header, _ = await bia.receive()
                    self.assertEqual(header.message_type, 0x26)
                    await bia.send(0x5d)
                    await bia.receive()
                    wait = await connect(ports[3])
                    await wait.send(0x4d, [user])
                    header, _ = await wait.receive()
                    self.assertEqual(header.message_type, 0x26)
                    await wait.send(0xd1, ["3", ["1"]])
                    await wait.receive()
                    lobby = await connect(ports[3])
                    await lobby.send(0xd2, [user, "1", "192.0.2.5", "0.0.0.0", "0"])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields, ["210", ["1"]])
                    await lobby.send(0xd1, ["23", ["1", "", "256"]])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields, ["38", ["23", ["1", ""]]])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields[0], "53")
                    return lobby, wait

                first_info, second_info = bytes(range(12)), bytes(range(12, 24))
                first, first_wait = await lobby_member("FirstFixture")
                _, fields = await first.receive()
                self.assertEqual(fields[1][0], "FirstFixture")
                await first.send(0xd1, ["42", [first_info]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["42"]])
                # The second player learns of the first, then of itself.
                second, second_wait = await lobby_member("SecondFixture")
                _, fields = await second.receive()
                self.assertEqual(fields, ["50", ["FirstFixture", "0", "1", "127.0.0.1", "127.0.0.1",
                                                 first_info, "0"]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["50", ["SecondFixture", "0", "1", "127.0.0.1", "127.0.0.1",
                                                 b"", "0"]])
                await second.send(0xd1, ["42", [second_info]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["38", ["42"]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["50", ["SecondFixture", "0", "1", "127.0.0.1", "127.0.0.1",
                                                 second_info, "0"]])
                # Tick's timed refresh (operation 9, flags 0x40) gets the
                # group record only; members are not announced again.
                for _ in range(3):
                    await first.send(0xd1, ["9", ["1", "64"]])
                    _, fields = await first.receive()
                    self.assertEqual(fields[0], "53")
                    self.assertEqual(fields[1][2][13], "2")
                await first.send(0x3a)
                header, _ = await first.receive()
                self.assertEqual(header.message_type, 0x3a)
                # The first player creates a protected match. The creator
                # also gets the listing (CreateRoomReply waits for it).
                await first.send(0xd1, ["12", bia_create(password="pw-fixture")])
                _, fields = await first.receive()
                self.assertEqual(fields[:1] + fields[1][:1], ["38", "12"])
                room_id, _, parent = fields[1][1]
                self.assertEqual(parent, "1")
                for client in (first, second):
                    _, fields = await client.receive()
                    self.assertEqual(fields[0], "53")
                    self.assertEqual([(child[2], child[10]) for child in fields[1][3]], [(room_id, SESSION)])
                _, fields = await second.receive()
                self.assertEqual(fields, ["50", ["FirstFixture", "0", room_id, "127.0.0.1", "127.0.0.1",
                                                 first_info, "0"]])
                # 0x0050b490 joins with the room's password.
                await first.send(0xd1, ["24", [room_id, "pw-fixture", "0", "128", BIA.sdk_version]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["24", [room_id, ""]]])
                _, fields = await first.receive()
                self.assertEqual(fields[:1] + fields[1][:2], ["53", room_id, "64"])
                # Retail run 9: listing the rooms again resets their player
                # counts, so the timed refresh (0x40) leaves them out, and
                # JoinLobbyReply's refresh (0x1c0) re-sends the members.
                await second.send(0xd1, ["9", ["1", "64"]])
                _, fields = await second.receive()
                self.assertEqual((fields[0], fields[1][3]), ("53", []))
                await second.send(0xd1, ["9", ["1", "448"]])
                _, fields = await second.receive()
                self.assertEqual([child[2] for child in fields[1][3]], [room_id])
                _, fields = await second.receive()
                self.assertEqual(fields, ["50", ["FirstFixture", "0", room_id, "127.0.0.1", "127.0.0.1",
                                                 first_info, "0"]])
                # Joining a friend's game (operation 106 on the wait
                # connection): the friend's lobby, then the room.
                await second_wait.send(0x4e, [bytes(4), bytes(4)])
                header, _ = await second_wait.receive()
                self.assertEqual(header.message_type, 0x26)
                await second_wait.send(0x4b, ["firstfixture", "group", bytes(4)])
                header, _ = await second_wait.receive()
                self.assertEqual(header.message_type, 0x26)
                header, fields = await second_wait.receive()
                self.assertEqual((header.message_type, fields[0], fields[1]),
                                 (0x4a, "FirstFixture", (9).to_bytes(4, "little")))
                await second_wait.send(0xd1, ["106", ["FirstFixture"]])
                _, fields = await second_wait.receive()
                self.assertEqual(fields, ["106", ["FirstFixture", ["1", "1"], ["1", room_id]]])
                # A player who is not on the requester's list is unavailable.
                await first_wait.send(0xd1, ["106", ["SecondFixture"]])
                _, fields = await first_wait.receive()
                self.assertEqual(fields, ["106", ["SecondFixture"]])
                # Play Next Game (retail run 11): operation 31 with new
                # session info, then the room's record (53, flags 0x40) for
                # everyone, the owner included -- RoomInfo completes it.
                next_info = bytes(range(1, 81))
                await first.send(0xd1, ["31", [room_id, "64", next_info]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["31", [room_id]]])
                for client in (first, second):
                    _, fields = await client.receive()
                    self.assertEqual(fields[:1] + fields[1][:2], ["53", room_id, "64"])
                    self.assertEqual((fields[1][2][2], fields[1][2][10]), (room_id, next_info))
                # A wrong password is a GSFAIL reply, not a disconnect.
                await second.send(0xd1, ["24", [room_id, "wrong", "0", "128", BIA.sdk_version]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["39", ["24", ["11", room_id, ""]]])
                await second.send(0xd1, ["24", [room_id, "pw-fixture", "0", "128", BIA.sdk_version]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["38", ["24", [room_id, ""]]])
                _, fields = await second.receive()
                self.assertEqual(fields[:1] + fields[1][:2], ["53", room_id, "64"])
                _, fields = await second.receive()
                self.assertEqual(fields[1][:3], ["FirstFixture", "0", room_id])
                _, fields = await first.receive()
                self.assertEqual(fields[1][:3], ["SecondFixture", "0", room_id])
                # Starting a match tells everyone in the room (operation 62).
                await first.send(0xd1, ["17", [room_id, "1"]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["17", [room_id]]])
                match_id = None
                for client in (first, second):
                    _, fields = await client.receive()
                    self.assertEqual(fields[0], "62")
                    self.assertEqual(fields[1][0], room_id)
                    match_id = fields[1][1]
                # Retail run 8: both players report 44; the owner then sends
                # results (30) and the round end (45). Neither closes the
                # connection, and no final statistics are pushed yet.
                await second.send(0xd1, ["44", [room_id]])
                await first.send(0xd1, ["30", [match_id, "0",
                                               ["FirstFixture", "7", "8", "255", ["1", "2", "1", "40", "9", "300", "1", "0"]],
                                               ["SecondFixture", "7", "8", "255", ["-1", "1", "2", "30", "5", "300", "0", "0"]]]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["30", [match_id]]])
                await first.send(0xd1, ["45", [room_id]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["45", [room_id, "1"]]])
                # The round end records the match: both players get their
                # thirteen totals (operation 71). A Friendly win adds no SCORE.
                for client in (first, second):
                    _, fields = await client.receive()
                    self.assertEqual(fields[:1] + fields[1][:2], ["71", match_id, "38"])
                    self.assertEqual(fields[1][2], [
                        ["FirstFixture", ["0", "2", "1", "40", "9", "22", "0", "0", "1", "1", "0", "1", "300"]],
                        ["SecondFixture", ["0", "1", "2", "30", "5", "16", "0", "0", "1", "0", "1", "0", "300"]]])
                await second.send(0x3a)
                header, _ = await second.receive()
                self.assertEqual(header.message_type, 0x3a)
                # Leaving the room returns the guest to the lobby group.
                await second.send(0xd1, ["8", [room_id]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["38", ["8", [room_id]]])
                # Operation 51 removes the guest from the room; operation 50
                # files it under the lobby again.
                _, fields = await first.receive()
                self.assertEqual(fields, ["51", ["SecondFixture", room_id]])
                _, fields = await first.receive()
                self.assertEqual(fields[1][:3], ["SecondFixture", "0", "1"])
                # Leaving the parent tells the remaining player.
                await second.send(0xd1, ["8", ["1"]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["38", ["8", ["1"]]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["51", ["SecondFixture", "1"]])
            finally:
                for client in clients:
                    client.writer.close()
                    await client.writer.wait_closed()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
