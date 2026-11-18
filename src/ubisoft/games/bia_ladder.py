"""Brothers in Arms ladder: stored per-account totals and a Tubisoft SCORE.

Ubisoft's server computed SCORE; its formula is not in the game. SCORE here
is Tubisoft's own, chosen by the project owner: 3 points for a Ladder win and
1 for any other Ladder match. Friendly matches add to the totals but not to
SCORE. See docs/brothers-in-arms-network.md.

Operation 30 values come from SubmitMatchResults (0x00524570): status (1 win,
-1 loss, 0), kills, deaths, shots fired, hits, time, team and match type.
Retail run 8 showed match type 0 for a Friendly match, and team 1 for the
player on the Axis side (so team 0 is the Allies); type 1 is taken to be
Ladder.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time
from pathlib import Path

from ..accounts import initialize_database

FRIENDLY, LADDER = 0, 1
ALLIES = 0
WIN_POINTS, PLAYED_POINTS = 3, 1

# The final statistics' thirteen values (LobbyRcv_FinalMatchResults
# 0x0052dd40), the ladder columns after COUNTRY_ID in table 0x0065fe40.
VALUES = ("SCORE", "KILLS", "DEATHS", "FIRED", "HITS", "ACCURACY", "RANKED_RNDS", "RANKED_WINS",
          "FRIEND_RNDS", "FRIEND_WINS", "ALLY_MATCHES", "AXIS_MATCHES", "PLAY_TIME")
# Stored counters; ACCURACY is derived from hits and shots fired.
COUNTERS = ("score", "kills", "deaths", "fired", "hits", "ranked_rnds", "ranked_wins",
            "friend_rnds", "friend_wins", "ally_matches", "axis_matches", "play_time")
# Columns a ranking reply can carry: every cell but ALIAS is an integer.
COLUMNS = ("QUERY_RANK", "GLOBAL_RANK", "ALIAS", "COUNTRY_ID") + VALUES


def increments(values: list[int]) -> dict[str, int]:
    """Counter increments for one player's eight operation 30 values."""
    status, kills, deaths, fired, hits, seconds, team, match_type = values
    won = status == 1
    return {
        "score": (WIN_POINTS if won else PLAYED_POINTS) if match_type == LADDER else 0,
        "kills": kills, "deaths": deaths, "fired": fired, "hits": hits,
        "ranked_rnds": int(match_type == LADDER), "ranked_wins": int(match_type == LADDER and won),
        "friend_rnds": int(match_type == FRIENDLY), "friend_wins": int(match_type == FRIENDLY and won),
        "ally_matches": int(team == ALLIES), "axis_matches": int(team != ALLIES),
        "play_time": seconds,
    }


def final_values(totals: dict[str, int]) -> list[int]:
    """The thirteen final-statistics values in VALUES order."""
    accuracy = totals["hits"] * 100 // totals["fired"] if totals["fired"] else 0
    stats = {name.upper(): value for name, value in totals.items()}
    stats["ACCURACY"] = accuracy
    return [stats[name] for name in VALUES]


class BIALadder:
    # Earned in Blood keeps the same versus totals in its own table.
    TABLE = "bia_ladder"

    def __init__(self, path: Path):
        self.path = Path(path)
        initialize_database(self.path)
        with self._connection() as db:
            db.execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} (name_key TEXT PRIMARY KEY REFERENCES accounts(name_key), "
                + ", ".join(f"{name} INTEGER NOT NULL" for name in COUNTERS)
                + ", matches INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
            db.execute(f"CREATE INDEX IF NOT EXISTS {self.TABLE}_rank ON {self.TABLE}(score)")
            db.commit()

    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return closing(db)

    def provisional(self, entries: list, mode: str | None):
        # Results are applied once, at the end of the round (operation 45);
        # None tells the profile to acknowledge operation 30 only.
        from .bia import summarize
        summarize(entries)
        return None

    def record(self, entries: list, mode: str | None) -> dict[str, list[int]]:
        """Add a finished match; returns each account's final values."""
        from .bia import summarize
        return self.add_versus(summarize(entries))

    def add_versus(self, players: list[tuple[str, list[int]]]) -> dict[str, list[int]]:
        """Add validated versus results (eight values per player)."""
        now = int(time.time())
        result = {}
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for name, values in players:
                    key = name.casefold()
                    if db.execute("SELECT 1 FROM accounts WHERE name_key = ?", (key,)).fetchone() is None:
                        continue
                    added = increments(values)
                    db.execute(f"INSERT INTO {self.TABLE} VALUES (?, " + ", ".join("0" for _ in COUNTERS)
                               + ", 0, ?) ON CONFLICT(name_key) DO NOTHING", (key, now))
                    db.execute(f"UPDATE {self.TABLE} SET " + ", ".join(f"{c} = {c} + ?" for c in COUNTERS)
                               + ", matches = matches + 1, updated_at = ? WHERE name_key = ?",
                               (*(added[c] for c in COUNTERS), now, key))
                    row = db.execute("SELECT " + ", ".join(COUNTERS) + f" FROM {self.TABLE} WHERE name_key = ?",
                                     (key,)).fetchone()
                    result[key] = final_values(dict(zip(COUNTERS, row)))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return result

    def board(self) -> list[tuple]:
        """Ranked rows: (rank, display name, counters...)."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT a.name, " + ", ".join("l." + c for c in COUNTERS)
                + f" FROM {self.TABLE} l JOIN accounts a ON a.name_key = l.name_key "
                "ORDER BY l.score DESC, l.ranked_wins DESC, l.name_key ASC").fetchall()
        return [(rank, *row) for rank, row in enumerate(rows, 1)]


def _cells(rank: int, row: tuple, position: int, columns: list[str]) -> list[str]:
    _, name, *counters = row
    values = dict(zip(VALUES, final_values(dict(zip(COUNTERS, counters)))))
    values.update({"QUERY_RANK": position, "GLOBAL_RANK": rank, "ALIAS": name, "COUNTRY_ID": 0})
    return [str(values[column]) for column in columns]


def ranking_table(profile, fields: list, username: str, path: Path) -> list:
    """Ranking query (operation 1) answered from the stored ladder."""
    default = profile.ranking_table(fields, username)  # validates the query
    query = fields[1]
    selection, values = query[4]
    requested = query[5][0]
    if any(not isinstance(column, str) or column not in COLUMNS for column in requested):
        return default
    # Every column, the requested ones first: the game's column tables
    # (0x0065fe90, 0x0065feb0) are read by name, and R6 showed that a parser
    # can read a column it did not request.
    columns = list(requested) + [column for column in COLUMNS if column not in requested]
    board = BIALadder(path).board()
    if selection == "1" and len(values) == 2 and isinstance(values[0], str) and str(values[1]).isdecimal():
        # Around a player, or one player's information (count 1); an
        # unranked player keeps the empty table.
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
    rows = [_cells(row[0], row, start + offset + 1, columns)
            for offset, row in enumerate(board[start:start + count])]
    if not rows:
        return default
    return ["1", [query[0], str(len(board)), [[column, column] for column in columns], rows]]
