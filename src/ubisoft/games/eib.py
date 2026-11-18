"""Brothers in Arms: Earned in Blood (PS2): Road to Hill 30's profile plus skirmish.

Addresses refer to the multiplayer executable ``MP.SOZ`` of ``SLUS-21310``.
Like Road to Hill 30's it is a plain ELF: one loadable segment at file
offset 0x100, linked at ``0x00100000``, with GP ``0x006b5f70``. See
docs/brothers-in-arms-earned-in-blood-network.md for the evidence.

Earned in Blood keeps Road to Hill 30's GS library and ``BIAGLClient``
wrapper. Its lobby reply dispatcher (0x004bef80) routes the same 42
operations, the lobby-callback vtable (0x006a9f7c) keeps the slot layout,
and the readers have the same field lists. So the profile subclasses
``BIAProfile`` and inherits its hooks. Their comments cite Road to Hill 30
addresses; the Earned in Blood counterparts are listed in the network
notes. This profile adds the identity, its own port block, two parent
lobbies (one per lobby type), skirmish match results and a separate ladder.
Lobby invitations travel through the chat (IRC) server in ``ubisoft.irc``;
the profile gives each player a chat ID. The room master push (operation
59) is not implemented yet.
"""
import base64
import hashlib

from .base import UnsupportedOperation
from .bia import RESULT_LIMITS, BIAProfile, _decimal, summarize as _summarize

VERSUS, SKIRMISH = "versus", "skirmish"

# SubmitMatchResults (0x004f4af0) has a versus branch, which sends Road to
# Hill 30's eight values (field IDs 0-7), and a skirmish branch, which sends
# field IDs 0-10 through LobbySend_SetMatchResult (0x004dade0). The skirmish
# branch always sends all eleven; values the mission type does not use are
# sent as 0. Bounds by field ID. Retail run 7 (two Ladder Objective
# matches on map 1, both won) sent [1, 1, 0, 2, -1, 4, 0, ...] for the
# host and [1, 1, 0, 0, 0, 2, 0, ...] for the guest: the host's OBJECTIVES
# was -1 both times, which runs 5 and 6 refused. The ladder counts it as 0.
# SetMapID (0x004f48b0) stores the scripts' number unchecked at result
# entry +0x68, so MAP_ID and WIN_STATUS keep wide bounds. Both players send
# WIN_STATUS 1 for every finished round, won or lost (runs 7-9).
SKIRMISH_LIMITS = (
    (-1, 9),       # 0 WIN_STATUS_SKR: 1 for a completed round
    (0, 255),      # 1 MAP_ID_SKR, set by the scripts (SetMapID)
    (0, 255),      # 2 SIDE_SKR: 0 Allies, as TEAM in versus
    (0, 9_999),    # 3 KILLS_SKR
    (-1, 999),     # 4 OBJECTIVES_SKR (-1 for the host, run 7)
    (0, 99),       # 5 SURVIVORS_SKR
    (0, 86_400),   # 6 TIME_SKR: seconds survived (run 9)
    (0, 999_999),  # 7 HITS_SKR
    (0, 999_999),  # 8 FIRED_SKR
    (0, 9_999),    # 9 DEF_KILLS_SKR
    (0, 999),      # 10 MAPS_REACHED_SKR
)

# Mission types by (game mode - 1) // 10 (subsystem +0x4e8, set by the
# scripts' SetGameMode), the order SubmitMatchResults and the ladder request
# (0x004fb3d0) branch in. Objective sends fields 3-5, Timed Assault 6,
# Defense 7-9, and Tour of Duty 3-5 and 10. The game mode is not part of
# the results, and MAP_ID need not follow it.
OBJECTIVE, TIMED_ASSAULT, DEFENSE, TOUR_OF_DUTY = range(4)
# Ladder boards: (1, game mode) for one mission, game modes 1-40.
MAX_MAP_ID = 40


# Parent lobbies by group ID: name and lobby type. NewLobby (0x004fdb80)
# reads the type from record field 11 into the lobby entry (+0x30, through
# 0x004f9600), and the menus read it back (GetLobbyType, GetCurrentLobbyType
# 0x004f79d0). Retail run 2 listed a type-0 lobby as Adversarial; Skirmish
# as type 1 is Pending (the menus' use of the type is in UnrealScript).
LOBBIES = {"1": ("Earned in Blood", "0"), "2": ("Earned in Blood Skirmish", "1")}


def mission_type(game_mode: int) -> int:
    return (game_mode - 1) // 10


def summarize(entries: list) -> tuple[str, list[tuple[str, list[int]]]]:
    """Validate operation 30 entries: (VERSUS or SKIRMISH, (name, values) per player)."""
    if not isinstance(entries, list):
        raise ValueError("EiB match results are not a list")
    lengths = {len(entry[4]) for entry in entries
               if isinstance(entry, list) and len(entry) == 5 and isinstance(entry[4], list)}
    if lengths <= {len(RESULT_LIMITS)}:
        return VERSUS, _summarize(entries)
    if lengths == {len(SKIRMISH_LIMITS)}:
        players = _summarize(entries, SKIRMISH_LIMITS)
        if len({values[1] for _, values in players}) > 1:
            raise ValueError("EiB skirmish results name more than one map")
        return SKIRMISH, players
    # The router logs the shape of an unsupported report.
    raise UnsupportedOperation("unsupported EiB match result shape")


class EIBProfile(BIAProfile):
    _ladders: dict = {}  # stored ladders by account database (class-level)

    def lobby_ids(self) -> tuple[str, ...]:
        return tuple(LOBBIES)

    def account_failure(self, message_type: int, reason: str) -> list | None:
        # GSFAIL with a four-byte reason, read by the shared reply reader
        # (0x004bfd10). The account-creation callback (0x004fc2c0) shows
        # reason 1 "account name exists", 2 and 3 "name not allowed" and 11
        # "password not allowed". Before this a taken name got no reply and
        # the game waited (retail run 1); run 15 showed the message and a
        # return to the account creation screen.
        code = {"duplicate": 1, "username": 3, "password": 11}.get(reason) if message_type == 1 else None
        return None if code is None else [bytes([message_type]), [code.to_bytes(4, "little")]]

    def player_info_id(self, name: str) -> str:
        # LoginRcv_PlayerInfo (0x004fba00) copies field 0, the IRCID, with
        # strncpy into 10 bytes: the player's own at +0x20c, which becomes
        # its IRC nick, and a looked-up friend's at +0x217, the target of
        # an invitation. A name can be longer, so each account gets a short
        # stable ID: "p" and eight base-32 characters of its name's hash.
        digest = hashlib.sha256(name.casefold().encode()).digest()
        return "p" + base64.b32encode(digest[:5]).decode().lower()

    def group_refresh_flags(self, values: list) -> str:
        # Operation 53's handler (0x004d0cc0) reads the group's own record
        # (index 2) only with flag 0x40: a record whose field 0 is 0 is a
        # lobby, passed to lobby-callback slot +0x2c (0x004fe060), which
        # updates the listed lobby's player count, limit and type in place.
        # Flag 0x100 lists the children (index 3). With "256" alone the
        # lobby list kept the counts from sign-in (retail run 10: a player
        # who had left was still counted). Echo the request's 0x40 and
        # 0x100; 0x80 (members) is answered with operation 50 instead.
        flags = _decimal(values[1], 0xffffffff) if len(values) > 1 else 0x100
        return str(flags & 0x140 or 0x100)

    def lobby_record(self, lobby: str, players: int) -> list:
        # Road to Hill 30's 14-field record: field 1 is the name, field 2
        # the group ID, field 3 the lobby server and field 11 the type.
        name, lobby_type = LOBBIES[lobby]
        record = self.parent_lobby(players)
        record[1], record[2], record[11] = name, lobby, lobby_type
        return record

    def match_ladder(self, account_db):
        from .eib_ladder import EIBLadder
        if account_db not in EIBProfile._ladders:
            EIBProfile._ladders[account_db] = EIBLadder(account_db)
        return EIBProfile._ladders[account_db]

    def ladder_ranking_table(self, fields: list, username: str, account_db) -> list:
        from .eib_ladder import ranking_table
        return ranking_table(self, fields, username, account_db)

    def match_results_event(self, match_id: str, entries: list, mode: str | None = None,
                            changes: dict | None = None) -> list | None:
        # Operation 30 entries keep Road to Hill 30's form (name, highest
        # field ID, field count, mask, values by field ID) with eight versus
        # or eleven skirmish values. Operation 71 (LobbyRcv_FinalMatchResults
        # 0x00500440, lobby-callback slot +0xb4) reads thirteen values per
        # player when the game mode (subsystem +0x4e8) is 0, as in Road to
        # Hill 30, and twelve skirmish values otherwise (the mode is then the
        # mission map). Other counts are logged as unknown and ignored.
        from .bia_ladder import COUNTERS, final_values
        from .eib_ladder import SKIRMISH_VALUES
        _decimal(match_id, 0xffffffff)
        kind, players = summarize(entries)
        if changes is None:
            return None
        empty = (final_values(dict.fromkeys(COUNTERS, 0)) if kind == VERSUS
                 else [0] * len(SKIRMISH_VALUES))
        records = [[name, [str(value) for value in changes.get(name.casefold(), empty)]]
                   for name, _ in players]
        return ["71", [match_id, "38", records]]


PROFILE = EIBProfile(
    slug="eib", product="BIAEIBPS2US", display_name="Earned in Blood",
    # Pointer table 0x0061df50 (Road to Hill 30's 0x0065fe10 layout): host,
    # request format, product (0x006642e8), SDK version (0x006642f8) and
    # game version (0x00664308). Friend::UpdateFriend (0x004f9b40) compares
    # a friend's product with the product slot (0x0061df60).
    game_version="1.0", sdk_version="BIAEIBPS2US1.0", default_ports=(30050, 30051, 30052),
    supports_discovery=True, lobby_members=True,
    supports_rooms=True, lobby_create=True, owner_game_started=False, game_started=False,
    # JoinFriend (0x004f0e80) keeps Road to Hill 30's thresholds: 6 or more
    # to join a game (0x004f0fb8) and 5 or more for a lobby (0x004f1004).
    negative_room_ids=True, friend_joining=True,
    creator_room_listing=True, self_member_event=True,
    # CNatService::CBRequestFinished (0x005d8610) prints the NAT server's
    # address word byte 3 first, as do PERMIT PING (0x005d82d0) and
    # CreateRoom's "Set room nat info" (0x004eea10), which copies the word
    # to session info bytes 4-7: Road to Hill 30's order.
    nat_address_byteorder="big",
    news=("Welcome to Tubisoft, a community revival of the Brothers in Arms: Earned in Blood online service.",
          "Earned in Blood online play on Tubisoft is still in development."),
    proxy_services=("ladderquery",),
    # The [Servers] keys are Road to Hill 30's (0x0061dda0-0x0061de30), plus
    # the chat server's: reader 0x004bca20 reads IRCIP%d and IRCPort%d, and
    # the Tick (0x004ed060) connects to the first entry ("Connect chat
    # server: %s:%d"). Without them it never connects (retail runs 1-12).
    bootstrap_section="Servers", chat_port=30053,
    bootstrap_keys=("RouterIP0", "RouterPort0", "RouterLauncherPort0", "NATServerIP0", "NATServerPort0",
                    "IRCIP0", "IRCPort0"),
)
