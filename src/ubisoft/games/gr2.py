"""GR2 bootstrap and parent-lobby discovery; room/gameplay adapters remain research.

See docs/ghost-recon-2-network.md for executable evidence and remaining gaps.
Opting in enables shared GS authentication experiments, not working multiplayer.
"""
from .base import GameProfile


class GR2Profile(GameProfile):
    def group_flags(self, flags: str) -> str:
        self.require_rooms()
        return flags

    def room_join_result(self, room) -> list:
        # 0x004d4860 mirrors the parent-join reader, including success text.
        self.require_rooms()
        return [str(room.room_id), ""]

    def game_started_fields(self, room) -> list:
        # 0x004d9560: room ID, binary data, numeric u16 port, two addresses.
        self.require_rooms()
        return ["56", [str(room.room_id), b"", room.game_port,
                       room.peer_ip, room.local_ip or room.peer_ip]]

    def game_connected(self, room, fields: list) -> None:
        # 0x004d3220 sends operation 34 with one room ID. There is no
        # operation-34 completion in 0x004c8e70; keep the GS session alive.
        self.require_rooms()
        if not room.dedicated or fields != [str(room.room_id)]:
            raise ValueError('invalid GR2 game-connected room')

    def game_transition(self, room, fields: list) -> None:
        # 0x003de860 -> 0x004c61e0 -> 0x004d23a0 sends operation 70
        # with the room ID at the observed round boundary. Like operation
        # 34, it has no completion in the SDK dispatcher 0x004c8e70.
        # It is distinct from operation 8 (leaving the room).
        self.require_rooms()
        if not room.dedicated or fields != [str(room.room_id)]:
            raise ValueError('invalid GR2 game-transition room')

    def parent_player_info(self, fields: list) -> bytes:
        # 0x003e1430 calls 0x003dd930 after parent-join success. It
        # publishes exactly 0x1c bytes from the player's GS record.
        if len(fields) != 1 or not isinstance(fields[0], bytes) or len(fields[0]) != 28:
            raise ValueError("invalid GR2 parent player data")
        return fields[0]

    def join_parent_lobby(self, fields: list) -> list:
        # 0x004d1610 sends group ID, password, and a numeric option.
        # 0x004d4430 reads group ID and optional text in the success list.
        from ..proxy import unsigned
        if (len(fields) != 3 or not all(isinstance(value, str) for value in fields)
                or fields[0] != "1" or fields[1] != ""):
            raise ValueError("unsupported GR2 parent lobby join")
        unsigned(fields[2])
        return ["1", ""]

    def ranking_table(self, fields: list, username: str) -> list:
        # 0x004efe90 builds operation 1 with app ID, product, version,
        # ladder, row selection, and column/sort/filter lists. 0x004f4fd0
        # reads a type-1 table: application query ID, total rows, columns,
        # then rows. 0x004f4de0 returns that ID to the game's callback.
        from ..proxy import unsigned
        if (len(fields) != 2 or fields[0] != "1" or not isinstance(fields[1], list)
                or len(fields[1]) != 6):
            raise ValueError("unsupported GR2 ranking request")
        query = fields[1]
        unsigned(query[0])
        unsigned(query[2])
        ladder = unsigned(query[3])
        if (query[1] != self.product or ladder > 4
                or not isinstance(query[4], list) or len(query[4]) != 2
                or unsigned(query[4][0]) > 4 or not isinstance(query[4][1], list)
                or not isinstance(query[5], list) or len(query[5]) != 3
                or not all(isinstance(part, list) for part in query[5])):
            raise ValueError("unsupported GR2 ranking selection")
        # No ranking records exist yet. Empty success lets the original
        # game follow its unranked-player path, without inventing scores.
        return ["1", [query[0], "0", [], []]]

    def login_notices(self, fields: list) -> list:
        # 0x004caab0 sends one language string; 0x004cbe40 requires a
        # nested pair of strings (each at most 512 bytes plus NUL).
        # No service notices are configured by default.
        if (len(fields) != 1 or not isinstance(fields[0], str)
                or fields[0] not in ("en", "de", "fr", "es", "it")):
            raise ValueError("unsupported GR2 notice language")
        return list(self.news)

    def discovery_login(self, fields: list) -> None:
        self.require_discovery()
        if fields != [self.product]:
            raise ValueError("unsupported GR2 discovery product")

    def parent_lobby(self) -> list:
        # The folder reader at 0x004d0d90 consumes 14 fields. This is
        # distinct from GR2's 20-field child-game reader at 0x004d0ab0.
        return ["0", self.display_name, "1", "1", "0", "0", "1", "", "",
                self.product, b"", "0", "8", "0"]


PROFILE = GR2Profile(
    slug="gr2", product="GHOSTRECON2PS2", display_name="Ghost Recon 2",
    game_version="1.0", sdk_version="GR2PS21.0", default_ports=(30010, 30011, 30012),
    supports_discovery=True,
    proxy_services=("ladderquery",),
    bootstrap_section="Servers",
    bootstrap_keys=("RouterIP0", "RouterPort0", "RouterLauncherPort0", "NATServerIP0", "NATServerPort0"),
)
