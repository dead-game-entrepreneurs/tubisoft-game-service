"""Earned in Blood ladder: Road to Hill 30's versus totals plus skirmish boards.

Versus results use Road to Hill 30's eight values, thirteen totals and
SCORE rule (bia_ladder), kept in their own table, ``eib_ladder``. Skirmish
results (eleven values, eib.SKIRMISH_LIMITS) are kept per account and
game mode in ``eib_skirmish`` (column ``map_id``). The game mode (1-40) is
the value StartMatch (0x004ef8c0, "Mode is %d", subsystem +0x4e8) sends
with operation 17, and the one the ladder request uses for a mission's
board. The results' own MAP_ID does not follow it: retail run 8, a Timed
Assault match in game mode 11, reported MAP_ID 1.

The ladder request (0x004fb3d0) puts two numbers in query fields 2 and 3:
(0, 0) for the versus board, (1, 0) for the overall skirmish board and
(1, game mode) for one mission's board. Its column tables:

- versus top list 0x0061e110 (5 columns), player information 0x0061df80 (18);
- skirmish overall 0x0061e020 (10), player information 0x0061dfd0 (17);
- per mission, by (game mode - 1) // 10: Objective and Tour of Duty
  0x0061e050 (adds COMPLETION, AVG_COMPLETION), Timed Assault 0x0061e080
  (TIME), Defense 0x0061e0b0 (HITS, FIRED, ACCURACY, DEF_KILLS).

Ubisoft's server computed the skirmish columns; the client only displays
them. Their meanings here are Tubisoft's own, chosen by the project owner
after retail runs 7-9:

- WINS and W_RATIO: 0. Both players send WIN_STATUS 1 for every finished
  round, won or lost (runs 7-9), so a win cannot be told apart, and
  counting completed rounds showed wins nobody had (run 11). The completed
  rounds are still stored (counter ``wins``).
- PLAYED: rounds reported.
- P_ALLIES, P_AXIS: rounds played on each side (SIDE_SKR 0 is the Allies).
- COMPLETION: the best single-round completion, i.e. maps reached (Tour of
  Duty, the only type that sends them) or else objectives completed
  (Objective); AVG_COMPLETION: the average per round, rounded down. Other
  mission types send neither (fields a type does not use are 0).
- TIME: the longest TIME_SKR, the seconds the player survived (only Timed
  Assault sends it; run 9: 71 and 183 in a round of about 185 seconds).
- HITS, FIRED, DEF_KILLS: totals; ACCURACY = hits x 100 / fired.

A mission's board ranks by its own statistic, by the game mode's mission
type: Objective and Tour of Duty by COMPLETION, Timed Assault by TIME,
Defense by DEF_KILLS. The overall board ranks by PLAYED. Ties go to PLAYED,
then the name.

The game cannot be told apart by its requests in one case: RequestAroundMe
(0x004fb570, "My Rank") is passed the current game mode, so after an
Adversarial match (mode 0) the Adversarial My Rank asks for the overall
skirmish board (1, 0), exactly as the Skirmish overall My Rank does. Its
reply handler (0x00500980) reads GLOBAL_RANK, SCORE, RANKED_WINS,
RANKED_RNDS and ALIAS for every around-me list, then the mode's skirmish
columns. So every reply carries every column, versus and skirmish
totals together (overlapping columns from the board's own kind), and an
around-me list on board 0 follows the versus order, over everyone who has
played either kind (run 11; chosen by the project owner). Top lists on
board 0 keep the skirmish order.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import bia_ladder
from .bia_ladder import BIALadder

IDENTITY = ("QUERY_RANK", "GLOBAL_RANK", "ALIAS", "COUNTRY", "COUNTRY_ID")
VERSUS_COLUMNS = IDENTITY + bia_ladder.VALUES
# LobbyRcv_FinalMatchResults' twelve skirmish values (0x005007b4-0x00500930),
# the column table 0x0061dfd0 after COUNTRY_ID.
SKIRMISH_VALUES = ("WINS", "PLAYED", "W_RATIO", "P_ALLIES", "P_AXIS", "COMPLETION", "AVG_COMPLETION",
                   "TIME", "HITS", "FIRED", "ACCURACY", "DEF_KILLS")
SKIRMISH_COLUMNS = IDENTITY + SKIRMISH_VALUES
# Every reply carries every column (see ranking_table).
ALL_COLUMNS = VERSUS_COLUMNS + tuple(c for c in SKIRMISH_VALUES if c not in bia_ladder.VALUES)
# Stored per account and map.
SKIRMISH_COUNTERS = ("played", "wins", "allies", "axis", "kills", "objectives", "survivors",
                     "completion_best", "completion_total", "best_time", "hits", "fired", "def_kills",
                     "maps_reached")
# Counters that keep a best value rather than a total.
_BEST = {"completion_best": "MAX({c}, ?)", "best_time": "MAX({c}, ?)"}


def board_statistic(board: int) -> str | None:
    """The column a mission's board ranks by; None for the overall board."""
    from .eib import DEFENSE, MAX_MAP_ID, OBJECTIVE, TIMED_ASSAULT, TOUR_OF_DUTY, mission_type
    if not 1 <= board <= MAX_MAP_ID:
        return None
    return {OBJECTIVE: "COMPLETION", TIMED_ASSAULT: "TIME", DEFENSE: "DEF_KILLS",
            TOUR_OF_DUTY: "COMPLETION"}[mission_type(board)]


def skirmish_increments(values: list[int]) -> dict[str, int]:
    """Counter changes for one player's eleven skirmish values."""
    status, map_id, side, kills, objectives, survivors, seconds, hits, fired, def_kills, maps = values
    # The host reports OBJECTIVES as -1 (retail run 7): none counted.
    objectives = max(objectives, 0)
    completion = maps or objectives
    # WIN_STATUS 1 marks a completed round, won or lost (runs 7-9).
    won = status == 1
    return {
        "played": 1, "wins": int(won), "allies": int(side == 0), "axis": int(side != 0),
        "kills": kills, "objectives": objectives, "survivors": survivors,
        "completion_best": completion, "completion_total": completion,
        "best_time": seconds,
        "hits": hits, "fired": fired, "def_kills": def_kills, "maps_reached": maps,
    }


def skirmish_values(totals: dict[str, int]) -> list[int]:
    """The twelve skirmish values in SKIRMISH_VALUES order."""
    played, fired = totals["played"], totals["fired"]
    # WINS and W_RATIO stay 0: no win can be told apart (see above).
    return [0, played, 0,
            totals["allies"], totals["axis"], totals["completion_best"],
            totals["completion_total"] // played if played else 0, totals["best_time"],
            totals["hits"], fired, totals["hits"] * 100 // fired if fired else 0, totals["def_kills"]]


class EIBLadder(BIALadder):
    TABLE = "eib_ladder"

    def __init__(self, path: Path):
        super().__init__(path)
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS eib_skirmish (name_key TEXT NOT NULL REFERENCES accounts(name_key), "
                "map_id INTEGER NOT NULL, " + ", ".join(f"{name} INTEGER NOT NULL" for name in SKIRMISH_COUNTERS)
                + ", updated_at INTEGER NOT NULL, PRIMARY KEY (name_key, map_id))")
            db.commit()

    def provisional(self, entries: list, mode: str | None):
        # Validated only; the ladder changes when the round ends (operation
        # 45). An unsupported shape is left to match_results_event, which
        # the router wraps to log the shape.
        from .base import UnsupportedOperation
        from .eib import summarize
        try:
            summarize(entries)
        except UnsupportedOperation:
            pass
        return None

    def record(self, entries: list, mode: str | None) -> dict[str, list[int]]:
        """Add a finished match; returns each account's final values.

        `mode` is the operation-17 value the match started with: the game
        mode, which names the skirmish board.
        """
        from .eib import MAX_MAP_ID, VERSUS, summarize
        kind, players = summarize(entries)
        if kind == VERSUS:
            return self.add_versus(players)
        board = None
        if isinstance(mode, str) and mode.isascii() and mode.isdecimal() and 1 <= int(mode) <= MAX_MAP_ID:
            board = int(mode)
        return self.add_skirmish(players, board)

    def add_skirmish(self, players: list[tuple[str, list[int]]],
                     board: int | None = None) -> dict[str, list[int]]:
        """Add validated skirmish results to a game mode's board (MAP_ID
        without one); returns each account's values on that board."""
        now = int(time.time())
        assignments = ", ".join(f"{c} = " + (_BEST[c].format(c=c) if c in _BEST else f"{c} + ?")
                                for c in SKIRMISH_COUNTERS)
        result = {}
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for name, values in players:
                    key, map_id = name.casefold(), board if board is not None else values[1]
                    if db.execute("SELECT 1 FROM accounts WHERE name_key = ?", (key,)).fetchone() is None:
                        continue
                    added = skirmish_increments(values)
                    parameters = [added[counter] for counter in SKIRMISH_COUNTERS]
                    db.execute("INSERT INTO eib_skirmish VALUES (?, ?, " + ", ".join("0" for _ in SKIRMISH_COUNTERS)
                               + ", ?) ON CONFLICT(name_key, map_id) DO NOTHING", (key, map_id, now))
                    db.execute(f"UPDATE eib_skirmish SET {assignments}, updated_at = ? "
                               "WHERE name_key = ? AND map_id = ?", (*parameters, now, key, map_id))
                    row = db.execute("SELECT " + ", ".join(SKIRMISH_COUNTERS)
                                     + " FROM eib_skirmish WHERE name_key = ? AND map_id = ?",
                                     (key, map_id)).fetchone()
                    result[key] = skirmish_values(dict(zip(SKIRMISH_COUNTERS, row)))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return result

    def versus_board(self) -> list[tuple[int, str, dict[str, int]]]:
        """Ranked versus rows: (rank, display name, values by column)."""
        return [(rank, name, dict(zip(bia_ladder.VALUES, bia_ladder.final_values(dict(zip(bia_ladder.COUNTERS,
                                                                                            counters))))))
                for rank, name, *counters in self.board()]

    def skirmish_board(self, map_id: int = 0) -> list[tuple[int, str, dict[str, int]]]:
        """Ranked rows for one mission map, or every map together (0)."""
        columns = ", ".join(f"{'MAX' if c in _BEST else 'SUM'}(s.{c})" for c in SKIRMISH_COUNTERS)
        where = "WHERE s.map_id = ? " if map_id else ""
        with self._connection() as db:
            rows = db.execute(f"SELECT a.name, a.name_key, {columns} FROM eib_skirmish s "
                              f"JOIN accounts a ON a.name_key = s.name_key {where}GROUP BY a.name_key",
                              (map_id,) if map_id else ()).fetchall()
        valued = [(name, key, dict(zip(SKIRMISH_VALUES, skirmish_values(dict(zip(SKIRMISH_COUNTERS, counters))))))
                  for name, key, *counters in rows]
        stat = board_statistic(map_id)
        valued.sort(key=lambda row: ((-row[2][stat],) if stat else ()) + (-row[2]["PLAYED"], row[1]))
        return [(rank, name, values) for rank, (name, _, values) in enumerate(valued, 1)]


def ranking_table(profile, fields: list, username: str, path: Path) -> list:
    """Ranking query (operation 1) answered from the stored boards."""
    from ..proxy import unsigned
    from .eib import MAX_MAP_ID
    default = profile.ranking_table(fields, username)  # validates the query
    query = fields[1]
    kind, board_id = unsigned(query[2]), unsigned(query[3])
    selection, values = query[4]
    requested = query[5][0]
    if (kind, board_id) != (0, 0) and not (kind == 1 and board_id <= MAX_MAP_ID):
        return default
    if any(not isinstance(column, str) or column not in ALL_COLUMNS for column in requested):
        return default
    ladder = profile.match_ladder(path)
    versus = ladder.versus_board()
    skirmish = ladder.skirmish_board(board_id if kind == 1 else 0)
    versus_values = {name.casefold(): row for _, name, row in versus}
    skirmish_values = {name.casefold(): row for _, name, row in skirmish}
    if kind == 0:
        board = versus
    elif board_id == 0 and selection == "1":
        # The Adversarial My Rank asks this too: versus order, everyone.
        seen = {name.casefold() for _, name, _ in versus}
        names = [name for _, name, _ in versus] + [name for _, name, _ in skirmish if name.casefold() not in seen]
        board = [(rank, name, None) for rank, name in enumerate(names, 1)]
    else:
        board = skirmish
    # Every column, the requested ones first (see bia_ladder and above).
    columns = list(requested) + [column for column in ALL_COLUMNS if column not in requested]
    if selection == "1" and len(values) == 2 and isinstance(values[0], str) and str(values[1]).isdecimal():
        position = next((index for index, row in enumerate(board)
                         if row[1].casefold() == values[0].casefold()), None)
        if position is None:
            return default
        count = max(1, min(int(values[1]), 100))
        start = max(0, min(position - count // 2, len(board) - count))
    elif selection == "3" and len(values) == 2 and all(str(value).isdecimal() for value in values):
        start, count = int(values[0]), max(0, min(int(values[1]), 100))
    else:
        return default
    rows = []
    for offset, (rank, name, _) in enumerate(board[start:start + count]):
        key = name.casefold()
        own, other = ((versus_values, skirmish_values) if kind == 0 else (skirmish_values, versus_values))
        cells = dict.fromkeys(ALL_COLUMNS, 0) | other.get(key, {}) | own.get(key, {})
        cells |= {"QUERY_RANK": start + offset + 1, "GLOBAL_RANK": rank, "ALIAS": name,
                  "COUNTRY": 0, "COUNTRY_ID": 0}
        rows.append([str(cells[column]) for column in columns])
    if not rows:
        return default
    return ["1", [query[0], str(len(board)), [[column, column] for column in columns], rows]]
