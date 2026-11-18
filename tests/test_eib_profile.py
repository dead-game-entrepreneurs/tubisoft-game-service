"""Earned in Blood profile: identity, skirmish results, ladder boards and sockets."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ubisoft import accounts, crypto, irc, router, service
from ubisoft.games import bia_ladder, eib, eib_ladder
from ubisoft.games.base import UnsupportedOperation
from ubisoft.games.registry import BIA, EIB, GR2, R6, ROD, XIII
from ubisoft.rooms import LobbyMember, RoomRegistry

from test_games import GSClient
from test_r6_profile import reserve_ports


SESSION = bytes(range(0x50))


def eib_create(name="Fixture match", password="", players="4", info=SESSION, product=None):
    # Sender 0x004f7b90's shape (Road to Hill 30): parent, name, product,
    # group type 5, player limit, 0 spectators, 0x50 bytes of session info,
    # password, versions and additional data.
    return ["1", name, product or EIB.product, "5", players, "0", info, password,
            EIB.sdk_version, EIB.game_version, b""]


def eib_query(app_id, *, kind="0", board="0", selection="3", values=None,
              columns=("GLOBAL_RANK", "ALIAS", "SCORE")):
    # Ladder request 0x004fb3d0: (0, 0) versus, (1, 0) skirmish overall,
    # (1, map ID) one mission.
    values = values or (["SharedFixture", "1"] if selection == "1" else ["0", "10"])
    return ["1", [str(app_id), EIB.product, kind, board, [selection, values], [list(columns), ["GLOBAL_RANK"], []]]]


def versus(name, values):
    return [name, "7", "8", "255", [str(value) for value in values]]


def skirmish(name, values):
    return [name, "10", "11", "2047", [str(value) for value in values]]


class EIBProfileTests(unittest.TestCase):
    def test_identity_and_sectioned_bootstrap(self):
        self.assertEqual((EIB.slug, EIB.product, EIB.sdk_version, EIB.game_version, EIB.display_name),
                         ("eib", "BIAEIBPS2US", "BIAEIBPS2US1.0", "1.0", "Earned in Blood"))
        self.assertIsInstance(EIB, type(BIA))
        body = EIB.bootstrap("192.0.2.1", 30050, 30051, "192.0.2.1", 30052, 30053)
        self.assertEqual(body, b"[Servers]\r\nRouterIP0=192.0.2.1\r\nRouterPort0=30050\r\n"
                         b"RouterLauncherPort0=30051\r\nNATServerIP0=192.0.2.1\r\nNATServerPort0=30052\r\n"
                         b"IRCIP0=192.0.2.1\r\nIRCPort0=30053\r\n")
        # Without a chat port the IRC keys are left out.
        self.assertNotIn(b"IRC", EIB.bootstrap("192.0.2.1", 30050, 30051, "192.0.2.1", 30052))

    def test_capabilities_match_road_to_hill_30(self):
        for flag in ("supports_discovery", "lobby_members", "supports_rooms", "lobby_create", "owner_game_started",
                     "game_started", "negative_room_ids", "friend_joining", "creator_room_listing",
                     "self_member_event", "nat_address_byteorder", "proxy_services", "bootstrap_section",
                     "profile_country_update"):
            with self.subTest(flag=flag):
                self.assertEqual(getattr(EIB, flag), getattr(BIA, flag))
        self.assertEqual(EIB.bootstrap_keys, BIA.bootstrap_keys + ("IRCIP0", "IRCPort0"))
        # Account refusals (callback 0x004fc2c0) come from the profile; titles
        # without verified codes keep the old behaviour (no reply).
        for reason, code in (("duplicate", 1), ("username", 3), ("password", 11)):
            self.assertEqual(EIB.account_failure(1, reason), [b"\x01", [code.to_bytes(4, "little")]])
        self.assertIsNone(EIB.account_failure(0x66, "password"))
        self.assertIsNone(BIA.account_failure(1, "duplicate"))
        self.assertIsNone(XIII.account_failure(1, "duplicate"))
        self.assertEqual((BIA.chat_port, EIB.chat_port), (None, 30053))
        # Chat IDs: short, stable, case-insensitive; other titles keep the name.
        self.assertEqual(BIA.player_info_id("SharedFixture"), "SharedFixture")
        chat_id = EIB.player_info_id("SharedFixture")
        self.assertRegex(chat_id, r"^p[a-z2-7]{8}$")
        self.assertEqual(chat_id, EIB.player_info_id("sharedfixture"))
        self.assertNotEqual(chat_id, EIB.player_info_id("OtherFixture"))
        self.assertEqual(EIB.friend_presence(False), (8, EIB.product))
        self.assertEqual(EIB.friend_presence(True), (9, EIB.product))
        folder = EIB.parent_lobby(2)
        self.assertEqual((len(folder), folder[1], folder[9], folder[11], folder[13]),
                         (14, "Earned in Blood", EIB.product, "0", "2"))
        # Two parent lobbies, told apart by the type in field 11: type 0 is
        # listed as Adversarial (retail run 2).
        self.assertEqual(EIB.lobby_ids(), ("1", "2"))
        self.assertEqual(BIA.lobby_ids(), ("1",))
        self.assertEqual(EIB.lobby_record("1", 3), EIB.parent_lobby(3))
        skirmish = EIB.lobby_record("2", 1)
        self.assertEqual((len(skirmish), skirmish[1], skirmish[2], skirmish[3], skirmish[11], skirmish[13]),
                         (14, "Earned in Blood Skirmish", "2", "1", "1", "1"))
        self.assertLess(len(skirmish[1].encode()), 33)
        self.assertEqual(EIB.join_parent_lobby(["2", "", "256"]), ["2", ""])
        self.assertEqual([EIB.group_refresh_flags(["1", flags]) for flags in ("64", "448", "256")],
                         ["64", "320", "256"])
        self.assertEqual((EIB.group_refresh_flags(["1"]), BIA.group_refresh_flags(["1", "64"])), ("256", "256"))
        with self.assertRaises(ValueError):
            BIA.join_parent_lobby(["2", "", "256"])
        member = LobbyMember("Fixture", EIB.product, "192.0.2.1", "", print, group="2", lobby="2")
        self.assertEqual(EIB.friend_location(member), [["1", "2"]])
        member.group = "-1000"
        self.assertEqual(EIB.friend_location(member), [["1", "2"], ["1", "-1000"]])
        EIB.discovery_login([EIB.product])
        with self.assertRaises(ValueError):
            EIB.discovery_login([BIA.product])
        self.assertTrue(all(len(item) <= 512 and item.isascii() for item in EIB.news))

    def test_rooms_carry_the_earned_in_blood_product(self):
        registry = RoomRegistry()
        room = registry.register("shared", "192.0.2.2", eib_create(password="pw"), EIB)
        self.assertLess(room.room_id, 0)
        self.assertEqual((room.product, room.info, room.password), (EIB.product, SESSION, "pw"))
        with self.assertRaises(ValueError):
            EIB.registration(eib_create(product=BIA.product), "192.0.2.2")
        with self.assertRaises(ValueError):
            BIA.registration(eib_create(), "192.0.2.2")
        # Room listings are per product, so the two games never see each
        # other's rooms.
        self.assertIs(registry.activate(room.room_id, "shared", "192.0.2.2", EIB.product), room)
        self.assertIs(registry.active_room(room.room_id, EIB.product), room)
        self.assertIsNone(registry.active_room(room.room_id, BIA.product))
        self.assertEqual(registry.children(BIA.product), [])
        skirmish_room = registry.register("other", "192.0.2.3", ["2"] + eib_create()[1:], EIB)
        registry.activate(skirmish_room.room_id, "other", "192.0.2.3", EIB.product)
        self.assertEqual([child[2] for child in registry.children(EIB.product, "2")], [str(skirmish_room.room_id)])
        self.assertEqual([child[2] for child in registry.children(EIB.product, "1")], [str(room.room_id)])
        with self.assertRaises(ValueError):
            EIB.registration(["3"] + eib_create()[1:], "192.0.2.2")
        # The creation reply names the lobby server (1), not the parent
        # lobby (retail run 3).
        self.assertEqual(EIB.lobby_create_result(skirmish_room), [str(skirmish_room.room_id), "", "1"])

    def test_versus_and_skirmish_results_are_validated(self):
        fight = [versus("Winner", [1, 0, 5, 35, 2, 600, 1, 1]), versus("Loser", [-1, 1, 5, 27, 3, 600, 0, 1])]
        self.assertEqual(eib.summarize(fight)[0], eib.VERSUS)
        # SubmitMatchResults' skirmish branch: field IDs 0-10, unused ones 0.
        timed = [skirmish("Able", [1, 12, 0, 0, 0, 0, 431, 0, 0, 0, 0]),
                 skirmish("Baker", [1, 12, 0, 0, 0, 0, 431, 0, 0, 0, 0])]
        kind, players = eib.summarize(timed)
        self.assertEqual((kind, players[1]), (eib.SKIRMISH, ("Baker", [1, 12, 0, 0, 0, 0, 431, 0, 0, 0, 0])))
        self.assertIsNone(EIB.match_results_event("1", timed))
        self.assertEqual([eib.mission_type(m) for m in (1, 10, 11, 20, 21, 30, 31, 40)], [0, 0, 1, 1, 2, 2, 3, 3])
        # MAP_ID is the scripts' own number (0 allowed) and WIN_STATUS is
        # bounded loosely (retail runs 5 and 6 were refused).
        self.assertEqual(eib.summarize([skirmish("Able", [2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])])[0], eib.SKIRMISH)
        for bad in ([skirmish("Able", [1, 256, 0, 0, 0, 0, 0, 0, 0, 0, 0])],
                    [skirmish("Able", [10, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0])],
                    [skirmish("Able", [-2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0])],
                    [skirmish("Able", [1, 1, 0, 0, 0, 100, 0, 0, 0, 0, 0])],
                    [skirmish("Able", [1, 1, 0, -1, 0, 0, 0, 0, 0, 0, 0])],
                    timed[:1] + [skirmish("Baker", [1, 13, 0, 0, 0, 0, 431, 0, 0, 0, 0])],
                    timed + [skirmish("able", [1, 12, 0, 0, 0, 0, 1, 0, 0, 0, 0])]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                EIB.match_results_event("1", bad)
        # A shape neither branch sends is unsupported, so the router logs it.
        for odd in (fight[:1] + timed[:1], [[*timed[0][:4], ["1"] * 12]]):
            with self.subTest(odd=odd), self.assertRaises(UnsupportedOperation):
                EIB.match_results_event("1", odd)

    def test_final_statistics_follow_the_mode(self):
        fight = [versus("Winner", [1, 0, 5, 35, 2, 600, 1, 1])]
        event = EIB.match_results_event("9", fight, None, {})
        self.assertEqual(event, ["71", ["9", "38", [["Winner", ["0"] * 13]]]])
        timed = [skirmish("Able", [1, 12, 0, 0, 0, 0, 431, 0, 0, 0, 0])]
        event = EIB.match_results_event("9", timed, None, {"able": list(range(12))})
        self.assertEqual(event, ["71", ["9", "38", [["Able", [str(value) for value in range(12)]]]]])
        self.assertEqual(EIB.match_results_event("9", timed, None, {})[1][2][0][1], ["0"] * 12)

    def test_ranking_queries_are_validated(self):
        self.assertEqual(EIB.ranking_table(eib_query(5), "x"), ["1", ["5", "0", [], []]])
        self.assertEqual(EIB.ranking_table(eib_query(5, kind="1", board="12"), "x"), ["1", ["5", "0", [], []]])
        with self.assertRaises(ValueError):
            BIA.ranking_table(eib_query(5), "x")

    def test_configuration_uses_the_next_port_block(self):
        # 187: Ride or Die has 30040-30042; Earned in Blood takes the next block.
        env = {"TUBISOFT_ADVERTISE_IP": "192.0.2.1", "TUBISOFT_GAMES": "xiii,gr2,r6,bia,rod,eib"}
        games = service.configuration(env).games
        self.assertEqual([g.profile for g in games], [XIII, GR2, R6, BIA, ROD, EIB])
        self.assertEqual((games[4].router_port, games[4].launcher_port, games[4].nat_port), (30040, 30041, 30042))
        self.assertEqual((games[5].router_port, games[5].launcher_port, games[5].nat_port), (30050, 30051, 30052))
        self.assertEqual((games[3].chat_port, games[4].chat_port, games[5].chat_port), (None, None, 30053))
        with self.assertRaises(ValueError):
            service.configuration(env | {"TUBISOFT_EIB_NAT_PORT": "30032"})
        with self.assertRaises(ValueError):
            service.configuration(env | {"TUBISOFT_EIB_CHAT_PORT": "30050"})

    def test_compose_publishes_the_ports_without_an_overlay(self):
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
        for name, port in (("ROUTER", 30050), ("LAUNCHER", 30051), ("NAT", 30052)):
            self.assertIn(f"TUBISOFT_EIB_{name}_PORT: ${{TUBISOFT_EIB_{name}_PORT:-{port}}}", compose)
        self.assertIn('"${TUBISOFT_EIB_ROUTER_PORT:-30050}:${TUBISOFT_EIB_ROUTER_PORT:-30050}/udp"', compose)
        self.assertIn('"${TUBISOFT_EIB_NAT_PORT:-30052}:${TUBISOFT_EIB_NAT_PORT:-30052}/udp"', compose)
        self.assertIn("TUBISOFT_EIB_CHAT_PORT: ${TUBISOFT_EIB_CHAT_PORT:-30053}", compose)
        self.assertIn('"${TUBISOFT_EIB_CHAT_PORT:-30053}:${TUBISOFT_EIB_CHAT_PORT:-30053}/tcp"', compose)
        self.assertIn("TUBISOFT_EIB_NEWS_1: ${TUBISOFT_EIB_NEWS_1:-}", compose)


class EIBLadderTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "accounts.sqlite3"
        accounts.initialize_database(self.path)
        for name in ("Able", "Baker", "Charlie"):
            self.assertTrue(accounts.create_account(self.path, name, "fixture-only"))
        self.ladder = EIB.match_ladder(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_skirmish_increments_by_mission_type(self):
        objective = eib_ladder.skirmish_increments([1, 3, 0, 12, 4, 2, 0, 0, 0, 0, 0])
        self.assertEqual((objective["wins"], objective["allies"], objective["completion_best"],
                          objective["best_time"]), (1, 1, 4, 0))
        timed = eib_ladder.skirmish_increments([1, 15, 1, 0, 0, 0, 431, 0, 0, 0, 0])
        self.assertEqual((timed["axis"], timed["completion_best"], timed["best_time"]), (1, 0, 431))
        # TIME is the seconds survived, kept whatever the status.
        timed_other = eib_ladder.skirmish_increments([-1, 15, 1, 0, 0, 0, 600, 0, 0, 0, 0])
        self.assertEqual((timed_other["wins"], timed_other["best_time"]), (0, 600))
        tour = eib_ladder.skirmish_increments([0, 35, 0, 20, 6, 1, 0, 0, 0, 0, 3])
        self.assertEqual((tour["completion_best"], tour["maps_reached"]), (3, 3))
        values = eib_ladder.skirmish_values(dict.fromkeys(eib_ladder.SKIRMISH_COUNTERS, 0)
                                            | {"played": 3, "wins": 2, "completion_total": 7, "hits": 9,
                                               "fired": 40})
        # WINS and W_RATIO stay 0: WIN_STATUS 1 marks any completed round.
        self.assertEqual(dict(zip(eib_ladder.SKIRMISH_VALUES, values)),
                         {"WINS": 0, "PLAYED": 3, "W_RATIO": 0, "P_ALLIES": 0, "P_AXIS": 0, "COMPLETION": 0,
                          "AVG_COMPLETION": 2, "TIME": 0, "HITS": 9, "FIRED": 40, "ACCURACY": 22, "DEF_KILLS": 0})

    def test_versus_uses_its_own_table(self):
        entries = [versus("Able", [1, 3, 1, 20, 10, 300, 1, 1]), versus("Baker", [-1, 1, 3, 10, 2, 300, 0, 1]),
                   versus("NoAccount", [0, 0, 0, 0, 0, 300, 0, 1])]
        self.assertIsNone(self.ladder.provisional(entries, None))
        result = self.ladder.record(entries, None)
        self.assertEqual(set(result), {"able", "baker"})
        self.assertEqual(dict(zip(bia_ladder.VALUES, result["able"]))["SCORE"], 3)
        self.assertEqual([row[:2] for row in self.ladder.versus_board()], [(1, "Able"), (2, "Baker")])
        # Road to Hill 30's ladder is untouched.
        self.assertEqual(bia_ladder.BIALadder(self.path).board(), [])
        top = EIB.ladder_ranking_table(eib_query(5), "x", self.path)
        self.assertEqual(top[1][:2], ["5", "2"])
        # Every column, versus and skirmish, the requested ones first.
        self.assertEqual([column for column, _ in top[1][2]],
                         ["GLOBAL_RANK", "ALIAS", "SCORE"] + [c for c in eib_ladder.ALL_COLUMNS
                                                              if c not in ("GLOBAL_RANK", "ALIAS", "SCORE")])
        self.assertEqual([row[:3] for row in top[1][3]], [["1", "Able", "3"], ["2", "Baker", "1"]])
        self.assertTrue(all(cell.lstrip("-").isdecimal() for row in top[1][3] for cell in row[:1] + row[2:]))
        # Skirmish columns are on the versus board too (zeros here); an
        # unknown column gets the empty table.
        self.assertEqual([row[:2] for row in EIB.ladder_ranking_table(
            eib_query(6, columns=("ALIAS", "WINS")), "x", self.path)[1][3]], [["Able", "0"], ["Baker", "0"]])
        self.assertEqual(EIB.ladder_ranking_table(eib_query(6, columns=("ALIAS", "NOPE")), "x", self.path),
                         ["1", ["6", "0", [], []]])

    def test_adversarial_my_rank_asks_for_the_overall_skirmish_board(self):
        # Retail run 11: RequestAroundMe (0x004fb570) is passed the game mode,
        # 0 after an Adversarial match, so the Adversarial My Rank asks for
        # (1, 0) around the player; its handler reads SCORE and the ranked
        # columns by name. Around-me on board 0 follows the versus order over
        # everyone; the skirmish top list keeps the skirmish order.
        self.ladder.record([versus("Baker", [1, 3, 1, 20, 10, 300, 1, 1]),
                            versus("Able", [-1, 1, 3, 10, 2, 300, 0, 1])], None)
        self.ladder.record([skirmish("Able", [1, 1, 0, 0, 0, 0, 100, 0, 0, 0, 0])], "11")
        self.ladder.record([skirmish("Charlie", [1, 1, 0, 0, 0, 0, 50, 0, 0, 0, 0]),
                            skirmish("Able", [1, 1, 0, 0, 0, 0, 60, 0, 0, 0, 0])], "11")
        columns = ("QUERY_RANK", "GLOBAL_RANK", "ALIAS", "COUNTRY", "COUNTRY_ID", "WINS", "PLAYED", "W_RATIO",
                   "P_ALLIES", "P_AXIS")
        mine = EIB.ladder_ranking_table(eib_query(4, kind="1", board="0", selection="1", values=["able", "20"],
                                                  columns=columns), "x", self.path)
        header = [column for column, _ in mine[1][2]]
        rows = [dict(zip(header, row)) for row in mine[1][3]]
        self.assertEqual([(row["GLOBAL_RANK"], row["ALIAS"], row["SCORE"], row["RANKED_WINS"], row["RANKED_RNDS"],
                           row["PLAYED"], row["WINS"]) for row in rows],
                         [("1", "Baker", "3", "1", "1", "0", "0"), ("2", "Able", "1", "0", "1", "2", "0"),
                          ("3", "Charlie", "0", "0", "0", "1", "0")])
        top = EIB.ladder_ranking_table(eib_query(5, kind="1", board="0", columns=columns), "x", self.path)
        self.assertEqual([row[2] for row in top[1][3]], ["Able", "Charlie"])

    def test_retail_run_7_reports_are_recorded(self):
        # Two Ladder Objective matches on map 1 (names replaced): the host
        # reports OBJECTIVES as -1.
        for host, guest in (([1, 1, 0, 2, -1, 4, 0, 0, 0, 0, 0], [1, 1, 0, 0, 0, 2, 0, 0, 0, 0, 0]),
                            ([1, 1, 0, 0, -1, 4, 0, 0, 0, 0, 0], [1, 1, 0, 0, 0, 4, 0, 0, 0, 0, 0])):
            entries = [skirmish("Able", host), skirmish("Baker", guest)]
            self.assertIsNone(self.ladder.provisional(entries, None))
            changes = self.ladder.record(entries, None)
            event = EIB.match_results_event("5", entries, None, changes)
            self.assertEqual([len(values) for _, values in event[1][2]], [12, 12])
        able = dict(zip(eib_ladder.SKIRMISH_VALUES, changes["able"]))
        self.assertEqual((able["WINS"], able["PLAYED"], able["W_RATIO"], able["P_ALLIES"], able["COMPLETION"]),
                         (0, 2, 0, 2, 0))
        self.assertEqual([row[:2] for row in self.ladder.skirmish_board()], [(1, "Able"), (2, "Baker")])
        self.assertEqual(len(EIB.ladder_ranking_table(eib_query(3, kind="1", board="0", columns=(
            "QUERY_RANK", "GLOBAL_RANK", "ALIAS", "COUNTRY", "COUNTRY_ID", "WINS", "PLAYED", "W_RATIO",
            "P_ALLIES", "P_AXIS")), "x", self.path)[1][3]), 2)
        with self.assertRaises(ValueError):
            eib.summarize([skirmish("Able", [1, 1, 0, 0, -2, 0, 0, 0, 0, 0, 0])])

    def test_skirmish_results_go_to_the_game_mode_board(self):
        # Retail run 8: a Timed Assault match started in game mode 11
        # (operation 17) reported MAP_ID 1 and TIME 334 and 562.
        entries = [skirmish("Able", [1, 1, 0, 0, 0, 0, 334, 0, 0, 0, 0]),
                   skirmish("Baker", [1, 1, 0, 0, 0, 0, 562, 0, 0, 0, 0])]
        changes = self.ladder.record(entries, "11")
        self.assertEqual(dict(zip(eib_ladder.SKIRMISH_VALUES, changes["baker"]))["TIME"], 562)
        # Timed Assault boards rank by the longest survival.
        self.assertEqual([row[:2] for row in self.ladder.skirmish_board(11)], [(1, "Baker"), (2, "Able")])
        # Retail run 9: 71 and 183 seconds in a round of about 185; the
        # longest time is kept.
        self.ladder.record([skirmish("Able", [1, 1, 0, 0, 0, 0, 183, 0, 0, 0, 0]),
                            skirmish("Baker", [1, 1, 0, 0, 0, 0, 71, 0, 0, 0, 0])], "11")
        times = {name: values["TIME"] for _, name, values in self.ladder.skirmish_board(11)}
        self.assertEqual(times, {"Able": 334, "Baker": 562})
        self.assertEqual([eib_ladder.board_statistic(board) for board in (0, 1, 11, 22, 35, 41)],
                         [None, "COMPLETION", "TIME", "DEF_KILLS", "COMPLETION", None])
        self.assertEqual(self.ladder.skirmish_board(1), [])
        # Without a usable mode the results' MAP_ID names the board.
        self.ladder.record(entries, "99")
        self.assertEqual(len(self.ladder.skirmish_board(1)), 2)

    def test_skirmish_boards_per_map_and_overall(self):
        timed = [skirmish("Able", [1, 1, 0, 0, 0, 0, 500, 0, 0, 0, 0]),
                 skirmish("Baker", [1, 1, 1, 0, 0, 0, 500, 0, 0, 0, 0])]
        self.ladder.record(timed, "15")
        shorter = self.ladder.record([skirmish("Able", [1, 1, 0, 0, 0, 0, 431, 0, 0, 0, 0])], "15")
        self.assertEqual(dict(zip(eib_ladder.SKIRMISH_VALUES, shorter["able"]))["TIME"], 500)
        longer = self.ladder.record([skirmish("Able", [1, 1, 0, 0, 0, 0, 900, 0, 0, 0, 0])], "15")
        able = dict(zip(eib_ladder.SKIRMISH_VALUES, longer["able"]))
        self.assertEqual((able["WINS"], able["PLAYED"], able["W_RATIO"], able["P_ALLIES"], able["TIME"]),
                         (0, 3, 0, 3, 900))
        defense = self.ladder.record([skirmish("Baker", [-1, 1, 1, 0, 0, 0, 0, 30, 120, 7, 0]),
                                      skirmish("Charlie", [1, 1, 0, 0, 0, 0, 0, 10, 20, 2, 0])], "22")
        baker = dict(zip(eib_ladder.SKIRMISH_VALUES, defense["baker"]))
        self.assertEqual((baker["PLAYED"], baker["ACCURACY"], baker["DEF_KILLS"]), (1, 25, 7))
        # Timed Assault ranks by TIME, Defense by DEF_KILLS, the overall
        # board by rounds played.
        self.assertEqual([row[:2] for row in self.ladder.skirmish_board(15)], [(1, "Able"), (2, "Baker")])
        self.assertEqual([row[:2] for row in self.ladder.skirmish_board(22)], [(1, "Baker"), (2, "Charlie")])
        self.assertEqual([row[1] for row in self.ladder.skirmish_board()], ["Able", "Baker", "Charlie"])
        overall = {name: values for _, name, values in self.ladder.skirmish_board()}
        self.assertEqual((overall["Baker"]["WINS"], overall["Baker"]["PLAYED"], overall["Baker"]["W_RATIO"]),
                         (0, 2, 0))
        self.assertEqual(overall["Able"]["TIME"], 900)
        mission = EIB.ladder_ranking_table(eib_query(7, kind="1", board="15", columns=(
            "QUERY_RANK", "GLOBAL_RANK", "ALIAS", "COUNTRY", "COUNTRY_ID", "WINS", "PLAYED", "W_RATIO",
            "P_ALLIES", "P_AXIS", "TIME")), "x", self.path)
        able_row = dict(zip([column for column, _ in mission[1][2]], mission[1][3][0]))
        self.assertEqual(mission[1][3][0][:11], ["1", "1", "Able", "0", "0", "0", "3", "0", "3", "0", "900"])
        self.assertEqual((len(able_row), able_row["SCORE"], able_row["DEF_KILLS"]), (len(eib_ladder.ALL_COLUMNS), "0", "0"))
        around = EIB.ladder_ranking_table(eib_query(8, kind="1", board="0", selection="1", values=["baker", "1"],
                                                    columns=("ALIAS", "WINS")), "x", self.path)
        self.assertEqual([row[:2] for row in around[1][3]], [["Baker", "0"]])
        self.assertEqual(EIB.ladder_ranking_table(eib_query(9, kind="1", board="41"), "x", self.path),
                         ["1", ["9", "0", [], []]])
        self.assertEqual(EIB.ladder_ranking_table(eib_query(9, kind="2"), "x", self.path),
                         ["1", ["9", "0", [], []]])


@asynccontextmanager
async def running_service(directory):
    reservations, ports = reserve_ports(8)
    try:
        env = {"TUBISOFT_ADVERTISE_IP": "127.0.0.1", "TUBISOFT_BIND_IP": "127.0.0.1",
               "TUBISOFT_STATE_DIR": directory, "TUBISOFT_GAMES": "xiii,eib",
               "TUBISOFT_HTTP_PORT": str(ports[6]), "TUBISOFT_EIB_CHAT_PORT": str(ports[7])}
        for prefix, selected in (("TUBISOFT_", ports[:3]), ("TUBISOFT_EIB_", ports[3:6])):
            for role, port in zip(("ROUTER", "LAUNCHER", "NAT"), selected):
                env[prefix + role + "_PORT"] = str(port)
        args = service.configuration(env)
    finally:
        for sock in reservations:
            sock.close()
    task = asyncio.create_task(service.serve(args))
    clients = []
    private = None

    async def connect(port, route=(1, 2)):
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
        client.route = route
        clients.append(client)
        if route != (8, 11):
            if private is None:
                private = router.load_key(args.private_key)
            await client.handshake(private)
        return client

    try:
        yield ports, connect
    finally:
        for client in clients:
            client.writer.close()
            await client.writer.wait_closed()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class EIBSignInTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_sign_in_sequence_reaches_the_parent_lobby(self):
        with TemporaryDirectory() as directory:
            async with running_service(directory) as (ports, connect):
                for attempt in range(100):
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", ports[6])
                        break
                    except ConnectionRefusedError:
                        if attempt == 99:
                            raise
                        await asyncio.sleep(0.01)
                # The request format is Road to Hill 30's (pointer table
                # 0x0061df50): "GET /gsinit.php?dp=%s&user=%s".
                writer.write(b"GET /gsinit.php?dp=BIAEIBPS2US&user=private-fixture HTTP/1.1\r\n"
                             b"HOST: gsconnect.ubisoft.com\r\n\r\n")
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 2)
                writer.close()
                await writer.wait_closed()
                self.assertIn(b"200 OK", response)
                self.assertIn(f"[Servers]\r\nRouterIP0=127.0.0.1\r\nRouterPort0={ports[3]}\r\n".encode(), response)
                self.assertIn(f"IRCIP0=127.0.0.1\r\nIRCPort0={ports[7]}\r\n".encode(), response)
                creator = await connect(ports[3])
                await creator.send(1, [EIB.sdk_version, "SharedFixture", "fixture-only", "", "", "", ""],
                                   encrypted=True)
                header, fields = await creator.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x01"]))
                # Retail run 1: a taken name got no reply and the game waited.
                # Refusals are GSFAIL with a four-byte reason (callback
                # 0x004fc2c0): 1 the name exists, 3 the name is not allowed,
                # 11 the password is not allowed. Each request has its own
                # connection, as in the retail log.
                for user, password, reason in (("sharedfixture", "other-fixture", 1), ("x" * 17, "fixture-only", 3),
                                               ("OtherFixture", "", 11)):
                    creator = await connect(ports[3])
                    await creator.send(1, [EIB.sdk_version, user, password, "", "", "", ""], encrypted=True)
                    header, fields = await creator.receive()
                    self.assertEqual((header.message_type, fields), (0x27, [b"\x01", [reason.to_bytes(4, "little")]]))
                login = await connect(ports[3])
                await login.send(0x66, ["SharedFixture", "fixture-only", EIB.sdk_version, b"\1"], encrypted=True)
                header, fields = await login.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x66"]))
                await login.send(0x5d)
                await login.receive()
                wait = await connect(ports[3])
                await wait.send(0x4d, ["SharedFixture"])
                header, _ = await wait.receive()
                self.assertEqual(header.message_type, 0x26)
                await wait.send(0xd1, ["21", [EIB.product]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["21"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["53", ["1", "256", ["0"], [EIB.lobby_record("1", 0),
                                                                      EIB.lobby_record("2", 0)]]])
                # Player info: reader 0x004c0d00 files seven strings. Field 0
                # is the IRCID (LoginRcv_PlayerInfo 0x004fba00 copies 10
                # bytes) and field 1 the alias, as Road to Hill 30 lays out.
                await wait.send(0x13, ["SharedFixture"])
                _, fields = await wait.receive()
                self.assertEqual((fields[0], len(fields[1]), fields[1][:2]),
                                 (b"\x13", 7, [EIB.player_info_id("SharedFixture"), "SharedFixture"]))
                await wait.send(0xde, ["en"])
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\xde", list(EIB.news)]))
                await wait.send(0xd1, ["3", ["1"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["3", ["1", "127.0.0.1", str(ports[3])]]])
                lobby = await connect(ports[3])
                await lobby.send(0xd2, ["SharedFixture", "1", "192.0.2.5", "0.0.0.0", "0"])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["210", ["1"]])
                await lobby.send(0xd1, ["23", ["1", "", "256"]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["38", ["23", ["1", ""]]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["53", ["1", "256", EIB.parent_lobby(1), []]])
                _, fields = await lobby.receive()
                self.assertEqual(fields, ["50", ["SharedFixture", "0", "1", "127.0.0.1", "127.0.0.1", b"", "0"]])


class EIBSkirmishMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_players_finish_a_skirmish_match_in_the_skirmish_lobby(self):
        with TemporaryDirectory() as directory:
            async with running_service(directory) as (ports, connect):
                async def lobby_member(user, lobby_id):
                    creator = await connect(ports[3])
                    await creator.send(1, [EIB.sdk_version, user, "fixture-only", "", "", "", ""], encrypted=True)
                    await creator.receive()
                    login = await connect(ports[3])
                    await login.send(0x66, [user, "fixture-only", EIB.sdk_version, b"\1"], encrypted=True)
                    header, _ = await login.receive()
                    self.assertEqual(header.message_type, 0x26)
                    await login.send(0x5d)
                    await login.receive()
                    wait = await connect(ports[3])
                    await wait.send(0x4d, [user])
                    await wait.receive()
                    await wait.send(0xd1, ["3", ["1"]])
                    await wait.receive()
                    lobby = await connect(ports[3])
                    await lobby.send(0xd2, [user, "1", "192.0.2.5", "0.0.0.0", "0"])
                    await lobby.receive()
                    await lobby.send(0xd1, ["23", [lobby_id, "", "256"]])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields, ["38", ["23", [lobby_id, ""]]])
                    _, fields = await lobby.receive()
                    self.assertEqual(fields[:1] + fields[1][:1], ["53", lobby_id])
                    return lobby

                async def player_data(client):
                    # Operation 42 follows the player's own member event.
                    await client.send(0xd1, ["42", [bytes(12)]])
                    _, fields = await client.receive()
                    self.assertEqual(fields, ["38", ["42"]])

                # A player in the Adversarial lobby sees nothing of the
                # Skirmish lobby's members or rooms.
                adversary = await lobby_member("AdversaryFixture", "1")
                await adversary.receive()  # its own member event
                await player_data(adversary)
                first = await lobby_member("FirstFixture", "2")
                _, fields = await first.receive()  # its own member event
                self.assertEqual(fields[1][:3], ["FirstFixture", "0", "2"])
                await player_data(first)
                second = await lobby_member("SecondFixture", "2")
                for _ in range(2):
                    await second.receive()  # the first player, then itself
                await player_data(second)
                await first.receive()  # the second player
                # A room must name the creator's own lobby.
                await first.send(0xd1, ["12", ["2"] + eib_create(players="2")[1:]])
                _, fields = await first.receive()
                room_id = fields[1][1][0]
                # Lobby server 1: CreateRoomReply joins the room through it.
                self.assertEqual(fields, ["38", ["12", [room_id, "", "1"]]])
                for client in (first, second):
                    _, fields = await client.receive()
                    self.assertEqual(fields[0], "53")
                await second.receive()  # the owner moved into the room
                await first.send(0xd1, ["24", [room_id, "", "0", "128", EIB.sdk_version]])
                await first.receive()
                await first.receive()
                await second.send(0xd1, ["24", [room_id, "", "0", "128", EIB.sdk_version]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["38", ["24", [room_id, ""]]])
                for client in (second, second, first):
                    await client.receive()
                await first.send(0xd1, ["17", [room_id, "1"]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["17", [room_id]]])
                for client in (first, second):
                    _, fields = await client.receive()
                    self.assertEqual(fields[0], "62")
                    match_id = fields[1][1]
                # An out-of-range report (map 256) is acknowledged but not
                # kept, and the room stays open (retail run 5 closed it).
                await first.send(0xd1, ["30", [match_id, "0",
                                               skirmish("FirstFixture", [1, 256, 0, 0, 0, 0, 0, 10, 40, 3, 0])]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["30", [match_id]]])
                # A Defense mission (map 22): fields 7-9 carry the values.
                await first.send(0xd1, ["30", [match_id, "0",
                                               skirmish("FirstFixture", [1, 22, 0, 0, 0, 0, 0, 10, 40, 3, 0]),
                                               skirmish("SecondFixture", [1, 22, 0, 0, 0, 0, 0, 5, 20, 1, 0])]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["30", [match_id]]])
                await first.send(0xd1, ["45", [room_id]])
                _, fields = await first.receive()
                self.assertEqual(fields, ["38", ["45", [room_id, "1"]]])
                for client in (first, second):
                    _, fields = await client.receive()
                    self.assertEqual(fields[:1] + fields[1][:2], ["71", match_id, "38"])
                    self.assertEqual(fields[1][2], [
                        ["FirstFixture", ["0", "1", "0", "1", "0", "0", "0", "0", "10", "40", "25", "3"]],
                        ["SecondFixture", ["0", "1", "0", "1", "0", "0", "0", "0", "5", "20", "25", "1"]]])
                # The Adversarial lobby has one player and no rooms; the
                # Skirmish lobby has two players and the room.
                # Refresh replies echo the request's 0x40 (the lobby's own
                # record, which updates the listed player count) and 0x100
                # (its rooms).
                await adversary.send(0xd1, ["9", ["1", "448"]])
                _, fields = await adversary.receive()
                self.assertEqual(fields, ["53", ["1", "320", EIB.lobby_record("1", 1), []]])
                await second.send(0xd1, ["9", ["2", "64"]])
                _, fields = await second.receive()
                self.assertEqual(fields, ["53", ["2", "64", EIB.lobby_record("2", 2), []]])
                await second.send(0xd1, ["9", ["2", "448"]])
                _, fields = await second.receive()
                self.assertEqual([child[2] for child in fields[1][3]], [room_id])
                # Retail run 10: the lobby list kept counting a player who
                # had left. A player in the other lobby sees the count drop.
                await adversary.send(0xd1, ["9", ["2", "64"]])
                _, fields = await adversary.receive()
                self.assertEqual(fields[1][2][13], "2")
                second.writer.close()
                await second.writer.wait_closed()
                for _ in range(100):
                    await adversary.send(0xd1, ["9", ["2", "64"]])
                    _, fields = await adversary.receive()
                    if fields[1][2][13] == "1":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(fields, ["53", ["2", "64", EIB.lobby_record("2", 1), []]])


if __name__ == "__main__":
    unittest.main()


def chat_packet(line: bytes) -> bytes:
    # The client's own packets (0x005e03b0): one CRLF line, no NUL.
    payload = crypto.encrypt_payload(irc.KEY, line + b"\r\n")
    return len(payload).to_bytes(2, "big") + payload


async def chat_receive(reader) -> str:
    size = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), 2), "big")
    plain = crypto.decrypt_payload(irc.KEY, await reader.readexactly(size))
    # Reader 0x005e08a0 copies only the trailer's length: the NUL ends it.
    assert plain.endswith(b"\r\n\x00"), plain
    return plain[:-3].decode()


class EIBChatTests(unittest.IsolatedAsyncioTestCase):
    def test_packets_use_the_fixed_blowfish_key_and_a_length_prefix(self):
        packet = irc.encode_packet(":tubisoft 001 pabc :Welcome")
        self.assertEqual(int.from_bytes(packet[:2], "big"), len(packet) - 2)
        self.assertEqual((len(packet) - 4) % 8, 0)
        self.assertEqual(irc.decode_payload(packet[2:]), [":tubisoft 001 pabc :Welcome"])
        self.assertEqual(irc.decode_payload(chat_packet(b"NICK :pabc")[2:]), ["NICK :pabc"])
        # IRCSendPrivateMessage's text keeps its colons and percent signs.
        self.assertEqual(irc.parse("PRIVMSG pabc :A%1%1%0%0%x%y"), ("PRIVMSG", ["pabc", "A%1%1%0%0%x%y"]))
        self.assertEqual(irc.parse(":n!u@h USER BIAEIBPS2US1.0 +i 0 :Name"),
                         ("USER", ["BIAEIBPS2US1.0", "+i", "0", "Name"]))

    async def test_commands_are_rechecked_after_sign_out(self):
        registry = RoomRegistry()
        server = irc.ChatServer(registry, EIB, None)
        sent = []

        class Transport:
            def is_closing(self):
                return False

            def get_write_buffer_size(self):
                return 0

        class Writer:
            transport = Transport()

            def write(self, packet):
                sent.extend(irc.decode_payload(packet[2:]))

        client = irc.Client(Writer(), ("192.0.2.5", 1), nick=EIB.player_info_id("Fixture"), user="Fixture",
                            registered=True)
        registry.friend_channels["fixture"] = (object(), print)
        registry.chat_hosts["fixture"] = "192.0.2.5"
        self.assertTrue(await server.dispatch(client, "PING", ["x"]))
        self.assertEqual(sent[-1], ":tubisoft PONG tubisoft :x")
        # The friends session is gone (or moved to another address): the
        # next command closes the chat connection instead of being handled.
        registry.chat_hosts["fixture"] = "192.0.2.6"
        self.assertFalse(await server.dispatch(client, "PRIVMSG", ["pabc", "x%1%1%1%1%x%"]))
        self.assertEqual(sent[-1], "ERROR :Closing link (tubisoft: signed out)")
        del registry.friend_channels["fixture"]
        self.assertFalse(await server.dispatch(client, "JOIN", ["#LobbyGrp2.1"]))
        self.assertEqual(server.channels, {})

    async def test_signed_in_friends_exchange_a_lobby_invitation(self):
        with TemporaryDirectory() as directory:
            async with running_service(directory) as (ports, connect):
                async def signed_in(user):
                    creator = await connect(ports[3])
                    await creator.send(1, [EIB.sdk_version, user, "fixture-only", "", "", "", ""], encrypted=True)
                    await creator.receive()
                    login = await connect(ports[3])
                    await login.send(0x66, [user, "fixture-only", EIB.sdk_version, b"\1"], encrypted=True)
                    await login.receive()
                    await login.send(0x5d)
                    await login.receive()
                    wait = await connect(ports[3])
                    await wait.send(0x4d, [user])
                    await wait.receive()
                    await wait.send(0x4e, [bytes(4), bytes(4)])
                    header, _ = await wait.receive()
                    self.assertEqual(header.message_type, 0x26)
                    return wait

                async def chat(nick, user):
                    reader, writer = await asyncio.open_connection("127.0.0.1", ports[7])
                    # Login 0x005e0b30: NICK, then USER with the SDK version,
                    # "+i", "0" and the username.
                    writer.write(chat_packet(f"NICK :{nick}".encode())
                                 + chat_packet(f"USER {EIB.sdk_version} +i 0 :{user}".encode()))
                    await writer.drain()
                    return reader, writer

                inviter = await signed_in("FirstFixture")
                await signed_in("SecondFixture")
                # The inviter looks up its friend's IRCID with 0x13 (stored
                # at +0x217 once its own chat login has succeeded).
                await inviter.send(0x13, ["SecondFixture"])
                _, fields = await inviter.receive()
                target = fields[1][0]
                self.assertEqual(target, EIB.player_info_id("SecondFixture"))
                first_nick = EIB.player_info_id("FirstFixture")
                writers = []
                try:
                    # Someone else's ID, or a player not signed in, is refused.
                    for nick, user in ((target, "FirstFixture"), (EIB.player_info_id("Absent"), "Absent")):
                        reader, writer = await chat(nick, user)
                        writers.append(writer)
                        self.assertTrue((await chat_receive(reader)).startswith("ERROR :Closing link"))
                        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
                    first, writer = await chat(first_nick, "FirstFixture")
                    writers.append(writer)
                    self.assertEqual(await chat_receive(first), f":tubisoft 001 {first_nick} :Welcome to Tubisoft chat")
                    second, second_writer = await chat(target, "SecondFixture")
                    writers.append(second_writer)
                    self.assertEqual(await chat_receive(second), f":tubisoft 001 {target} :Welcome to Tubisoft chat")
                    # After 001 the client sends MODE <nick> +i, then joins its
                    # lobby's channel #LobbyGrp<lobby>.<lobby server>.
                    second_writer.write(chat_packet(f"MODE {target} :+i".encode())
                                        + chat_packet(b"JOIN :#LobbyGrp2.1"))
                    self.assertEqual(await chat_receive(second), f":{target}!{target}@tubisoft JOIN :#LobbyGrp2.1")
                    second_writer.write(chat_packet(b"PING :x"))
                    self.assertEqual(await chat_receive(second), ":tubisoft PONG tubisoft :x")
                    # IRCSendPrivateMessage (0x005ddcf0) formats the invitation
                    # "%s%%%d%%%d%%%d%%%d%%%s%%%s"; the receiver's handler
                    # (0x005de460) wants its own nick as the target.
                    invitation = "FirstFixture%2%1%0%0%Earned in Blood Skirmish%"
                    writer.write(chat_packet(f"PRIVMSG {target} :{invitation}".encode()))
                    self.assertEqual(await chat_receive(second),
                                     f":{first_nick}!{first_nick}@tubisoft PRIVMSG {target} :{invitation}")
                    writer.write(chat_packet(b"PRIVMSG pabsent :x%1%1%1%1%x%"))
                    self.assertEqual(await chat_receive(first), f":tubisoft 401 {first_nick} pabsent :No such nick")
                    # A player who ignores the sender gets nothing.
                    accounts.add_ignored(Path(directory) / "accounts.sqlite3", "SecondFixture", "FirstFixture")
                    writer.write(chat_packet(f"PRIVMSG {target} :{invitation}".encode())
                                 + chat_packet(f"PRIVMSG {target} :{invitation}".encode()))
                    second_writer.write(chat_packet(b"PING :y"))
                    self.assertEqual(await chat_receive(second), ":tubisoft PONG tubisoft :y")
                    # Signing out of the friends service closes that player's
                    # chat session (RoomRegistry.end_chat).
                    inviter.writer.close()
                    self.assertEqual(await chat_receive(first), "ERROR :Closing link (tubisoft: signed out)")
                    self.assertEqual(await asyncio.wait_for(first.read(), 2), b"")
                finally:
                    for writer in writers:
                        writer.close()
