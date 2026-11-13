import unittest

from ubisoft.rooms import RoomRegistry


def registration():
    return ["12", "1", ["launcher", "1", "Example", "XIIIPS2US", "6", "16", "0",
                         b"2?17?20?10?N?8:7777", b"\0", "", "1.0", "XIIIPS21.0",
                         "8208", "192.0.2.2", "7777", b""]]


class RoomRegistryTests(unittest.TestCase):
    def test_host_connections_require_ownership_and_only_the_last_departure_removes_the_room(self):
        registry = RoomRegistry()
        room = registry.register("host", "198.51.100.2", registration())
        registry.activate(room.room_id, room.owner, room.peer_ip)
        first, second, guest = object(), object(), object()
        self.assertFalse(registry.bind_host(room, "guest", room.peer_ip, guest))
        self.assertFalse(registry.bind_host(room, "host", "198.51.100.3", guest))
        self.assertTrue(registry.bind_host(room, room.owner, room.peer_ip, first))
        self.assertTrue(registry.bind_host(room, room.owner, room.peer_ip, second))
        self.assertFalse(registry.unbind_host(room, guest))
        self.assertFalse(registry.unbind_host(room, first))
        self.assertIs(registry.active_room(room.room_id), room)
        self.assertTrue(registry.unbind_host(room, second))
        self.assertIsNone(registry.active_room(room.room_id))
        self.assertFalse(registry.unbind_host(room, second))

    def test_public_and_client_supplied_addresses_remain_distinct_and_nonempty(self):
        registry = RoomRegistry()
        room = registry.register("host", "198.51.100.2", registration())
        registry.activate(room.room_id, room.owner, room.peer_ip)
        self.assertEqual(room.game_started_fields()[1][3:], ["198.51.100.2", "192.0.2.2"])
        self.assertEqual(room.group_fields()[18:], ["198.51.100.2", "192.0.2.2"])

    def test_unusable_supplied_addresses_fall_back_to_observed_address(self):
        for supplied in ("", "not-an-address", "0.0.0.0", "127.0.0.1", "255.255.255.255"):
            with self.subTest(supplied=supplied):
                registry = RoomRegistry()
                values = registration()
                values[2][13] = supplied
                room = registry.register("host", "198.51.100.2", values)
                registry.activate(room.room_id, room.owner, room.peer_ip)
                self.assertEqual(room.game_started_fields()[1][3:], [room.peer_ip, room.peer_ip])
                self.assertEqual(room.group_fields()[18:], [room.peer_ip, room.peer_ip])

    def test_remove_event_requires_owner_and_occurs_once(self):
        registry = RoomRegistry()
        room = registry.register("user", "192.0.2.2", registration())
        removed = []
        registry.add_remove_listener(removed.append)
        registry.remove(room.room_id, "other", room.peer_ip)
        self.assertEqual(removed, [])
        registry.remove(room.room_id, room.owner, room.peer_ip)
        registry.remove(room.room_id, room.owner, room.peer_ip)
        self.assertEqual(removed, [room])
        registry.remove_remove_listener(removed.append)
        next_room = registry.register("user", "192.0.2.2", registration())
        registry.remove(next_room.room_id, next_room.owner, next_room.peer_ip)
        self.assertEqual(removed, [room])

    def test_remove_drops_the_rooms_unrecorded_match(self):
        registry = RoomRegistry()
        room = registry.register("user", "192.0.2.2", registration())
        other = registry.register("other", "192.0.2.3", registration())
        for owned in (room, other):
            match_id = str(registry.next_match_id("3"))
            registry.room_matches[owned.room_id] = match_id
            registry.match_results[match_id] = (owned.room_id, [])
            registry.match_players[match_id] = {owned.owner}
        kept = registry.room_matches[other.room_id]
        registry.remove(room.room_id, room.owner, room.peer_ip)
        self.assertEqual(registry.room_matches, {other.room_id: kept})
        self.assertEqual(list(registry.match_results), [kept])
        self.assertEqual(list(registry.match_modes), [kept])
        self.assertEqual(list(registry.match_players), [kept])
        self.assertIsNone(registry.forget_match(room.room_id))

    def test_room_requires_owner_activation_and_disappears_on_remove(self):
        registry = RoomRegistry()
        room = registry.register("user", "192.0.2.2", registration())
        self.assertEqual(registry.children(), [])
        self.assertIsNone(registry.activate(room.room_id, "other", "192.0.2.2"))
        self.assertIsNone(registry.activate(room.room_id, "user", "192.0.2.3"))
        self.assertIs(registry.activate(room.room_id, "user", "192.0.2.2"), room)
        self.assertEqual(registry.children()[0][2], str(room.room_id))
        self.assertEqual(registry.children()[0][10], b"2?17?20?10?N?8:7777")
        registry.remove(room.room_id, "other", "192.0.2.2")
        self.assertEqual(len(registry.children()), 1)
        registry.remove(room.room_id, "user", "192.0.2.2")
        self.assertEqual(registry.children(), [])

    def test_rejects_unsupported_requests_and_duplicate_active_rooms(self):
        registry = RoomRegistry()
        malformed = registration()
        malformed[2][7] = "not binary"
        with self.assertRaises(ValueError):
            registry.register("user", "192.0.2.2", malformed)
        private = registration()
        private[2][9] = "secret"
        with self.assertRaises(ValueError):
            registry.register("user", "192.0.2.2", private)
        room = registry.register("user", "192.0.2.2", registration())
        registry.activate(room.room_id, room.owner, room.peer_ip)
        with self.assertRaises(ValueError):
            registry.register("user", "192.0.2.2", registration())

    def test_registration_with_new_settings_replaces_unfinished_room(self):
        registry = RoomRegistry()
        room = registry.register("user", "192.0.2.2", registration())
        updated = registration()
        updated[2][7] = b"2?18?20?10?N?8:7777"

        replacement = registry.register(room.owner, room.peer_ip, updated)

        self.assertNotEqual(replacement.room_id, room.room_id)
        self.assertIsNone(registry.activate(room.room_id, room.owner, room.peer_ip))
        self.assertIs(registry.activate(replacement.room_id, room.owner, room.peer_ip), replacement)
        self.assertEqual(replacement.info, updated[2][7])

    def test_pending_room_survives_launcher_close_but_eventually_expires(self):
        registry = RoomRegistry()
        room = registry.register("user", "192.0.2.2", registration())
        registry.expire_pending(room.pending_until - 1)
        self.assertIs(registry.activate(room.room_id, "user", "192.0.2.2"), room)
        registry.expire_pending(room.pending_until + 1)
        self.assertEqual(len(registry.children()), 1)

        abandoned = registry.register("other", "192.0.2.3", registration())
        registry.expire_pending(abandoned.pending_until)
        self.assertIsNone(registry.activate(abandoned.room_id, "other", "192.0.2.3"))
        registry.register("other", "192.0.2.3", registration())


if __name__ == "__main__":
    unittest.main()
