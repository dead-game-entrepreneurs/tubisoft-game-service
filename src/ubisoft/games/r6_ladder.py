"""Rainbow Six 3 ladders: stored per-account statistics and a Tubisoft Elo rating.

Ubisoft's server computed RATING and LEVEL; its formula is not in the game.
The rating here is Tubisoft's own Elo variant, chosen by the project owner,
and is documented in docs/rainbow-six-3-network.md. It is not the original.

Ladder indices follow the ranking query's fourth value (0x00427770 stores it
at +0x30): sign-in asks for ladders 0-3 and for LEVEL on ladders 1-3. Ladders
1-3 are taken to be the game modes in the order of the ladder names in MP.SOZ
(SURVIVAL, TEAM SURVIVAL, SHARP SHOOTER), which is also the operation 17 mode
number. Ladder 0 combines every mode.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import sqlite3
import struct
import time
from pathlib import Path

from ..accounts import _countries_table, initialize_database

START_RATING = 1000
K_FACTOR = 32
LEVEL_STEP = 50  # rating points per level above START_RATING; levels 0-15
MODES = {"1": 1, "2": 2, "3": 3}  # operation 17 mode -> ladder index
TEAM_MODES = frozenset(("2",))
SURVIVAL_MODES = frozenset(("1", "2"))  # no respawn
# Column index table 0x0058f4c0 (getters 0x004861b0..0x004862d0).
COLUMNS = ("GLOBAL_RANK", "ALIAS", "LEVEL", "RATING", "FRAGSCORE", "KILLS", "DEATHS", "RATIO",
           "TEAMKILLS", "ROUND", "TEAMROUND", "TEAMWIN", "TEAMRATIO", "TIMEPLAYED", "COUNTRY_ID")
COUNTERS = ("frag_score", "kills", "deaths", "team_kills", "rounds", "team_rounds", "team_wins", "seconds")
# The profile screen's three parallel "P_country" lists (online menu +0x190
# names, +0x19c codes, +0x1a8 IDs, read from retail RAM): operation 0x62
# stores the code (0x005140e0), while ladder rows carry the numeric
# COUNTRY_ID, which 0x00513eb0 maps back to a name. "nll" is "No Limit".
COUNTRY_IDS = {
    "ad": 120, "ae": 307, "af": 41, "ag": 124, "ai": 122, "al": 48, "am": 126, "an": 172,
    "ao": 121, "aq": 123, "ar": 125, "as": 119, "at": 129, "au": 128, "aw": 127, "az": 130,
    "ba": 142, "bb": 134, "bd": 133, "be": 136, "bf": 149, "bg": 148, "bh": 132, "bi": 150,
    "bj": 138, "bm": 139, "bn": 147, "bo": 141, "br": 145, "bs": 131, "bt": 140, "bv": 144,
    "bw": 143, "by": 135, "bz": 137, "ca": 153, "cc": 161, "cf": 156, "cg": 164, "ch": 288,
    "ci": 114, "ck": 165, "cl": 158, "cm": 152, "cn": 159, "co": 162, "cr": 166, "cu": 169,
    "cv": 154, "cx": 160, "cy": 170, "cz": 171, "de": 35, "dj": 174, "dk": 173, "dm": 175,
    "do": 176, "dz": 50, "ec": 178, "ee": 183, "eg": 179, "eh": 210, "er": 182, "es": 276,
    "et": 184, "fi": 188, "fj": 187, "fk": 185, "fm": 224, "fo": 186, "fr": 189, "fx": 189,
    "ga": 193, "gb": 11, "gd": 87, "ge": 31, "gf": 190, "gh": 36, "gi": 39, "gl": 72, "gm": 29,
    "gn": 91, "gp": 88, "gq": 181, "gr": 51, "gs": 275, "gt": 90, "gu": 89, "gw": 98, "gy": 99,
    "hk": 104, "hm": 102, "hn": 103, "hr": 168, "ht": 101, "hu": 105, "id": 108, "ie": 111,
    "il": 112, "in": 107, "io": 146, "iq": 110, "ir": 109, "is": 106, "it": 113, "jm": 115,
    "jo": 118, "jp": 117, "ke": 195, "kg": 199, "kh": 151, "ki": 196, "km": 163, "kn": 279,
    "kp": 242, "kr": 197, "kw": 198, "ky": 155, "kz": 194, "la": 200, "lb": 202, "lc": 280,
    "li": 206, "lk": 277, "lr": 204, "ls": 203, "lt": 208, "lu": 209, "lv": 201, "ly": 205,
    "ma": 229, "mc": 226, "md": 225, "mg": 213, "mh": 219, "mk": 212, "ml": 217, "mm": 231,
    "mn": 227, "mo": 211, "mp": 243, "mq": 220, "mr": 167, "ms": 228, "mt": 218, "mu": 221,
    "mv": 216, "mw": 214, "mx": 223, "my": 215, "mz": 230, "na": 232, "nc": 207, "ne": 238,
    "nf": 241, "ng": 239, "ni": 237, "nl": 235, "nll": 0, "no": 244, "np": 234, "nr": 233,
    "nu": 240, "nz": 236, "om": 245, "pa": 248, "pe": 251, "pf": 191, "pg": 249, "ph": 252,
    "pk": 246, "pl": 254, "pm": 281, "pn": 253, "pr": 256, "pt": 255, "pw": 247, "py": 250,
    "qa": 257, "re": 258, "ro": 259, "ru": 260, "rw": 261, "sa": 265, "sb": 272, "sc": 267,
    "sd": 283, "se": 287, "sg": 269, "sh": 278, "si": 271, "sj": 285, "sk": 270, "sl": 268,
    "sm": 263, "sn": 266, "so": 273, "sr": 284, "st": 264, "sv": 180, "sy": 291, "sz": 286,
    "tc": 303, "td": 157, "tf": 189, "tg": 296, "th": 295, "tj": 293, "tk": 297, "tm": 302,
    "tn": 300, "to": 298, "tp": 177, "tr": 301, "tt": 299, "tv": 304, "tw": 292, "tz": 294,
    "ua": 306, "ug": 305, "uk": 308, "um": 310, "us": 309, "uy": 311, "uz": 312, "va": 314,
    "vc": 282, "ve": 315, "vg": 318, "vn": 316, "vu": 313, "wf": 319, "ws": 262, "ye": 320,
    "yt": 222, "yu": 321, "za": 274, "zm": 322, "zr": 289, "zw": 323,
}
# A ranking filter is a 0x8c-byte binary (0x00518af0 -> 0x004863f0, sent as
# the query's third column list by 0x004379d0): a field name padded to 0x84
# bytes, then two little-endian words, operator (0 = equals) and value.
FILTER_SIZE = 0x8C
FILTER_NAME_SIZE = 0x84


def level(rating: int) -> int:
    return max(0, min(15, (rating - START_RATING) // LEVEL_STEP))


@dataclass(frozen=True)
class Summary:
    """One player's statistics for one match (operation 30 entry)."""
    name: str
    team: int
    frag_score: int
    kills: int
    deaths: int
    team_kills: int
    rounds: int
    team_rounds: int
    team_wins: int
    seconds: int


# Upper bound of each operation 30 value by field ID. The game only counts
# up (team, kills by victim level, deaths, team kills, seconds), so negative
# values are rejected; the bounds are far above any real round (61 retail
# reports never exceeded a few hundred) and keep a forged report from
# swamping the stored counters and ratings.
FIELD_LIMITS = (255,) + (9_999,) * 16 + (9_999, 9_999, 86_400)
EXTRA_FIELD_LIMIT = 2**31 - 1  # IDs past 19 are not used by the ladder


def _value(value, field_id: int) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ValueError("invalid R6 match result value")
    number = int(value)
    limit = FIELD_LIMITS[field_id] if field_id < len(FIELD_LIMITS) else EXTRA_FIELD_LIMIT
    if number > limit:
        raise ValueError("R6 match result value out of range")
    return number


def summarize(entries: list, mode: str | None) -> list[Summary]:
    # Operation 30 entries (0x00457370): name, highest field ID, field
    # count, field-ID mask and the values by field ID. Kill recorder
    # 0x004006d0 fills them: field 0 is the team; a kill of another team adds
    # one to field 1 + the victim's level (0-15) for the room's ladder, so
    # fields 1-16 are kills by victim level; field 17 counts deaths; a
    # same-team kill (a suicide included) adds a death (17) and a team kill
    # (18) to the killer. 0x00400610 sets field 19, the seconds played.
    if len(entries) > 64:
        raise ValueError("R6 match results exceed local limit")
    players = []
    for entry in entries:
        if (not isinstance(entry, list) or len(entry) != 5 or not isinstance(entry[0], str)
                or not 0 < len(entry[0].encode()) <= 32 or not isinstance(entry[4], list)
                or len(entry[4]) > 64):
            raise ValueError("unsupported R6 match result entry")
        for value in entry[1:4]:
            if not isinstance(value, str) or not value.isdecimal() or len(value) > 10:
                raise ValueError("invalid R6 match result header")
        fields = [_value(value, field_id) for field_id, value in enumerate(entry[4])] + [0] * (20 - len(entry[4]))
        players.append((entry[0], fields))
    names = [name.casefold() for name, _ in players]
    if len(set(names)) != len(names):
        raise ValueError("duplicate R6 match result player")
    team_mode = mode in TEAM_MODES
    # Survival modes have no respawn: a team with a player who did not die is
    # still standing, and it wins when it is the only one.
    standing = {fields[0] for _, fields in players if fields[17] == 0}
    summaries = []
    for name, fields in players:
        kills = sum(fields[1:17])
        won = team_mode and standing == {fields[0]}
        summaries.append(Summary(name, fields[0], kills - fields[18], kills, fields[17], fields[18],
                                 0 if team_mode else 1, 1 if team_mode else 0, 1 if won else 0,
                                 fields[19]))
    return summaries


def _outcome(mine: Summary, other: Summary, mode: str | None) -> float | None:
    """Elo score of one pairing: 1 win, 0.5 draw, 0 loss, None not compared."""
    if mode in TEAM_MODES:
        if mine.team == other.team:
            return None
        if mine.team_wins != other.team_wins:
            return 1.0 if mine.team_wins else 0.0
        return 0.5
    if mode in SURVIVAL_MODES:
        a, b = (mine.deaths == 0, mine.frag_score), (other.deaths == 0, other.frag_score)
    else:
        a, b = mine.frag_score, other.frag_score
    return 1.0 if a > b else 0.0 if a < b else 0.5


def elo_changes(players: list[Summary], ratings: dict[str, int], mode: str | None) -> dict[str, int]:
    """Rating change per player key (case-folded name).

    Every pair of rated players is an Elo game; a player's change is
    K x (sum of score - expected score) / number of opponents, rounded.
    Sharpshooter compares frag scores (kills - team kills); Survival first
    rewards surviving, then frag score; Team Survival compares only players
    on different teams by the round's winning team.
    """
    changes = {}
    for mine in players:
        key = mine.name.casefold()
        if key not in ratings:
            continue
        total, opponents = 0.0, 0
        for other in players:
            other_key = other.name.casefold()
            if other_key == key or other_key not in ratings:
                continue
            score = _outcome(mine, other, mode)
            if score is None:
                continue
            expected = 1 / (1 + 10 ** ((ratings[other_key] - ratings[key]) / 400))
            total += score - expected
            opponents += 1
        changes[key] = round(K_FACTOR * total / opponents) if opponents else 0
    return changes


class R6Ladder:
    def __init__(self, path: Path):
        self.path = Path(path)
        initialize_database(self.path)
        with self._connection() as db:
            _countries_table(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS r6_ladder (name_key TEXT NOT NULL REFERENCES accounts(name_key), "
                "ladder INTEGER NOT NULL, rating INTEGER NOT NULL, frag_score INTEGER NOT NULL, "
                "kills INTEGER NOT NULL, deaths INTEGER NOT NULL, team_kills INTEGER NOT NULL, "
                "rounds INTEGER NOT NULL, team_rounds INTEGER NOT NULL, team_wins INTEGER NOT NULL, "
                "seconds INTEGER NOT NULL, matches INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
                "PRIMARY KEY(name_key, ladder))")
            db.execute("CREATE INDEX IF NOT EXISTS r6_ladder_rank ON r6_ladder(ladder, rating)")
            db.commit()

    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        return closing(db)

    def _ratings(self, db, ladder: int, players: list[Summary]) -> dict[str, int]:
        """Stored or starting rating for every player who has an account."""
        ratings = {}
        for player in players:
            key = player.name.casefold()
            if db.execute("SELECT 1 FROM accounts WHERE name_key = ?", (key,)).fetchone() is None:
                continue
            row = db.execute("SELECT rating FROM r6_ladder WHERE name_key = ? AND ladder = ?",
                             (key, ladder)).fetchone()
            ratings[key] = row[0] if row else START_RATING
        return ratings

    def provisional(self, entries: list, mode: str | None) -> dict[str, tuple[int, int]]:
        """Rating change so far for the mode's ladder; no level change yet."""
        players = summarize(entries, mode)
        if mode not in MODES:
            return {}
        with self._connection() as db:
            ratings = self._ratings(db, MODES[mode], players)
        return {key: (change, 0) for key, change in elo_changes(players, ratings, mode).items()}

    def record(self, entries: list, mode: str | None) -> dict[str, tuple[int, int]]:
        """Apply a finished match to its mode ladder and to ladder 0.

        Returns (rating change, level change) on the mode's ladder per key.
        """
        players = summarize(entries, mode)
        if mode not in MODES:
            return {}
        result = {}
        now = int(time.time())
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for ladder in (MODES[mode], 0):
                    ratings = self._ratings(db, ladder, players)
                    changes = elo_changes(players, ratings, mode)
                    for player in players:
                        key = player.name.casefold()
                        if key not in ratings:
                            continue
                        rating = ratings[key] + changes[key]
                        db.execute(
                            "INSERT INTO r6_ladder VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, ?) "
                            "ON CONFLICT(name_key, ladder) DO NOTHING", (key, ladder, START_RATING, now))
                        db.execute(
                            "UPDATE r6_ladder SET rating = ?, "
                            + ", ".join(f"{name} = {name} + ?" for name in COUNTERS)
                            + ", matches = matches + 1, updated_at = ? WHERE name_key = ? AND ladder = ?",
                            (rating, *(getattr(player, name) for name in COUNTERS), now, key, ladder))
                        if ladder == MODES[mode]:
                            result[key] = (changes[key], level(rating) - level(ratings[key]))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return result

    def board(self, ladder: int) -> list[tuple]:
        """Ranked rows: (rank, display name, rating, counters..., country ID)."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT a.name, l.rating, " + ", ".join("l." + name for name in COUNTERS)
                + ", COALESCE(c.country, '') FROM r6_ladder l JOIN accounts a ON a.name_key = l.name_key "
                "LEFT JOIN countries c ON c.name_key = l.name_key WHERE l.ladder = ? "
                "ORDER BY l.rating DESC, l.kills DESC, l.name_key ASC", (ladder,)).fetchall()
        return [(rank, *row[:-1], country_id(row[-1])) for rank, row in enumerate(rows, 1)]


def country_id(code: str) -> int:
    """Numeric COUNTRY_ID for a stored profile country code; 0 is No Limit."""
    return COUNTRY_IDS.get(code.casefold(), 0) if isinstance(code, str) else 0


def ranking_filters(filters) -> list[tuple[str, int, int]]:
    """Decode the query's filter list as (field, operator, value)."""
    decoded = []
    for item in filters if isinstance(filters, list) else ():
        if not isinstance(item, bytes) or len(item) != FILTER_SIZE:
            raise ValueError("invalid R6 ranking filter")
        name = item[:FILTER_NAME_SIZE].split(b"\0", 1)[0].decode("ascii", "replace")
        operator, value = struct.unpack_from("<ii", item, FILTER_NAME_SIZE)
        decoded.append((name, operator, value))
    return decoded


def _cells(row: tuple | None, alias: str, columns: list[str]) -> list[str]:
    if row is None:
        # No finished match yet: unranked, as before stored ladders existed.
        values = dict.fromkeys(COLUMNS, "0")
        values["ALIAS"] = alias
        return [values[column] for column in columns]
    rank, name, rating, *counters, country = row
    stats = dict(zip(COUNTERS, counters))
    # Every cell but ALIAS must be a whole integer: the int getter (0x00427530
    # -> 0x0043c650, strtol) throws CLadderResults::NonNumericValue otherwise,
    # and My Rank (0x004849b0) reads every column as an integer. Ratios are
    # therefore sent x100, rounded down (retail run 21: "1.50" left it blank).
    ratio = stats["kills"] * 100 // stats["deaths"] if stats["deaths"] else stats["kills"] * 100
    team_ratio = stats["team_wins"] * 100 // stats["team_rounds"] if stats["team_rounds"] else 0
    values = {
        "GLOBAL_RANK": rank, "ALIAS": name, "LEVEL": level(rating), "RATING": rating,
        "FRAGSCORE": stats["frag_score"], "KILLS": stats["kills"], "DEATHS": stats["deaths"],
        "RATIO": ratio, "TEAMKILLS": stats["team_kills"], "ROUND": stats["rounds"],
        "TEAMROUND": stats["team_rounds"], "TEAMWIN": stats["team_wins"],
        "TEAMRATIO": team_ratio, "TIMEPLAYED": stats["seconds"], "COUNTRY_ID": country,
    }
    return [str(values[column]) for column in columns]


def ranking_table(profile, fields: list, username: str, path: Path) -> list:
    """Ranking query (operation 1) answered from the stored ladders."""
    # The title adapter validates the query and supplies the empty forms.
    default = profile.ranking_table(fields, username)
    query = fields[1]
    selection, values = query[4]
    # Every column is returned, the requested ones first. The My Rank
    # (type 1) and ladder browsing (type 3) screens send no column names
    # (retail run 20), and top-list parser 0x00485410 also reads COUNTRY_ID
    # (0x00486210), which the sign-in query (0x00485db0) does not request; a
    # missing column fails the parse.
    requested = query[5][0]
    if (not query[3].isdecimal() or int(query[3]) > 3
            or any(not isinstance(column, str) or column not in COLUMNS for column in requested)):
        return default
    columns = list(requested) + [column for column in COLUMNS if column not in requested]
    try:
        filters = ranking_filters(query[5][2])
    except ValueError:
        return default
    board = R6Ladder(path).board(int(query[3]))
    by_key = {row[1].casefold(): row for row in board}
    if selection == "2":
        # Sign-in parser 0x00485250 needs a row for each requested alias.
        rows = [_cells(by_key.get(alias.casefold()), alias, columns) for alias in values]
        return ["1", [query[0], str(len(rows)), [[column, column] for column in columns], rows]]
    # The ladder screen's country choice arrives as COUNTRY_ID = ID (operator
    # 0); "No Limit" sends no filter. GLOBAL_RANK stays the overall position.
    for name, operator, value in filters:
        if name == "COUNTRY_ID" and operator == 0 and value != 0:
            board = [row for row in board if row[-1] == value]
    if selection == "1" and len(values) == 2 and isinstance(values[0], str) and str(values[1]).isdecimal():
        # Rows around the player; an unranked player keeps the empty table.
        position = next((index for index, row in enumerate(board)
                         if row[1].casefold() == values[0].casefold()), None)
        if position is None:
            return default
        count = max(1, min(int(values[1]), 100))
        start = max(0, min(position - count // 2, len(board) - count))
        rows = [_cells(item, item[1], columns) for item in board[start:start + count]]
    elif selection == "3" and len(values) == 2 and all(str(value).isdecimal() for value in values):
        start, count = int(values[0]), max(0, min(int(values[1]), 100))
        rows = [_cells(item, item[1], columns) for item in board[start:start + count]]
    else:
        return default
    if not rows:
        # Keep the empty form the sign-in stage already accepts.
        return default
    return ["1", [query[0], str(len(board)), [[column, column] for column in columns], rows]]
