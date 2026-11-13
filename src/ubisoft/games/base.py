"""Title adapters; shared GS services must not guess another game's schema."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..rooms import Room


class UnsupportedOperation(ValueError):
    """A title's wire format has not been implemented/verified yet."""


@dataclass(frozen=True)
class RoomSpec:
    name: str
    product: str
    group_type: str
    parent_id: str
    flags: str
    max_players: str
    max_spectators: str
    info: bytes
    additional_info: bytes
    game_port: str
    game_version: str
    sdk_version: str
    local_ip: str


@dataclass(frozen=True)
class GameProfile:
    slug: str
    product: str
    display_name: str
    game_version: str
    sdk_version: str
    default_ports: tuple[int, int, int]
    supports_rooms: bool = False
    supports_discovery: bool = False
    proxy_services: tuple[str, ...] = ()
    bootstrap_section: str | None = None
    # Rooms created by the lobby server itself (0xd1 operation 12), rather
    # than by XIII's separate launcher registration.
    lobby_create: bool = False
    # Whether the room owner also receives GAME_STARTED after its own
    # in-room player data. XIII/GR2 retain their existing behavior.
    owner_game_started: bool = True
    # Parent-lobby member announcements (operations 50/51) on the lobby
    # connection itself, rather than XIII's host UDP channel.
    lobby_members: bool = False
    # Game rooms use non-positive group IDs; lobbies stay positive.
    negative_room_ids: bool = False
    # Whether joining guests receive GAME_STARTED (operation 56).
    game_started: bool = True
    # The two login notices ("Latest News"); TUBISOFT_<SLUG>_NEWS_1/_2
    # replace them (registry.bindings).
    news: tuple[str, str] = ("", "")
    bootstrap_keys: tuple[str, ...] = (
        "routerip0", "routerport0", "routerlauncherport0", "natserverip0", "natserverport0")

    def bootstrap(self, address: str, router_port: int, launcher_port: int,
                  nat_address: str | None, nat_port: int | None) -> bytes:
        values = [address, router_port, launcher_port]
        if nat_address is not None and nat_port is not None:
            values.extend((nat_address, nat_port))
        lines = [f"[{self.bootstrap_section}]"] if self.bootstrap_section else []
        lines.extend(f"{key}={value}" for key, value in zip(self.bootstrap_keys, values))
        return ("\r\n".join(lines) + "\r\n").encode("ascii")

    def require_rooms(self) -> None:
        if not self.supports_rooms:
            raise UnsupportedOperation(f"{self.slug} room operations are not implemented")

    def require_discovery(self) -> None:
        if not (self.supports_discovery or self.supports_rooms):
            raise UnsupportedOperation(f"{self.slug} discovery is not implemented")

    def discovery_login(self, fields: list) -> None:
        self.require_discovery()

    def join_parent_lobby(self, fields: list) -> list:
        raise UnsupportedOperation(f"{self.slug} parent lobby join is not implemented")

    def parent_player_info(self, fields: list) -> bytes:
        raise UnsupportedOperation(f"{self.slug} parent player data is not implemented")

    def login_notices(self, fields: list) -> list:
        raise UnsupportedOperation(f"{self.slug} login notices are not implemented")

    def ranking_table(self, fields: list, username: str) -> list:
        raise UnsupportedOperation(f"{self.slug} ranking queries are not implemented")

    def registration(self, fields: list, peer_ip: str) -> RoomSpec:
        raise UnsupportedOperation(f"{self.slug} registration is not implemented")

    def parent_lobby(self) -> list:
        raise UnsupportedOperation(f"{self.slug} parent lobby is not implemented")

    def launcher_groups(self) -> list:
        raise UnsupportedOperation(f"{self.slug} launcher groups are not implemented")

    def group_flags(self, flags: str) -> str:
        raise UnsupportedOperation(f"{self.slug} group flags are not implemented")

    def game_started_fields(self, room: Room) -> list:
        raise UnsupportedOperation(f"{self.slug} join completion is not implemented")

    def room_join_result(self, room: Room) -> list:
        return [str(room.room_id)]

    def lobby_create_result(self, room: Room) -> list:
        raise UnsupportedOperation(f"{self.slug} lobby room creation is not implemented")

    def room_update(self, room: Room, subtype: int, values: list) -> list:
        raise UnsupportedOperation(f"{self.slug} lobby room updates are not implemented")

    # One-way in-room reports without a native completion reader.
    room_notice_subtypes = ()  # class-level, not a dataclass field

    def room_notice(self, room: Room, subtype: int, values: list) -> None:
        raise UnsupportedOperation(f"{self.slug} room reports are not implemented")

    def room_notice_reply(self, room: Room, subtype: int) -> list | None:
        # Completion for a room report whose title has a reader for it.
        return None

    def game_connected(self, room: Room, fields: list) -> None:
        raise UnsupportedOperation(f"{self.slug} game-connected notification is not implemented")

    def game_transition(self, room: Room, fields: list) -> None:
        raise UnsupportedOperation(f"{self.slug} game-transition notification is not implemented")

    def player_port(self, info: bytes) -> int:
        raise UnsupportedOperation(f"{self.slug} player info is not implemented")

    def member_events(self, room: Room, username: str, ip: str, info: bytes, *, new: bool) -> list:
        raise UnsupportedOperation(f"{self.slug} member notifications are not implemented")

    def member_left(self, username: str) -> list:
        raise UnsupportedOperation(f"{self.slug} member leave is not implemented")

    def host_operation(self, room: Room, fields: list) -> list | None:
        raise UnsupportedOperation(f"{self.slug} host operations are not implemented")

    def lobby_member_event(self, member) -> list:
        raise UnsupportedOperation(f"{self.slug} lobby member notifications are not implemented")

    def lobby_member_left(self, member) -> list:
        raise UnsupportedOperation(f"{self.slug} lobby member leave is not implemented")

    def match_started_event(self, room: Room, match_id: int) -> list:
        raise UnsupportedOperation(f"{self.slug} match start notifications are not implemented")

    def match_results_event(self, match_id: str, entries: list, mode: str | None = None,
                            changes: dict | None = None) -> list:
        raise UnsupportedOperation(f"{self.slug} match result notifications are not implemented")

    def match_ladder(self, account_db):
        # Stored match ladder for titles whose results the service rates.
        return None
