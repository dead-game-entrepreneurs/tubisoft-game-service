"""Shared GS proxy directory, short-lived handoffs, and query envelopes.

Title-specific service names and result schemas belong to game profiles.
No credentials, peer addresses, tickets, or query contents are persisted here.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import time

from .games.base import GameProfile


def unsigned(value: str, bits: int = 32) -> int:
    if (not isinstance(value, str) or not value or len(value) > 10
            or not value.isascii() or not value.isdigit() or int(value) >= 1 << bits):
        raise ValueError("invalid proxy integer")
    return int(value)


class Directory:
    """Service lookup/selection on one authenticated router connection."""
    def __init__(self, profile: GameProfile, address: str, port: int):
        self.profile, self.address, self.port = profile, address, port
        self.advertised: set[str] = set()

    def reply(self, fields: list) -> list | None:
        # 0x004ecf10 reports the socket result as ONE nested list, unlike
        # lookup/selection's operation-plus-list request envelope.
        if (len(fields) == 1 and isinstance(fields[0], list)
                and len(fields[0]) == 2 and fields[0][0] in ("3", "4")
                and fields[0][1] == "1" and "1" in self.advertised):
            return None
        if len(fields) != 2 or not isinstance(fields[1], list):
            raise ValueError("invalid proxy directory request")
        operation = unsigned(fields[0], 16)
        values = fields[1]
        if operation == 1:
            if (len(values) != 3 or values[0] not in self.profile.proxy_services
                    or values[1:] != ["0", "0"]):
                raise ValueError("unsupported proxy service")
            # 0x004eb1f0 reads service/version/instance, then server records.
            self.advertised.add("1")
            return ["38", ["1", [values[0], "0", "0", [["1", self.address, str(self.port)]]]]]
        if operation == 2 and values == ["1"] and "1" in self.advertised:
            # 0x004ec280 starts the proxy socket after this selection.
            return ["38", ["2", ["1"]]]
        raise ValueError("unsupported proxy directory operation")


@dataclass(frozen=True)
class Grant:
    peer: str
    username: str
    expires: float


class Handoffs:
    """Per-title, bounded, single-use proxy tickets; never infer an IP's user."""
    def __init__(self, *, limit: int = 4096, lifetime: float = 120):
        self.limit, self.lifetime = limit, lifetime
        self.grants: dict[str, Grant] = {}
        self.logins: dict[tuple[str, str, str], float] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for ticket, grant in list(self.grants.items()):
            if grant.expires <= now:
                del self.grants[ticket]
        for key, expires in list(self.logins.items()):
            if expires <= now:
                del self.logins[key]

    def authorize_login(self, peer: str, username: str, version: str) -> None:
        """Called only after service selection on the authenticated wait socket."""
        self._prune()
        key = (peer, username.casefold(), version)
        if key not in self.logins and len(self.grants) + len(self.logins) >= self.limit:
            raise ValueError("proxy authorization capacity reached")
        self.logins[key] = time.monotonic() + self.lifetime

    def consume_login(self, peer: str, username: str, version: str) -> bool:
        self._prune()
        return self.logins.pop((peer, username.casefold(), version), None) is not None

    def issue(self, peer: str, username: str) -> str:
        self._prune()
        if len(self.grants) + len(self.logins) >= self.limit:
            raise ValueError("proxy handoff capacity reached")
        # GR2's redirect reader has a 16-byte ticket buffer including NUL.
        ticket = secrets.token_urlsafe(10)
        self.grants[ticket] = Grant(peer, username, time.monotonic() + self.lifetime)
        return ticket

    def redeem(self, peer: str, ticket: str) -> str | None:
        self._prune()
        grant = self.grants.get(ticket)
        if grant is None or grant.peer != peer:
            return None
        del self.grants[ticket]
        return grant.username


def query_reply(profile: GameProfile, fields: list, username: str, *, ranking_store=None,
                account_db=None) -> list:
    if len(fields) != 3 or unsigned(fields[0], 16) != 0x501:
        raise ValueError("unsupported proxy query")
    unsigned(fields[1])
    if not isinstance(fields[2], list):
        raise ValueError("invalid proxy query payload")
    if ranking_store is not None and profile.slug=='gr2':
        from .games.gr2_ladder import ranking_table
        table=ranking_table(profile,fields[2],username,ranking_store)
    elif account_db is not None and profile.match_ladder(account_db) is not None:
        from .games.r6_ladder import ranking_table
        table = ranking_table(profile, fields[2], username, account_db)
    else:table = profile.ranking_table(fields[2], username)
    # 0x004c6a70 reconstructs the SDK result and appends the transport
    # correlation ID. Do not put the application's separate ID in its place.
    return [fields[0], fields[1], ["38", table]]
