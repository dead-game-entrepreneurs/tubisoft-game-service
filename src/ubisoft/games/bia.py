"""Brothers in Arms: Road to Hill 30 (PS2): sign-in, discovery and parent lobby.

Addresses refer to the multiplayer executable ``MP.SOZ`` of ``SLUS-21163``.
Unlike Rainbow Six 3's file it is a plain ELF, not a zlib stream; its single
loadable segment is linked at ``0x00100000`` and ``.reginfo`` gives GP
``0x006f4370``. See docs/brothers-in-arms-network.md for the evidence.

The game wraps the GS client library in its own ``BIAGLClient`` class, but
the library's message senders and reply readers have the same shapes as
Rainbow Six 3's. The profile is experimental and opt-in. It covers sign-in, the
parent lobby and its member directory (retail runs 1-3), and creating,
listing, joining and starting player-hosted matches (static; retail run 4
showed the creation request). Match results and account changes are not
implemented yet.
"""
import ipaddress

from .base import GameProfile, RoomSpec

# Notice request 0x0051cd40 picks one of these codes from the language
# setting (subsystem +0x74) before calling the 0xde sender 0x004f0f60.
LANGUAGES = frozenset(("en", "fr", "de", "it", "es"))

# LobbyRcv_NewRoom (0x0052b370) ignores a room whose session info is not
# 0x50 bytes; 0x00527920 copies it to the room entry at +0x24. It carries
# the map name, match type (Friendly or Ladder), friendly fire, the player
# number mode, the host's NAT address (+0x28) and port (+0x2c) and the
# locked flag (entry +0x25 bit 4, the Protected option).
SESSION_INFO_BYTES = 0x50

# JoinRoomReply (0x0052afc0) gives reason 11 its own event (0x18); reasons
# 5 and 7 share 0x19 and others fall back to 0x1b, FAILEDTOJOINROOM
# (0x0051ee18). Reason 11 for a wrong password is Pending a retail check.
WRONG_PASSWORD = "11"


def _decimal(value: str, maximum: int) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ValueError("invalid BiA numeric field")
    number = int(value)
    if number > maximum:
        raise ValueError("BiA numeric field out of range")
    return number


# Bounds for SubmitMatchResults' eight values by field ID: status, kills,
# deaths, shots fired, hits, time, team and match type.
RESULT_LIMITS = ((-1, 1), (0, 9_999), (0, 9_999), (0, 999_999), (0, 999_999), (0, 86_400), (0, 255), (0, 255))


def summarize(entries: list, limits: tuple = RESULT_LIMITS) -> list[tuple[str, list[int]]]:
    """Validate operation 30 entries; returns (name, values) per player.

    `limits` gives each value's bounds by field ID: the eight versus values
    by default (Earned in Blood passes its eleven skirmish values).
    """
    if not isinstance(entries, list) or len(entries) > 16:
        raise ValueError("BiA match results exceed local limit")
    players = []
    for entry in entries:
        if (not isinstance(entry, list) or len(entry) != 5 or not isinstance(entry[0], str)
                or not 0 < len(entry[0].encode()) <= 32 or not isinstance(entry[4], list)
                or len(entry[4]) != len(limits)):
            raise ValueError("unsupported BiA match result entry")
        if not all(isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 10
                   for value in entry[1:4]):
            raise ValueError("invalid BiA match result header")
        values = []
        for value, (low, high) in zip(entry[4], limits):
            digits = value[1:] if isinstance(value, str) and value.startswith("-") else value
            if not isinstance(digits, str) or not digits.isascii() or not digits.isdecimal() or len(digits) > 10:
                raise ValueError("invalid BiA match result value")
            number = int(value)
            if not low <= number <= high:
                raise ValueError("BiA match result value out of range")
            values.append(number)
        players.append((entry[0], values))
    names = [name.casefold() for name, _ in players]
    if len(set(names)) != len(names):
        raise ValueError("duplicate BiA match result player")
    return players


def _text(value: str, limit: int) -> bool:
    return isinstance(value, str) and len(value.encode()) <= limit and all(" " <= c <= "~" for c in value)


class BIAProfile(GameProfile):
    _ladders: dict = {}  # stored ladders by account database (class-level)

    def match_ladder(self, account_db):
        from .bia_ladder import BIALadder
        if account_db not in BIAProfile._ladders:
            BIAProfile._ladders[account_db] = BIALadder(account_db)
        return BIAProfile._ladders[account_db]

    def ladder_ranking_table(self, fields: list, username: str, account_db) -> list:
        from .bia_ladder import ranking_table
        return ranking_table(self, fields, username, account_db)

    def login_notices(self, fields: list) -> list:
        # Reader 0x004f2040 takes the nested list at field 1 and copies two
        # strings into 0x201-byte buffers (getter 0x004e9740): MOTD lines.
        if (len(fields) != 1 or not isinstance(fields[0], str)
                or fields[0] not in LANGUAGES):
            raise ValueError("unsupported BiA notice language")
        return list(self.news)

    def discovery_login(self, fields: list) -> None:
        # 0x004f7340 sends 0xd1 [21, [product]]; 21 is the GP constant at
        # 0x006ec678 and the product comes from pointer table 0x0065fe20.
        self.require_discovery()
        if fields != [self.product]:
            raise ValueError("unsupported BiA discovery product")

    LOBBY_LIMIT = 100

    def parent_lobby(self, players: int = 0) -> list:
        # Folder reader 0x004f6d80 reads 14 fields: s16 ID, name (33 bytes),
        # four u32, s16, strings of 16/132/99 bytes, binary, u32 and two s32.
        # The 20-field child reader 0x004f6aa0 has the same prefix. Both
        # match Rainbow Six 3's readers, so the record keeps R6's layout:
        # field 12 is the limit and field 13 the players in the lobby.
        # Players cannot create lobbies, so everyone shares this one; the
        # limit is shown in the lobby list (JoinLobby, 0x0051e6b0, does not
        # check it, and neither does the server). R6's placeholder 8 read as
        # a cap, so BiA advertises LOBBY_LIMIT.
        return ["0", self.display_name, "1", "1", "0", "0", "1", "", "",
                self.product, b"", "0", str(self.LOBBY_LIMIT), str(players)]

    def join_parent_lobby(self, fields: list) -> list:
        # Sender 0x004f7600 issues operation 23 through 0x00508480 with group
        # ID (number), password (string) and a numeric option, as in R6.
        from ..proxy import unsigned
        if (len(fields) != 3 or not all(isinstance(value, str) for value in fields)
                or fields[0] not in self.lobby_ids() or fields[1] != ""):
            raise ValueError("unsupported BiA parent lobby join")
        unsigned(fields[2])
        return [fields[0], ""]

    def parent_player_info(self, fields: list) -> bytes:
        # 0x00528cb0 copies 12 bytes from the game's player record (+0x68c:
        # IPv4 address, port and four more bytes) and passes them through
        # 0x0050b5d0 to 0x004f93e0, which sends operation 42 with one binary.
        if len(fields) != 1 or not isinstance(fields[0], bytes) or len(fields[0]) != 12:
            raise ValueError("invalid BiA parent player data")
        return fields[0]

    def lobby_member_event(self, member) -> list:
        # Reader 0x00502aa0 (operation 50): name, a flag character, a group
        # ID, two address strings, the member's binary data and a u16 -- the
        # shape of R6's reader 0x004237f0.
        return ["50", [member.name, "0", member.group, member.address,
                       member.local_address or member.address, member.info, "0"]]

    def lobby_member_left(self, member) -> list:
        # Reader 0x00503140 (operation 51): name (33 bytes) and group ID.
        return ["51", [member.name, member.group]]

    def registration(self, fields: list, peer_ip: str) -> RoomSpec:
        # Lobby operation 12, sender 0x004f7b90: parent ID, room name,
        # product, three numbers, session info, password, two version
        # strings and additional data (retail run 4 confirmed the types).
        # CreateRoom 0x0051e960 passes the lobby ID, the name, the product,
        # group type 5, the player limit (2 or 4 in the menu), 0 spectators,
        # 0x50 bytes of session info from +0x4f4, the password from +0x550
        # and the SDK and game versions.
        types = (str, str, str, str, str, str, bytes, str, str, str, bytes)
        if (len(fields) != len(types)
                or not all(isinstance(value, typ) for value, typ in zip(fields, types))):
            raise ValueError("unsupported BiA room creation structure")
        parent, name, product, group_type, players, spectators, info, password, first, second, extra = fields
        if parent not in self.lobby_ids() or product != self.product:
            raise ValueError("unsupported BiA room parent or product")
        if not name or not _text(name, 32) or not _text(password, 32):
            raise ValueError("invalid BiA room text field")
        if not _text(first, 32) or not _text(second, 32):
            raise ValueError("invalid BiA room version field")
        if not 1 <= _decimal(group_type, 0xffff):
            raise ValueError("invalid BiA room type")
        if not 2 <= _decimal(players, 0xffff) <= 4:
            raise ValueError("invalid BiA player limit")
        _decimal(spectators, 0xffff)
        if len(info) != SESSION_INFO_BYTES or len(extra) > 16384:
            raise ValueError("invalid BiA room data")
        return RoomSpec(name, product, group_type, parent, "0", players, spectators, info, extra,
                        "0", first, second, peer_ip, password)

    def host_endpoint(self, room, peer_ip: str) -> str:
        # Guests dial the address and port at entry +0x28/+0x2c, bytes 4-11
        # of the session info (GetCurrentRoomIPAddress 0x00526450 ->
        # 0x005279e0, GetCurrentRoomPort 0x005264f0 -> 0x00527a00). CreateRoom
        # copies them from the NAT service (0x00821940 +0x98/+0x9c).
        from ..router import endpoint_shape
        return endpoint_shape(room.info[4:10], peer_ip, self.nat_address_byteorder)

    def lobby_create_result(self, room) -> list:
        # Reader 0x004fb380 (dispatcher 0x004ef620 -> 0x00502910): room ID,
        # text and the lobby server ID. CreateRoomReply (0x0052a390) then
        # waits for the room's record and sends operation 24 itself
        # (0x0050b490) through that lobby server. Earned in Blood's
        # CreateRoomReply (0x004fc780) passes the third value to
        # LobbySend_JoinRoom (0x004c70c0), which picks the lobby server
        # connection by it (0x004d7d70); with the parent group ID "2" there,
        # the game asked for lobby server 2 (operation 3) and dropped its
        # lobby connection (Earned in Blood retail run 3). Tubisoft has one
        # lobby server, ID 1.
        return [str(room.room_id), "", "1"]

    def room_join_result(self, room) -> list:
        # Reader 0x004fa540 (0x00502980): room ID and text on success.
        return [str(room.room_id), ""]

    def room_join_failure(self, room, reason: str) -> list | None:
        # On GSFAIL the same reader starts one field later: reason, room ID,
        # text.
        if reason == "password":
            return [WRONG_PASSWORD, str(room.room_id), ""]
        return None

    def group_flags(self, flags: str) -> str:
        return flags

    def room_update(self, room, subtype: int, values: list) -> list:
        # Owner operations, senders 0x004f90b0 (15), 0x004f7f30 (17),
        # 0x004f8500 (31) and 0x004f8a60 (33); their fields match R6's
        # except 31 (see room_data_update). Replies read the room ID.
        if not isinstance(values, list) or not values or values[0] != str(room.room_id):
            raise ValueError("BiA room update does not name the owner's room")
        if subtype == 33:
            # Room ID, binary, port and address.
            if (len(values) != 4 or not isinstance(values[1], bytes) or len(values[1]) > 16384
                    or not isinstance(values[3], str)):
                raise ValueError("unsupported BiA room address update")
            port = _decimal(values[2], 0xffff)
            if port:
                room.game_port = str(port)
            try:
                local = ipaddress.IPv4Address(values[3])
                if not (local.is_unspecified or local.is_multicast or local.is_loopback
                        or local in ipaddress.IPv4Network("192.0.2.0/24")):
                    room.local_ip = str(local)
            except ValueError:
                pass
        elif subtype == 31:
            self.room_data_update(room, values[1:])
        elif subtype == 17:
            if len(values) != 2:
                raise ValueError("unsupported BiA match start")
            _decimal(values[1], 0xffffffff)
        elif subtype == 15:
            if len(values) != 1:
                raise ValueError("unsupported BiA room update")
        else:
            raise ValueError("unsupported BiA room update")
        return [str(room.room_id)]

    # Operation 31 (sender 0x004f8500): after the room ID comes a mask of
    # the fields that follow, in this order. A second mask follows when any
    # of 0x2, 0x4 or 0x200 is set.
    ROOM_DATA_FIELDS = ((0x8, "players"), (0x10, "spectators"), (0x20, "name"),
                        (0x40, "info"), (0x400, "additional"))
    ROOM_DATA_MASK = 0x2 | 0x4 | 0x200 | 0x8 | 0x10 | 0x20 | 0x40 | 0x400

    def room_data_update(self, room, values: list) -> None:
        # Retail run 8 sent every field: [room, mask, mask, players,
        # spectators, name, info, additional]. Retail run 10's Play Next
        # Game sent [room, mask, info] (mask 0x40, new session info).
        if not values:
            raise ValueError("BiA room data update has no mask")
        mask = _decimal(values[0], 0xffffffff)
        if mask & ~self.ROOM_DATA_MASK:
            raise ValueError("unsupported BiA room data mask")
        rest = list(values[1:])
        if mask & 0x206:
            if not rest:
                raise ValueError("BiA room data update is short")
            _decimal(rest.pop(0), 0xffffffff)
        update = {}
        for bit, key in self.ROOM_DATA_FIELDS:
            if not mask & bit:
                continue
            if not rest:
                raise ValueError("BiA room data update is short")
            update[key] = rest.pop(0)
        if rest:
            raise ValueError("BiA room data update is long")
        for key in ("players", "spectators"):
            if key in update:
                _decimal(update[key], 0xffff)
        if "name" in update and not _text(update["name"], 32):
            raise ValueError("invalid BiA room name")
        for key in ("info", "additional"):
            if key in update and (not isinstance(update[key], bytes) or len(update[key]) > 16384):
                raise ValueError("invalid BiA room data")
        # Keep the limit within registration's bounds, and the session info
        # only when NewRoom would accept it.
        if "players" in update and 2 <= int(update["players"]) <= 4:
            room.max_players = str(int(update["players"]))
        if len(update.get("info", b"")) == SESSION_INFO_BYTES:
            room.info = update["info"]

    def room_update_event(self, room, subtype: int) -> list | None:
        # ChangeMatchMap (0x0051fe80, Play Next Game) sets request 14 and
        # sends operation 31, whose reply callback (slot +0x6c, 0x0052d3c0)
        # does nothing. The map change completes when the room's record
        # arrives as operation 53 with flags 0x40 (handler 0x005013d0 ->
        # RoomInfo, 0x0052bad0): it copies the session info into the listed
        # room and, with request 14 pending, fires
        # GSEVT_MATCH_UPDATEDGAMEINFO. Without it the host re-sent 31 and
        # timed out (retail run 11). Other members' lists take the new info
        # without a rebuild, so player counts are kept.
        if subtype != 31:
            return None
        return ["53", [str(room.room_id), "64", room.group_fields()]]

    # Senders 0x004f8f00 (34), 0x004f8d50 (35), 0x004f8220 (44), 0x004f80b0
    # (45) and 0x004f8390 (70) send only the room ID. Dispatcher 0x004ef620
    # has a reader only for 45 (0x00503690). Retail run 8: each player sent
    # 44 twice after operation 62.
    room_notice_subtypes = (34, 35, 44, 45, 70)

    def room_notice(self, room, subtype: int, values: list) -> None:
        if values != [str(room.room_id)]:
            raise ValueError("BiA room report does not name the joined room")

    def room_notice_reply(self, room, subtype: int) -> list | None:
        # Reader 0x00503690 reads two numbers, which LobbyRcv_MatchFinishReply
        # logs as the group ID and the lobby server ID ("1", the ID the 0xd2
        # lobby login reply echoes).
        if subtype == 45:
            return ["38", ["45", [str(room.room_id), "1"]]]
        return None

    def match_results_event(self, match_id: str, entries: list, mode: str | None = None,
                            changes: dict | None = None) -> list | None:
        # Operation 30 from the owner at the end of a round (retail run 8):
        # [match ID, 0, entries]. Each entry is R6's form -- name, highest
        # field ID (7), field count (8), field mask (255) and the values by
        # field ID -- with SubmitMatchResults' (0x00524570) eight fields:
        # status (1 win, -1 loss, 0), kills, deaths, shots fired, hits, time,
        # team and match type. The ladder applies them once, when the owner
        # ends the round (operation 45); `changes` is then each account's
        # thirteen totals, and None (operation 30) only acknowledges.
        # Operation 71 (reader 0x00505260, callback 0x0052dd40): match ID,
        # status (GSSUCCESS, as in R6) and [name, [values]] per player.
        from .bia_ladder import COUNTERS, final_values
        _decimal(match_id, 0xffffffff)
        players = summarize(entries)
        if changes is None:
            return None
        empty = final_values(dict.fromkeys(COUNTERS, 0))
        records = [[name, [str(value) for value in changes.get(name.casefold(), empty)]]
                   for name, _ in players]
        return ["71", [match_id, "38", records]]

    def group_refresh(self, values: list) -> tuple[bool, bool]:
        # Tick's timed refresh (0x0051e048) asks with flags 0x40. NewRoom
        # (0x0052b370) replaces a listed room's entry, and the entry
        # constructor (0x00527920) starts its player count (+0x1a) at 0; only
        # member events (0x0052ca48, 0x0052ce20) change it. So the timed
        # refresh gets the lobby record only, and other requests (JoinLobby
        # Reply's 0x1c0) get the rooms followed by the members (retail run 9
        # showed 0/2 with the rooms resent every half second).
        if values[1:] == ["64"]:
            return False, False
        return True, True

    def friend_location(self, member) -> list:
        # Operation 106 reply (handler 0x00504ec0): [friend name, [lobby
        # server, ID]...], shaped like a push (no status field). For each
        # pair callback 0x0052dbd0 files a positive ID as the friend's lobby
        # (+0x4d4, server +0x4d8) and a negative one as the room (+0x4d0);
        # the end of the list completes JoinFriend (0x00520bd0). Joining a
        # game needs both: 0x00520ff0 finds the listed room with that ID and
        # server and joins it (0x0051efa0). An empty list fails the request
        # with the game's own message.
        locations = [["1", member.lobby]]
        if member.in_room:
            locations.append(["1", member.group])
        return locations

    def friend_presence(self, in_room: bool) -> tuple[int, str]:
        # Friend::UpdateFriend (0x00527ca0, table 0x006a38b0) maps the GS
        # status: 0 -> 4, 8 -> 5, and 9-11 -> 7 with this product (4
        # otherwise). JoinFriend (0x00520bd0) needs 5 or more to join a
        # friend's lobby and 6 or more to join a game, so a friend in the
        # lobby reported as 0 could not be joined (retail run 13: no
        # operation 106 was sent). The lobby is reported as 8 instead.
        return (9, self.product) if in_room else (8, self.product)

    def match_started_event(self, room, match_id: int) -> list:
        # Reader 0x004fd990 (operation 62, handler 0x00503cd0): room ID and
        # match ID, as in R6. LobbyRcv_MatchStarted ignores a duplicate.
        return ["62", [str(room.room_id), str(match_id)]]

    def ranking_table(self, fields: list, username: str) -> list:
        # The game builds queries through thunks 0x0050b620-0x0050b670:
        # product, then selection type 1 [alias, count] (0x005060c0) or
        # type 3 [start, count] (0x005061b0), a sort column (0x005061e0)
        # and a column list (0x00506230) from tables 0x0065fe90 (eight
        # columns, player information) and 0x0065feb0 (five, top list and
        # around me). The ladder is initialized at sign-in (0x00528c50 ->
        # 0x00524910) but only queried from the menus. No results are
        # stored yet, so every query gets the empty table.
        from ..proxy import unsigned
        if (len(fields) != 2 or fields[0] != "1" or not isinstance(fields[1], list)
                or len(fields[1]) != 6):
            raise ValueError("unsupported BiA ranking request")
        query = fields[1]
        unsigned(query[0])
        unsigned(query[2])
        unsigned(query[3])
        if (query[1] != self.product
                or not isinstance(query[4], list) or len(query[4]) != 2
                or unsigned(query[4][0]) > 3 or not isinstance(query[4][1], list)
                or not isinstance(query[5], list) or len(query[5]) != 3
                or not all(isinstance(part, list) for part in query[5])):
            raise ValueError("unsupported BiA ranking selection")
        return ["1", [query[0], "0", [], []]]


PROFILE = BIAProfile(
    slug="bia", product="BROTHERSINARMSPS2US", display_name="Brothers in Arms",
    # Pointer table 0x0065fe10: host, request format, product (0x0065fe20),
    # SDK version (0x0065fe28) and game version (0x0065fe30). The primary
    # login (0x004ef0c0 -> 0x004f07b0) sends the SDK version.
    game_version="1.0", sdk_version="BIAPS2US1.0", default_ports=(30030, 30031, 30032),
    supports_discovery=True, lobby_members=True,
    supports_rooms=True, lobby_create=True, owner_game_started=False, game_started=False,
    # Rooms have negative IDs, lobbies positive ones: JoinFriend's location
    # callback (0x0052dbd0) files them by sign.
    negative_room_ids=True, friend_joining=True,
    creator_room_listing=True,
    # LobbyRcv_MemberJoined (0x0052bf00) compares the member's name with the
    # player's own (+0x16b) and only then calls 0x00528cb0, which sends
    # operation 42; otherwise the join times out (0x006a4f00) and the game
    # leaves the lobby (retail run 1). Tick (0x0051d510, call 0x0051e048)
    # refreshes every listed lobby with operation 9 (flags 0x40) on a timer.
    self_member_event=True,
    # CNatService::CBRequestFinished (0x0060f230) stores the NAT server's
    # address word and prints it byte 3 first; PERMIT PING (0x0060eef0) and
    # GetCurrentRoomIPAddress (0x005279e0) format it the same way. With the
    # s_addr order, retail run 7 sent PERMIT PING to the reversed address.
    nat_address_byteorder="big",
    news=("Welcome to Tubisoft, a community revival of the Brothers in Arms online service.",
          "Brothers in Arms online play on Tubisoft is still in development."),
    proxy_services=("ladderquery",),
    # Reader 0x005300b0 looks up keys in [Servers] (0x006a6878) through
    # 0x004ed130, needs routers (type 0) and NAT servers (type 3), and reads
    # RouterLauncherPortN separately with a default of 40005.
    bootstrap_section="Servers",
    bootstrap_keys=("RouterIP0", "RouterPort0", "RouterLauncherPort0", "NATServerIP0", "NATServerPort0"),
)
