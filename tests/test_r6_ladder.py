"""Rainbow Six 3 stored ladders and the Tubisoft Elo rating (not Ubisoft's formula)."""
from pathlib import Path
import tempfile
import unittest

from ubisoft import accounts, proxy
from ubisoft.games import r6_ladder
from ubisoft.games.r6_ladder import R6Ladder, Summary, elo_changes, level, summarize
from ubisoft.games.registry import R6

from test_r6_profile import r6_query


def entry(name, team, kills=0, deaths=0, team_kills=0, seconds=60, victim_level=0):
    # Operation 30 entry: name, highest field ID, count, mask, fields 0-19.
    fields = [0] * 20
    fields[0], fields[1 + victim_level], fields[17], fields[18], fields[19] = (
        team, kills, deaths, team_kills, seconds)
    return [name, "19", "20", "1048575", [str(value) for value in fields]]


def country_filter(value, operator=0):
    # 0x8c-byte filter built by 0x00518af0: name padded to 0x84, then two words.
    return b"COUNTRY_ID".ljust(0x84, b"\0") + operator.to_bytes(4, "little") + value.to_bytes(4, "little")


def player(name, team=5, frag=0, deaths=0, wins=0):
    return Summary(name, team, frag, frag, deaths, 0, 1, 0, wins, 60)


class CountryTests(unittest.TestCase):
    def test_country_ids_follow_the_game_lists(self):
        self.assertEqual(len(r6_ladder.COUNTRY_IDS), 240)
        self.assertEqual([r6_ladder.country_id(code) for code in ("ca", "CA", "us", "uk", "de", "nll", "", "zz")],
                         [153, 153, 309, 308, 35, 0, 0, 0])
        self.assertEqual(r6_ladder.ranking_filters([country_filter(153)]), [("COUNTRY_ID", 0, 153)])
        with self.assertRaises(ValueError):
            r6_ladder.ranking_filters(["COUNTRY_ID"])


class EloTests(unittest.TestCase):
    def test_summary_fields(self):
        (one,) = summarize([entry("One", 5, kills=2, deaths=3, team_kills=1, seconds=90, victim_level=4)], "3")
        self.assertEqual(one, Summary("One", 5, 1, 2, 3, 1, 1, 0, 0, 90))
        with self.assertRaises(ValueError):
            summarize([["One", "19", "20", "1", ["x"]]], "3")
        # The game only counts up: negative and out-of-range values are
        # rejected before anything is rated or stored.
        for field_id, value in ((1, "-1"), (17, "-5"), (18, "-1"), (19, "-30"), (0, "256"),
                                (5, "10000"), (19, "86401"), (3, "+1"), (3, "\uff11")):
            fields = ["0"] * 20
            fields[field_id] = value
            with self.subTest(field=field_id, value=value), self.assertRaises(ValueError):
                summarize([["One", "19", "20", "1048575", fields]], "3")
        # A player may appear only once in a report.
        with self.assertRaises(ValueError):
            summarize([entry("One", 5), entry("ONE", 6)], "3")
        fields = ["255"] + ["9999"] * 18 + ["86400"]
        (top,) = summarize([["One", "19", "20", "1048575", fields]], "3")
        self.assertEqual((top.kills, top.deaths, top.seconds), (16 * 9999, 9999, 86400))

    def test_levels_follow_the_rating(self):
        self.assertEqual([level(r) for r in (0, 1000, 1049, 1050, 1749, 1750, 9999)], [0, 0, 0, 1, 14, 15, 15])

    def test_sharpshooter_pairs_compare_frag_scores(self):
        ratings = {"one": 1000, "two": 1000, "three": 1000}
        changes = elo_changes([player("One", frag=5), player("Two", frag=5), player("Three", frag=1)],
                              ratings, "3")
        self.assertEqual(changes, {"one": 8, "two": 8, "three": -16})
        # A stronger winner gains less than an even match would give.
        self.assertEqual(elo_changes([player("One", frag=2), player("Two")], {"one": 1200, "two": 1000}, "3"),
                         {"one": 8, "two": -8})
        # Players without an account are neither rated nor opponents.
        self.assertEqual(elo_changes([player("One", frag=2), player("Guest")], {"one": 1000}, "3"), {"one": 0})

    def test_survival_rewards_surviving_first(self):
        changes = elo_changes([player("One", frag=0), player("Two", frag=3, deaths=1)],
                              {"one": 1000, "two": 1000}, "1")
        self.assertEqual(changes, {"one": 16, "two": -16})

    def test_team_survival_compares_opposing_teams_only(self):
        players = [player("One", 2, wins=1), player("Two", 2, wins=1), player("Three", 3)]
        changes = elo_changes(players, dict.fromkeys(("one", "two", "three"), 1000), "2")
        self.assertEqual(changes, {"one": 16, "two": 16, "three": -16})


class StoredLadderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "accounts.sqlite3"
        for name in ("LadderOne", "LadderTwo"):
            accounts.create_account(self.path, name, "fixture-only")
        self.ladder = R6Ladder(self.path)

    def test_finished_matches_update_mode_and_overall_ladders(self):
        match = [entry("LadderOne", 5, kills=3, deaths=1, seconds=120),
                 entry("laddertwo", 6, kills=1, deaths=3, team_kills=1, seconds=120),
                 entry("NoAccount", 7, kills=9)]
        self.assertEqual(self.ladder.provisional(match, "3"), {"ladderone": (16, 0), "laddertwo": (-16, 0)})
        self.assertEqual(self.ladder.board(3), [])
        self.assertEqual(self.ladder.record(match, "3"), {"ladderone": (16, 0), "laddertwo": (-16, 0)})
        for ladder in (3, 0):
            self.assertEqual(self.ladder.board(ladder), [
                (1, "LadderOne", 1016, 3, 3, 1, 0, 1, 0, 0, 120, 0),
                (2, "LadderTwo", 984, 0, 1, 3, 1, 1, 0, 0, 120, 0)])
        self.assertEqual(self.ladder.board(1), [])
        # Unknown modes are not rated.
        self.assertEqual(self.ladder.record(match, None), {})
        # Levels change once the rating crosses a 50-point step.
        for _ in range(3):
            result = self.ladder.record(match, "3")
        self.assertEqual(result["ladderone"][1], 1)

    def test_ranking_queries_read_the_stored_ladder(self):
        self.ladder.record([entry("LadderOne", 5, kills=3, deaths=2), entry("LadderTwo", 6, kills=1)], "3")
        columns = ("ALIAS", "LEVEL", "RATING", "KILLS", "DEATHS", "RATIO", "GLOBAL_RANK", "TIMEPLAYED")
        fields = r6_query(5, columns=columns)
        fields[1][3] = "3"
        fields[1][4] = ["2", ["laddertwo", "Unplayed"]]
        # Requested columns come first; the rest follow (top-list parser
        # 0x00485410 also reads COUNTRY_ID, which it does not request).
        table = r6_ladder.ranking_table(R6, fields, "laddertwo", self.path)[1]
        self.assertEqual(table[:2], ["5", "2"])
        self.assertEqual([column[0] for column in table[2]][:len(columns)], list(columns))
        self.assertEqual(len(table[2]), len(r6_ladder.COLUMNS))
        self.assertEqual([row[:len(columns)] for row in table[3]], [
            ["LadderTwo", "0", "984", "1", "0", "100", "2", "60"],
            ["Unplayed", "0", "0", "0", "0", "0", "0", "0"]])
        top = r6_query(6, selection="3", columns=("ALIAS", "GLOBAL_RANK", "RATING"))
        top[1][3] = "3"
        table = r6_ladder.ranking_table(R6, top, "x", self.path)[1]
        self.assertIn(["COUNTRY_ID", "COUNTRY_ID"], table[2])
        self.assertEqual([row[:3] for row in table[3]], [["LadderOne", "1", "1016"], ["LadderTwo", "2", "984"]])
        around = r6_query(7, selection="1", columns=("ALIAS", "GLOBAL_RANK"))
        around[1][3] = "0"
        around[1][4] = ["1", ["laddertwo", "1"]]
        self.assertEqual([row[:2] for row in r6_ladder.ranking_table(R6, around, "x", self.path)[1][3]],
                         [["LadderTwo", "2"]])
        # My Rank and the ladder screens send no columns: all are returned.
        mine = r6_query(9, selection="1", columns=())
        mine[1][3] = "3"
        mine[1][4] = ["1", ["ladderone", "1"]]
        table = r6_ladder.ranking_table(R6, mine, "x", self.path)
        self.assertEqual([column[0] for column in table[1][2]], list(r6_ladder.COLUMNS))
        self.assertEqual(table[1][3], [["1", "LadderOne", "0", "1016", "3", "3", "2", "150",
                                        "0", "1", "0", "0", "0", "60", "0"]])
        # Every cell but ALIAS is an integer string (strtol in 0x0043c650).
        for row in table[1][3]:
            self.assertTrue(all(cell.lstrip("-").isdecimal() for i, cell in enumerate(row) if i != 1))
        browse = r6_query(10, selection="3", columns=())
        browse[1][3] = "0"
        self.assertEqual(len(r6_ladder.ranking_table(R6, browse, "x", self.path)[1][3]), 2)
        # Empty ladders and unranked players keep the empty form.
        empty = r6_query(8, selection="3", columns=("ALIAS", "GLOBAL_RANK", "RATING"))
        self.assertEqual(r6_ladder.ranking_table(R6, empty, "x", self.path), ["1", ["8", "0", [], []]])
        # COUNTRY_ID is the numeric ID of the stored profile code.
        accounts.set_country(self.path, "LadderOne", "ca")
        accounts.set_country(self.path, "LadderTwo", "us")
        table = r6_ladder.ranking_table(R6, mine, "x", self.path)
        self.assertEqual(table[1][3][0][14], "153")
        # A COUNTRY_ID filter keeps that country's rows and global ranks.
        us = r6_query(11, selection="3", columns=("ALIAS", "GLOBAL_RANK", "COUNTRY_ID"))
        us[1][3] = "3"
        us[1][5][2] = [country_filter(309)]
        self.assertEqual([row[:3] for row in r6_ladder.ranking_table(R6, us, "x", self.path)[1][3]],
                         [["LadderTwo", "2", "309"]])
        around = r6_query(12, selection="1", columns=("ALIAS", "GLOBAL_RANK"))
        around[1][3] = "3"
        around[1][4] = ["1", ["laddertwo", "1"]]
        around[1][5][2] = [country_filter(309)]
        self.assertEqual([row[:2] for row in r6_ladder.ranking_table(R6, around, "x", self.path)[1][3]],
                         [["LadderTwo", "2"]])
        around[1][4] = ["1", ["ladderone", "1"]]
        self.assertEqual(r6_ladder.ranking_table(R6, around, "x", self.path), ["1", ["12", "0", [], []]])
        us[1][5][2] = [country_filter(0)]
        self.assertEqual(len(r6_ladder.ranking_table(R6, us, "x", self.path)[1][3]), 2)
        us[1][5][2] = [b"COUNTRY_ID"]
        self.assertEqual(r6_ladder.ranking_table(R6, us, "x", self.path), ["1", ["11", "0", [], []]])
        # The proxy answers R6 queries from the stored ladder.
        reply = proxy.query_reply(R6, ["1281", "4", top], "x", account_db=self.path)
        self.assertEqual(reply[2][1][1][3][0][:3], ["LadderOne", "1", "1016"])


if __name__ == "__main__":
    unittest.main()
