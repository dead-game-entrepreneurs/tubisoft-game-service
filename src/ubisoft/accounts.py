"""SQLite account persistence; no plaintext passwords, emails, or client IPs."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path


@contextmanager
def _connection(path: Path):
    with closing(sqlite3.connect(path, timeout=10)) as db:
        with db:
            yield db


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)
    with _connection(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ValueError("account database schema is newer than this server")
        db.execute("CREATE TABLE IF NOT EXISTS accounts (name_key TEXT PRIMARY KEY, name TEXT NOT NULL, salt BLOB NOT NULL, password_hash BLOB NOT NULL)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
        if "created_at" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0")
        if "last_login_at" not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN last_login_at INTEGER")
        db.execute("PRAGMA user_version=1")


def _valid(username: str, password: str, *, creating: bool = False) -> bool:
    if not username or not password or len(username) > (16 if creating else 128) or len(password) > 256:
        return False
    try:
        username.encode("utf-8")
        password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ord(char) < 32 or ord(char) == 127 for char in username + password)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)


def create_account(path: Path, username: str, password: str) -> bool:
    """Atomically create a case-insensitive name, retaining the display spelling."""
    if not _valid(username, password, creating=True):
        raise ValueError("invalid account fields")
    initialize_database(path)
    salt = secrets.token_bytes(16)
    password_hash = _hash(password, salt)
    with _connection(path) as db:
        try:
            db.execute("INSERT INTO accounts (name_key, name, salt, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                       (username.casefold(), username, salt, password_hash, int(time.time())))
        except sqlite3.IntegrityError:
            return False
    return True


def verify_account(path: Path, username: str, password: str) -> bool:
    if not _valid(username, password):
        return False
    initialize_database(path)
    with _connection(path) as db:
        row = db.execute("SELECT salt, password_hash FROM accounts WHERE name_key = ?", (username.casefold(),)).fetchone()
    candidate = _hash(password, row[0] if row else bytes(16))
    if row is None or not hmac.compare_digest(candidate, row[1]):
        return False
    with _connection(path) as db:
        db.execute("UPDATE accounts SET last_login_at = ? WHERE name_key = ?", (int(time.time()), username.casefold()))
    return True


def search_accounts(path: Path, text: str, limit: int = 20) -> list[str]:
    """Display names containing `text` (case-insensitive), alphabetically."""
    if not text or len(text) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        return []
    initialize_database(path)
    escaped = text.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _connection(path) as db:
        rows = db.execute("SELECT name FROM accounts WHERE name_key LIKE ? ESCAPE '\\' "
                          "ORDER BY name_key LIMIT ?", (f"%{escaped}%", limit)).fetchall()
    return [row[0] for row in rows]


MAX_FRIENDS = 100


def _friends_table(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS friends ("
               "owner TEXT NOT NULL REFERENCES accounts(name_key), "
               "friend TEXT NOT NULL REFERENCES accounts(name_key), "
               "friend_group TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, "
               "PRIMARY KEY(owner, friend))")


def find_account(path: Path, username: str) -> str | None:
    """The display spelling of an existing account, or None."""
    if not username or len(username) > 128:
        return None
    initialize_database(path)
    with _connection(path) as db:
        row = db.execute("SELECT name FROM accounts WHERE name_key = ?", (username.casefold(),)).fetchone()
    return row[0] if row else None


def friends(path: Path, owner: str) -> list[tuple[str, str]]:
    """(display name, group) pairs on the owner's friends list."""
    initialize_database(path)
    with _connection(path) as db:
        _friends_table(db)
        rows = db.execute("SELECT accounts.name, friends.friend_group FROM friends "
                          "JOIN accounts ON accounts.name_key = friends.friend "
                          "WHERE friends.owner = ? ORDER BY friends.friend", (owner.casefold(),)).fetchall()
    return [(row[0], row[1]) for row in rows]


def add_friend(path: Path, owner: str, friend: str, group: str = "") -> str | None:
    """Add an existing account (not the owner); returns its display name."""
    if (not friend or friend.casefold() == owner.casefold() or len(group) > 32
            or any(ord(char) < 32 or ord(char) == 127 for char in group)):
        return None
    initialize_database(path)
    with _connection(path) as db:
        _friends_table(db)
        names = dict(db.execute("SELECT name_key, name FROM accounts WHERE name_key IN (?, ?)",
                                (owner.casefold(), friend.casefold())).fetchall())
        if owner.casefold() not in names or friend.casefold() not in names:
            return None
        count = db.execute("SELECT COUNT(*) FROM friends WHERE owner = ?", (owner.casefold(),)).fetchone()[0]
        exists = db.execute("SELECT 1 FROM friends WHERE owner = ? AND friend = ?",
                            (owner.casefold(), friend.casefold())).fetchone()
        if exists is None and count >= MAX_FRIENDS:
            return None
        db.execute("INSERT INTO friends (owner, friend, friend_group, created_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(owner, friend) DO UPDATE SET friend_group = excluded.friend_group",
                   (owner.casefold(), friend.casefold(), group, int(time.time())))
    return names[friend.casefold()]


def remove_friend(path: Path, owner: str, friend: str) -> bool:
    if not friend:
        return False
    initialize_database(path)
    with _connection(path) as db:
        _friends_table(db)
        removed = db.execute("DELETE FROM friends WHERE owner = ? AND friend = ?",
                             (owner.casefold(), friend.casefold())).rowcount
    return removed > 0


MAX_IGNORED = 100


def _ignores_table(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS ignores ("
               "owner TEXT NOT NULL REFERENCES accounts(name_key), "
               "ignored TEXT NOT NULL REFERENCES accounts(name_key), "
               "created_at INTEGER NOT NULL, PRIMARY KEY(owner, ignored))")


def ignored(path: Path, owner: str) -> list[str]:
    """Display names on the owner's ignore list."""
    initialize_database(path)
    with _connection(path) as db:
        _ignores_table(db)
        rows = db.execute("SELECT accounts.name FROM ignores JOIN accounts ON accounts.name_key = ignores.ignored "
                          "WHERE ignores.owner = ? ORDER BY ignores.ignored", (owner.casefold(),)).fetchall()
    return [row[0] for row in rows]


def add_ignored(path: Path, owner: str, name: str) -> str | None:
    """Ignore an existing account (not the owner); returns its display name."""
    if not name or len(name) > 128 or name.casefold() == owner.casefold():
        return None
    initialize_database(path)
    with _connection(path) as db:
        _ignores_table(db)
        names = dict(db.execute("SELECT name_key, name FROM accounts WHERE name_key IN (?, ?)",
                                (owner.casefold(), name.casefold())).fetchall())
        if owner.casefold() not in names or name.casefold() not in names:
            return None
        exists = db.execute("SELECT 1 FROM ignores WHERE owner = ? AND ignored = ?",
                            (owner.casefold(), name.casefold())).fetchone()
        count = db.execute("SELECT COUNT(*) FROM ignores WHERE owner = ?", (owner.casefold(),)).fetchone()[0]
        if exists is None and count >= MAX_IGNORED:
            return None
        db.execute("INSERT OR IGNORE INTO ignores (owner, ignored, created_at) VALUES (?, ?, ?)",
                   (owner.casefold(), name.casefold(), int(time.time())))
    return names[name.casefold()]


def remove_ignored(path: Path, owner: str, name: str) -> bool:
    if not name or len(name) > 128:
        return False
    initialize_database(path)
    with _connection(path) as db:
        _ignores_table(db)
        removed = db.execute("DELETE FROM ignores WHERE owner = ? AND ignored = ?",
                             (owner.casefold(), name.casefold())).rowcount
    return removed > 0


def ignoring(path: Path, owner: str, names: list[str]) -> set[str]:
    """Case-folded names among `names` that the owner ignores."""
    initialize_database(path)
    with _connection(path) as db:
        _ignores_table(db)
        rows = db.execute("SELECT ignored FROM ignores WHERE owner = ?", (owner.casefold(),)).fetchall()
    listed = {row[0] for row in rows}
    return {name.casefold() for name in names if name.casefold() in listed}


def _countries_table(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS countries ("
               "name_key TEXT PRIMARY KEY REFERENCES accounts(name_key), "
               "country TEXT NOT NULL, updated_at INTEGER NOT NULL)")


def valid_country(code: str) -> bool:
    # A short code chosen in the game's profile screen, nothing personal.
    return isinstance(code, str) and len(code) <= 15 and all(" " <= char <= "~" for char in code)


def set_country(path: Path, owner: str, code: str) -> bool:
    if not valid_country(code):
        return False
    initialize_database(path)
    with _connection(path) as db:
        _countries_table(db)
        if db.execute("SELECT 1 FROM accounts WHERE name_key = ?", (owner.casefold(),)).fetchone() is None:
            return False
        if code:
            db.execute("INSERT INTO countries (name_key, country, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(name_key) DO UPDATE SET country = excluded.country, "
                       "updated_at = excluded.updated_at", (owner.casefold(), code, int(time.time())))
        else:
            db.execute("DELETE FROM countries WHERE name_key = ?", (owner.casefold(),))
    return True


def country(path: Path, name: str) -> str:
    """The stored country code of an account, or ""."""
    initialize_database(path)
    with _connection(path) as db:
        _countries_table(db)
        row = db.execute("SELECT country FROM countries WHERE name_key = ?", (name.casefold(),)).fetchone()
    return row[0] if row else ""


def backup_database(path: Path, destination: Path) -> None:
    """Snapshot committed SQLite state, including WAL, without copying live files."""
    if path.resolve() == destination.resolve() or destination.exists():
        raise ValueError("backup destination must be a new, separate file")
    if not path.is_file():
        raise ValueError("account database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup integrity check failed")
