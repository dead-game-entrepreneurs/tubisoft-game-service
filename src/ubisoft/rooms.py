"""Shared transient room lifecycle; title adapters own registration and join schemas."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Callable
import time

from .games.base import GameProfile
from .games.xiii import PROFILE as XIII

PENDING_ROOM_SECONDS = 120


@dataclass
class Room:
    room_id: int
    owner: str
    peer_ip: str
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
    local_ip: str = ""
    profile: GameProfile = field(default=XIII, repr=False)
    # The owner's sign-in name exactly as the game sent it; `owner` is the
    # case-folded identity used for authorization.
    owner_name: str = ""
    active: bool = False
    pending_until: float = 0
    members: set[tuple[str, str]] = field(default_factory=set)
    member_info: dict[tuple[str, str], bytes] = field(default_factory=dict)
    host_connections: set[object] = field(default_factory=set, repr=False)
    dedicated: bool = False
    worker_players: int = 0
    worker_run_id: str | None = None

    def game_started_fields(self) -> list:
        return self.profile.game_started_fields(self)

    def group_fields(self) -> list:
        """Common GS group layout with title-specific advertisement flags."""
        advertised_flags = self.profile.group_flags(self.flags)
        host = (self.peer_ip, self.owner)
        player_count = self.worker_players if self.dedicated else 1 + len(self.members - {host})
        return [self.group_type, self.name, str(self.room_id), "1", self.parent_id,
                advertised_flags, "2", self.owner_name or self.owner, "", self.product, self.info, "0",
                self.max_players, str(player_count), self.max_spectators, "0",
                self.game_version, self.sdk_version, self.peer_ip, self.local_ip or self.peer_ip]


@dataclass
class LobbyMember:
    """A signed-in parent-lobby member, as announced to the other members."""
    name: str
    product: str
    address: str
    local_address: str
    send: Callable[[list], None] = field(repr=False)
    info: bytes = b""
    group: str = "1"
    announced: bool = False


class RoomRegistry:
    def __init__(self) -> None:
        self._rooms: dict[int, Room] = {}
        self._lobby: dict[object, LobbyMember] = {}
        # Signed-in friends channels by case-folded name: (connection, send).
        self.friend_channels: dict[str, tuple[object, Callable[[int, list], None]]] = {}
        # Stored friends of each signed-in watcher, used for live presence
        # updates: friend key -> (display name, group).
        self.friend_lists: dict[str, dict[str, tuple[str, str]]] = {}
        self._next_match = 1
        # Operation 17 state (game mode) of recent matches by match ID.
        self.match_modes: dict[str, str] = {}
        # Latest results of unfinished matches: match ID -> (room ID, entries),
        # and each room's current match ID. A room's entries are dropped when
        # its match is recorded, replaced or the room is removed.
        self.match_results: dict[str, tuple[int, list]] = {}
        self.room_matches: dict[int, str] = {}
        # Case-folded names of the players who were in a match's room at any
        # point since its operation 17; only they can be rated for it.
        self.match_players: dict[str, set[str]] = {}
        self._next_id = 1000
        self._remove_listeners: set[Callable[[Room], None]] = set()
        self.worker_provider = None
        self._worker_id = None
        self._syncing_worker = False

    def sync_worker(self) -> None:
        """Reflect a local, healthy dedicated worker without a synthetic owner player."""
        if self.worker_provider is None or self._syncing_worker:
            return
        self._syncing_worker = True
        try:
            worker = self.worker_provider()
            current = self._rooms.get(self._worker_id)
            if current and (worker is None or worker['run_id'] != current.worker_run_id):
                self._worker_id = None
                self.remove(current.room_id, current.owner, current.peer_ip)
                current = None
            if worker is None:
                return
            if current is None:
                current = Room(self._next_id, 'tubisoft-worker', worker['address'],
                               **asdict(worker['spec']), profile=worker['profile'],
                               dedicated=True, worker_run_id=worker['run_id'], active=True)
                self._next_id += 1
                self._worker_id = current.room_id
                self._rooms[current.room_id] = current
            current.name = worker['spec'].name
            current.info = worker['spec'].info
            current.additional_info = worker['spec'].additional_info
            current.peer_ip = worker['address']
            current.local_ip = worker['spec'].local_ip
            current.game_port = worker['spec'].game_port
            current.max_players = worker['spec'].max_players
            current.worker_players = worker['players']
        finally:
            self._syncing_worker = False

    def add_remove_listener(self, listener: Callable[[Room], None]) -> None:
        self._remove_listeners.add(listener)

    def remove_remove_listener(self, listener: Callable[[Room], None]) -> None:
        self._remove_listeners.discard(listener)

    def register(self, owner: str, peer_ip: str, fields: list, profile: GameProfile = XIII) -> Room:
        now = time.monotonic()
        self.expire_pending(now)
        profile.require_rooms()
        spec = profile.registration(fields, peer_ip)
        if spec.product != profile.product:
            raise ValueError("registration product does not match listener")
        settings = asdict(spec)
        pending_until = now + PENDING_ROOM_SECONDS
        existing = next((room for room in self._rooms.values()
                         if (room.owner, room.peer_ip, room.product) == (owner, peer_ip, profile.product)), None)
        if existing is not None:
            if existing.active:
                raise ValueError("owner already has a registered room for this game")
            # A repeated launcher request can race the host's UDP login.
            # Keep its room ID unless the host changed the registration settings.
            if all(getattr(existing, key) == value for key, value in settings.items()):
                existing.pending_until = pending_until
                return existing
            del self._rooms[existing.room_id]
        room_id = -self._next_id if profile.negative_room_ids else self._next_id
        room = Room(room_id, owner, peer_ip, **settings, profile=profile, pending_until=pending_until)
        self._next_id += 1
        self._rooms[room.room_id] = room
        return room

    def activate(self, room_id: int, owner: str, peer_ip: str, product: str = XIII.product) -> Room | None:
        self.expire_pending()
        room = self._rooms.get(room_id)
        if room is None or room.dedicated or room.owner != owner or room.peer_ip != peer_ip or room.product != product:
            return None
        room.active = True
        return room

    def expire_pending(self, now: float | None = None) -> None:
        """Discard registrations that never completed the host handoff."""
        if now is None:
            now = time.monotonic()
        for room_id, room in list(self._rooms.items()):
            if not room.active and room.pending_until <= now:
                del self._rooms[room_id]

    def bind_host(self, room: Room, owner: str, peer_ip: str, connection: object) -> bool:
        """Bind only the authenticated owner's actual room connection."""
        if room.dedicated or self.active_room(room.room_id) is not room or (owner, peer_ip) != (room.owner, room.peer_ip):
            return False
        room.host_connections.add(connection)
        return True

    def unbind_host(self, room: Room, connection: object) -> bool:
        if connection not in room.host_connections:
            return False
        room.host_connections.remove(connection)
        if not room.host_connections and self.active_room(room.room_id) is room:
            self.remove(room.room_id, room.owner, room.peer_ip)
            return True
        return False

    def remove(self, room_id: int, owner: str, peer_ip: str) -> None:
        room = self._rooms.get(room_id)
        if room is not None and room.owner == owner and room.peer_ip == peer_ip:
            del self._rooms[room_id]
            room.active = False
            room.host_connections.clear()
            self.forget_match(room_id)
            for listener in tuple(self._remove_listeners):
                listener(room)

    def active_room(self, room_id: int, product: str | None = None) -> Room | None:
        self.sync_worker()
        room = self._rooms.get(room_id)
        return room if room is not None and room.active and (product is None or room.product == product) else None

    def forget_match(self, room_id: int) -> str | None:
        """Drop a room's current match and its unrecorded state."""
        match_id = self.room_matches.pop(room_id, None)
        if match_id is not None:
            self.match_results.pop(match_id, None)
            self.match_modes.pop(match_id, None)
            self.match_players.pop(match_id, None)
        return match_id

    def next_match_id(self, mode: str | None = None) -> int:
        match_id = self._next_match
        self._next_match += 1
        if mode is not None:
            self.match_modes[str(match_id)] = mode
            while len(self.match_modes) > 1024:
                del self.match_modes[next(iter(self.match_modes))]
        return match_id

    def lobby_join(self, connection: object, member: LobbyMember) -> LobbyMember:
        return self._lobby.setdefault(connection, member)

    def lobby_member(self, connection: object) -> LobbyMember | None:
        return self._lobby.get(connection)

    def lobby_leave(self, connection: object) -> LobbyMember | None:
        return self._lobby.pop(connection, None)

    def lobby_members(self, product: str, exclude: object = None) -> list[LobbyMember]:
        return [member for connection, member in self._lobby.items()
                if connection is not exclude and member.product == product]

    def children(self, product: str = XIII.product) -> list:
        self.sync_worker()
        return [room.group_fields() for room in self._rooms.values() if room.active and room.product == product]
