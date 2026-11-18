"""187 Ride or Die USA: opt-in experimental GS matchmaking adapter.

Native addresses refer to SLUS-21116, SHA-256 in the protocol notes. Keep
the 36-byte game settings and 12-byte player data distinct from other titles.
See docs/187-ride-or-die-network.md for the traces and test coverage.
"""
from dataclasses import dataclass
import ipaddress
import struct

from .base import GameProfile, RoomSpec

SESSION_INFO_BYTES = 36
PLAYER_INFO_BYTES = 12
GAME_PORT = 3658
PASSWORD_FLAG = 0x01
MAX_PLAYERS = 64  # Local service bound; not a claim about the retail menu.


def _uint(value, maximum=0xffffffff):
    if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            or len(value) > 10 or int(value) > maximum):
        raise ValueError("invalid 187 numeric field")
    return int(value)


def _text(value, limit=32):
    return isinstance(value, str) and len(value.encode()) <= limit and all(" " <= c <= "~" for c in value)


def session_port(info: bytes) -> int:
    # CreateRoom 0x0041e580 stores the owner's NAT port at +0x20.
    # The direct endpoint path uses 3658 (0x0041ea10).
    if not isinstance(info, bytes) or len(info) != SESSION_INFO_BYTES:
        raise ValueError("invalid 187 session info")
    return struct.unpack_from("<H", info, 32)[0] or GAME_PORT


@dataclass(frozen=True)
class PlayerInfo:
    nat: bool
    address: ipaddress.IPv4Address
    port: int

    @classmethod
    def decode(cls, info: bytes):
        # Member record +0x40: NAT flag at +1, host-order IPv4 word at +4,
        # u16 port at +8. 0x003b6580 publishes exactly these 12 bytes.
        if not isinstance(info, bytes) or len(info) != PLAYER_INFO_BYTES:
            raise ValueError("invalid 187 player info")
        return cls(bool(info[1]), ipaddress.IPv4Address(int.from_bytes(info[4:8], "little")),
                   struct.unpack_from("<H", info, 8)[0])


class RodProfile(GameProfile):
    def login_notices(self, fields: list) -> list:
        # 0x00367820 sends one language string. Reader 0x0036a800
        # requires two nested strings, each in a 0x201-byte buffer.
        if (len(fields) != 1 or not isinstance(fields[0], str)
                or fields[0] not in ("en", "fr", "es", "de", "it")):
            raise ValueError("unsupported 187 news language")
        return list(self.news)

    def account_failure(self, message_type: int, reason: str) -> list | None:
        # 0x00368a20: original operation byte, then a nested four-byte
        # little-endian error. 0x003b4010 / menu 0x0033f580 map 4 to
        # InvalidName and 15 to InvalidPassword. Create callback
        # 0x003b3f00 maps 1 to AccountExist.
        code = {(0x66, "unknown-account"): 4, (0x66, "password"): 15,
                (1, "duplicate"): 1}.get((message_type, reason))
        return None if code is None else [bytes([message_type]), [code.to_bytes(4, "little")]]

    def discovery_login(self, fields: list) -> None:
        # 0x00370450 sends operation 21 with exactly the product string.
        self.require_discovery()
        if fields != [self.product]:
            raise ValueError("unsupported 187 discovery product")

    def room_catalog(self, fields: list) -> None:
        # 0x00368400 sends 109 on the router connection, with no completion
        # reader in 0x00367ca0. Groups arrive through operation 53 instead.
        self.require_rooms()
        if fields != [self.product]:
            raise ValueError("unsupported 187 room catalog product")

    def parent_lobby(self, players: int = 0) -> list:
        # 0x0036fd90 reads the 14-field folder; 0x0037d000 dispatches
        # operation 53 with flag 0x100 to the folder-added callback.
        self.require_discovery()
        return ["0", self.display_name, "1", "1", "0", "0", "1", "", "",
                self.product, b"", "0", "100", str(players)]

    def join_parent_lobby(self, fields: list) -> list:
        # Sender 0x003707a0, reader 0x00372fe0: ID and optional success text.
        if len(fields) != 3 or fields[:2] != ["1", ""]:
            raise ValueError("unsupported 187 parent join")
        _uint(fields[2])
        return ["1", ""]

    def parent_player_info(self, fields: list) -> bytes:
        if len(fields) != 1:
            raise ValueError("invalid 187 player data structure")
        PlayerInfo.decode(fields[0])
        return fields[0]

    def room_player_info(self, fields: list) -> bytes:
        # 0x003b5770 copies the supplied byte count into the same record.
        # Enforce its actual size before publishing to any other client.
        return self.parent_player_info(fields)

    def player_port(self, info: bytes) -> int:
        return PlayerInfo.decode(info).port

    def player_endpoint(self, info: bytes, peer_ip: str) -> str:
        from ..router import endpoint_shape
        PlayerInfo.decode(info)
        return endpoint_shape(info[4:10], peer_ip, self.nat_address_byteorder)

    def lobby_member_event(self, member) -> list:
        # 0x0037ec00 -> 0x003b5280: seven fields, one group normalized to
        # an array by the SDK. Empty data is allowed for the initial self.
        if member.info:
            PlayerInfo.decode(member.info)
        return ["50", [member.name, "0", member.group, member.address,
                       member.local_address or member.address, member.info, "0"]]

    def lobby_member_left(self, member) -> list:
        return ["51", [member.name, member.group]]

    def lobby_player_update(self, member) -> list:
        # 0x003b5770 updates an existing player. Repeating operation 50
        # would run 0x0041ea10 again and duplicate the in-room peer entry.
        PlayerInfo.decode(member.info)
        return ["66", [member.name, member.info]]

    def registration(self, fields: list, peer_ip: str) -> RoomSpec:
        # 0x0041e580 -> 0x00370e90: lobby operation 12, no launcher.
        types = (str, str, str, str, str, str, bytes, str, str, str, bytes)
        if len(fields) != len(types) or not all(isinstance(v, t) for v, t in zip(fields, types)):
            raise ValueError("unsupported 187 room creation structure")
        parent, name, product, kind, players, spectators, info, password, version, sdk, extra = fields
        if parent != "1" or product != self.product or kind != "5" or spectators != "0":
            raise ValueError("unsupported 187 room identity or type")
        if not name or not _text(name) or not _text(password):
            raise ValueError("invalid 187 room text")
        if version != self.game_version or sdk != self.sdk_version:
            raise ValueError("unsupported 187 room version")
        if not 1 <= _uint(players, 0xffff) <= MAX_PLAYERS or len(extra) > 16384:
            raise ValueError("invalid 187 room limits")
        port = session_port(info)
        # Browser 0x00346df0 calls 0x003b64f0: room flags & 1 controls
        # the password prompt. A checked password without this flag cannot
        # be entered through the normal retail join flow.
        flags = str(PASSWORD_FLAG if password else 0)
        return RoomSpec(name, product, kind, parent, flags, players, spectators, info, extra,
                        str(port), version, sdk, peer_ip, password)

    def lobby_create_result(self, room) -> list:
        # 0x0041ded0 immediately self-joins with the returned ID/server.
        return [str(room.room_id), "", room.parent_id]

    def room_join_result(self, room) -> list:
        # 0x00373590 -> 0x0041df90: room ID and optional text.
        return [str(room.room_id), ""]

    def room_join_failure(self, room, reason: str) -> list | None:
        # 0x0041df90 maps 7 to the unavailable/full state (8), and gives
        # wrong-password reason 11 its own failure state (9).
        code = {"password": "11", "full": "7"}.get(reason)
        return None if code is None else [code, str(room.room_id), ""]

    def group_flags(self, flags: str) -> str:
        _uint(flags)
        return flags

    def group_refresh(self, values: list) -> tuple[bool, bool]:
        if not values or values[0] != "1" or len(values) > 2:
            raise ValueError("unsupported 187 group refresh")
        mask = _uint(values[1]) if len(values) == 2 else 0x1c0
        # Parent joins already seed all members. 0x003b4f90 retains them
        # on a game leave, and repeating joins duplicates 0x0041ea10 peers.
        return bool(mask & 0x100), False

    def host_endpoint(self, room, peer_ip: str) -> str:
        return f"{'peer' if room.peer_ip == peer_ip else 'other'}:{session_port(room.info)}"

    ROOM_FIELDS = ((0x8, "players"), (0x10, "spectators"), (0x20, "name"),
                   (0x40, "info"), (0x400, "extra"))
    ROOM_MASK = 0x206 | 0x8 | 0x10 | 0x20 | 0x40 | 0x400

    def room_update(self, room, subtype: int, values: list) -> list:
        if not values or values[0] != str(room.room_id):
            raise ValueError("187 update does not name the owner's room")
        if subtype == 31:
            self._room_data_update(room, values[1:])
        elif subtype == 33:
            # 0x00371b70: room ID, binary, port, local address string.
            if (len(values) != 4 or not isinstance(values[1], bytes) or len(values[1]) > 16384
                    or not isinstance(values[3], str)):
                raise ValueError("invalid 187 endpoint update")
            port = _uint(values[2], 0xffff)
            try:
                address = ipaddress.IPv4Address(values[3]) if values[3] else None
            except ValueError:
                raise ValueError("invalid 187 endpoint address") from None
            if port:
                room.game_port = str(port)
            if address is not None and not (address.is_unspecified or address.is_multicast
                    or address.is_loopback or address in ipaddress.IPv4Network("192.0.2.0/24")):
                room.local_ip = str(address)
        elif subtype == 17:
            # 0x00371170 includes a third numeric u8 (unlike R6/BiA).
            if len(values) != 3:
                raise ValueError("invalid 187 match start")
            _uint(values[1])
            _uint(values[2], 255)
        elif subtype != 15 or len(values) != 1:
            raise ValueError("unsupported 187 room update")
        return [str(room.room_id)]

    def _room_data_update(self, room, values: list) -> None:
        # 0x00371770: field-selection mask, optional flag values, then
        # player/spectator limits, name, game data and additional data.
        if not values:
            raise ValueError("187 room update has no mask")
        mask = _uint(values[0])
        if mask & ~self.ROOM_MASK:
            raise ValueError("unsupported 187 room update mask")
        rest, changes = list(values[1:]), {}
        flags = None
        if mask & 0x206:
            if not rest:
                raise ValueError("short 187 room update")
            flags = _uint(rest.pop(0))
        for bit, key in self.ROOM_FIELDS:
            if mask & bit:
                if not rest:
                    raise ValueError("short 187 room update")
                changes[key] = rest.pop(0)
        if rest:
            raise ValueError("long 187 room update")
        if "players" in changes and not 1 <= _uint(changes["players"], 0xffff) <= MAX_PLAYERS:
            raise ValueError("invalid 187 player limit")
        if "spectators" in changes and _uint(changes["spectators"], 0xffff) != 0:
            raise ValueError("187 spectators are unsupported")
        if "name" in changes and (not changes["name"] or not _text(changes["name"])):
            raise ValueError("invalid 187 room name")
        if "info" in changes:
            session_port(changes["info"])
        if "extra" in changes and (not isinstance(changes["extra"], bytes) or len(changes["extra"]) > 16384):
            raise ValueError("invalid 187 additional data")
        # Validate everything before changing a live room.
        if flags is not None:
            old = _uint(room.flags)
            for selected, advertised in ((0x200, 0x4000), (0x2, 0x10), (0x4, 0x800)):
                if mask & selected:
                    old = (old & ~advertised) | (flags & advertised)
            room.flags = str(old)
        for key, attr in (("players", "max_players"), ("name", "name"),
                          ("info", "info"), ("extra", "additional_info")):
            if key in changes:
                setattr(room, attr, changes[key])
        if "info" in changes:
            room.game_port = str(session_port(room.info))

    def room_update_event(self, room, subtype: int) -> list | None:
        # 0x003b4e60 refreshes settings/counts in an existing room record.
        if subtype in (17, 31, 33):
            return ["53", [str(room.room_id), "64", room.group_fields()]]
        return None

    def match_started_event(self, room, match_id: int) -> list:
        # SDK 0x00377f40 reads room ID and match ID for operation 62.
        return ["62", [str(room.room_id), str(match_id)]]

    room_notice_subtypes = (34, 35, 44, 45, 70)

    def room_notice(self, room, subtype: int, values: list) -> None:
        if values != [str(room.room_id)]:
            raise ValueError("187 report does not name the joined room")

    def room_notice_reply(self, room, subtype: int) -> list | None:
        # 0x0037f900 reads the room ID; server ID comes from SDK context.
        if subtype == 45:
            return ["38", ["45", [str(room.room_id)]]]
        return None


PROFILE = RodProfile(
    slug="rod", product="RODPS2US", display_name="187 Ride or Die",
    game_version="1.0", sdk_version="RODPS21.0", default_ports=(30040, 30041, 30042),
    supports_discovery=True, supports_rooms=True, lobby_create=True,
    lobby_members=True, negative_room_ids=True, creator_room_listing=True,
    # NAT completion 0x0043eda0 -> 0x003b6580 needs the client's own member
    # entry before it can publish player data. Seed it after the parent join.
    self_member_event=True, self_room_member_event=True, product_room_catalog=True,
    room_join_resends_members=False, refresh_resends_members=False,
    # 0x0041ea10 uses the NAT word directly, or ntohl(parsed GS address).
    nat_address_byteorder="big", game_started=False, owner_game_started=False,
    news=("Welcome to Tubisoft's 187 Ride or Die online service.",
          "This service is being tested. Thanks for helping bring the game back online."),
)
