import concurrent.futures
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ubisoft import accounts


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state" / "accounts.sqlite3"

    def test_accounts_survive_restart_and_store_salted_hashes_only(self):
        # Synthetic test fixture, never a deployed account.
        self.assertTrue(accounts.create_account(self.path, "ExampleOne", "fixture-one"))
        self.assertTrue(accounts.create_account(self.path, "ExampleTwo", "fixture-one"))
        self.assertFalse(accounts.create_account(self.path, "exampleone", "fixture-other"))
        accounts.initialize_database(self.path)
        self.assertTrue(accounts.verify_account(self.path, "EXAMPLEONE", "fixture-one"))
        self.assertFalse(accounts.verify_account(self.path, "ExampleOne", "incorrect"))
        self.assertFalse(accounts.verify_account(self.path, "absent", "fixture-one"))
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT salt, password_hash, created_at, last_login_at FROM accounts ORDER BY name").fetchall()
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertNotEqual(rows[0][:2], rows[1][:2])
        self.assertEqual(len(rows[0][1]), 32)
        self.assertGreater(rows[0][2], 0)
        self.assertIsNotNone(rows[0][3])
        self.assertNotIn(b"fixture-one", self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_legacy_database_migrates_without_losing_passwords_and_backup_restores(self):
        self.path.parent.mkdir()
        salt = bytes(range(16))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE accounts (name_key TEXT PRIMARY KEY, name TEXT NOT NULL, salt BLOB NOT NULL, password_hash BLOB NOT NULL)")
            db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?)", ("example", "Example", salt, accounts._hash("fixture", salt)))
            db.commit()
        accounts.initialize_database(self.path)
        self.assertTrue(accounts.verify_account(self.path, "Example", "fixture"))
        snapshot = self.path.parent / "snapshot.sqlite3"
        accounts.backup_database(self.path, snapshot)
        self.assertTrue(accounts.verify_account(snapshot, "example", "fixture"))
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            accounts.backup_database(self.path, snapshot)

    def test_country_is_the_only_stored_profile_field(self):
        self.assertTrue(accounts.create_account(self.path, "ExampleOne", "fixture-one"))
        self.assertEqual(accounts.country(self.path, "exampleone"), "")
        self.assertTrue(accounts.set_country(self.path, "EXAMPLEONE", "CA"))
        self.assertEqual(accounts.country(self.path, "ExampleOne"), "CA")
        self.assertTrue(accounts.set_country(self.path, "exampleone", "US"))
        self.assertEqual(accounts.country(self.path, "exampleone"), "US")
        for bad in ("x" * 16, "tab\there", "caf\u00e9"):
            self.assertFalse(accounts.set_country(self.path, "exampleone", bad))
        self.assertFalse(accounts.set_country(self.path, "missing", "CA"))
        self.assertTrue(accounts.set_country(self.path, "exampleone", ""))
        self.assertEqual(accounts.country(self.path, "exampleone"), "")

    def test_ignore_list(self):
        for name in ("ExampleOne", "ExampleTwo"):
            self.assertTrue(accounts.create_account(self.path, name, "fixture-one"))
        self.assertEqual(accounts.ignored(self.path, "exampleone"), [])
        self.assertEqual(accounts.add_ignored(self.path, "exampleone", "EXAMPLETWO"), "ExampleTwo")
        self.assertEqual(accounts.add_ignored(self.path, "exampleone", "exampletwo"), "ExampleTwo")
        for name in ("ExampleOne", "Missing", ""):
            self.assertIsNone(accounts.add_ignored(self.path, "exampleone", name))
        self.assertEqual(accounts.ignored(self.path, "ExampleOne"), ["ExampleTwo"])
        self.assertEqual(accounts.ignoring(self.path, "exampleone", ["ExampleTwo", "Other"]), {"exampletwo"})
        self.assertEqual(accounts.ignoring(self.path, "exampletwo", ["ExampleOne"]), set())
        self.assertTrue(accounts.remove_ignored(self.path, "exampleone", "ExampleTwo"))
        self.assertFalse(accounts.remove_ignored(self.path, "exampleone", "ExampleTwo"))
        self.assertEqual(accounts.ignored(self.path, "exampleone"), [])

    def test_search_lookup_and_friends_list(self):
        for name in ("ExampleOne", "ExampleTwo", "Other_3"):
            self.assertTrue(accounts.create_account(self.path, name, "fixture-one"))
        self.assertEqual(accounts.search_accounts(self.path, "example"), ["ExampleOne", "ExampleTwo"])
        self.assertEqual(accounts.search_accounts(self.path, "_"), ["Other_3"])
        self.assertEqual(accounts.search_accounts(self.path, "%"), [])
        self.assertEqual(accounts.find_account(self.path, "exampletwo"), "ExampleTwo")
        self.assertIsNone(accounts.find_account(self.path, "absent"))
        self.assertEqual(accounts.add_friend(self.path, "exampleone", "EXAMPLETWO", "group"), "ExampleTwo")
        self.assertEqual(accounts.add_friend(self.path, "exampleone", "exampletwo", "other"), "ExampleTwo")
        for friend, group in (("absent", ""), ("ExampleOne", ""), ("Other_3", "x" * 33)):
            self.assertIsNone(accounts.add_friend(self.path, "ExampleOne", friend, group))
        self.assertEqual(accounts.friends(self.path, "EXAMPLEONE"), [("ExampleTwo", "other")])
        self.assertEqual(accounts.friends(self.path, "ExampleTwo"), [])
        self.assertTrue(accounts.remove_friend(self.path, "ExampleOne", "exampletwo"))
        self.assertFalse(accounts.remove_friend(self.path, "ExampleOne", "exampletwo"))
        self.assertEqual(accounts.friends(self.path, "ExampleOne"), [])

    def test_concurrent_duplicate_creation_is_atomic(self):
        accounts.initialize_database(self.path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: accounts.create_account(self.path, "Example", "fixture"), range(2)))
        self.assertEqual(sorted(outcomes), [False, True])
