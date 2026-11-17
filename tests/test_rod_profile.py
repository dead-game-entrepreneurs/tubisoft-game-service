"""187 sign-in: real GS crypto/sockets, synthetic accounts and records."""
import asyncio
from tempfile import TemporaryDirectory
import unittest

from ubisoft import router, service
from ubisoft.games.base import UnsupportedOperation
from ubisoft.games.registry import BIA, GR2, R6, ROD, XIII, bindings
from ubisoft.rooms import RoomRegistry

from test_games import GSClient, registration
from test_r6_profile import reserve_ports


class RodProfileTests(unittest.TestCase):
    def test_identity_and_native_sectionless_bootstrap(self):
        self.assertEqual((ROD.product, ROD.sdk_version, ROD.game_version),
                         ("RODPS2US", "RODPS21.0", "1.0"))
        self.assertEqual(ROD.bootstrap("192.0.2.1", 30040, 30041, "192.0.2.1", 30042),
                         b"routerip0=192.0.2.1\r\nrouterport0=30040\r\nrouterlauncherport0=30041\r\n"
                         b"natserverip0=192.0.2.1\r\nnatserverport0=30042\r\n")

    def test_news_accepts_native_languages_and_configured_text(self):
        configured = bindings({"TUBISOFT_GAMES": "rod", "TUBISOFT_ROD_NEWS_1": "First item",
                               "TUBISOFT_ROD_NEWS_2": "Second item"})[0].profile
        for language in ("en", "fr", "es", "de", "it"):
            self.assertEqual(configured.login_notices([language]), ["First item", "Second item"])
        for fields in ([], ["en", "extra"], [b"en"], [["en"]], ["unknown"]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ROD.login_notices(fields)

    def test_discovery_rejects_other_titles_and_has_the_native_folder_shape(self):
        ROD.discovery_login([ROD.product])
        for fields in ([], [XIII.product], [GR2.product], [R6.product], [BIA.product],
                       [ROD.product, "extra"], [ROD.product.encode()]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ROD.discovery_login(fields)
        self.assertEqual(ROD.parent_lobby(), [
            "0", "187 Ride or Die", "1", "1", "0", "0", "1", "", "",
            "RODPS2US", b"", "0", "100", "0"])

    def test_187_uses_lobby_rooms_and_keeps_other_title_contracts_separate(self):
        self.assertTrue(ROD.supports_rooms and ROD.lobby_create and ROD.lobby_members)
        self.assertEqual(ROD.proxy_services, ())
        registry = RoomRegistry()
        with self.assertRaises(ValueError):
            registry.register("Fixture", "192.0.2.2", registration(ROD.product), ROD)
        with self.assertRaises(UnsupportedOperation):
            ROD.launcher_groups()
        with self.assertRaises(ValueError):
            ROD.parent_player_info([bytes(24)])
        self.assertEqual(registry.children(ROD.product), [])

    def test_opt_in_ports_do_not_change_existing_defaults(self):
        self.assertEqual([b.profile for b in bindings({})], [XIII])
        all_games = bindings({"TUBISOFT_GAMES": "xiii,gr2,r6,bia,rod"})
        self.assertEqual([b.profile for b in all_games], [XIII, GR2, R6, BIA, ROD])
        self.assertEqual((all_games[-1].router_port, all_games[-1].launcher_port,
                          all_games[-1].nat_port), (30040, 30041, 30042))
        with self.assertRaises(ValueError):
            bindings({"TUBISOFT_GAMES": "xiii,rod", "TUBISOFT_ROD_ROUTER_PORT": "30000"})


class RodSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_account_isolated_handoff_directory_and_product_catalog(self):
        with TemporaryDirectory() as directory:
            reservations, ports = reserve_ports(7)
            try:
                env = {"TUBISOFT_ADVERTISE_IP": "127.0.0.1", "TUBISOFT_BIND_IP": "127.0.0.1",
                       "TUBISOFT_STATE_DIR": directory, "TUBISOFT_GAMES": "xiii,rod",
                       "TUBISOFT_HTTP_PORT": str(ports[6])}
                for prefix, selected in (("TUBISOFT_", ports[:3]), ("TUBISOFT_ROD_", ports[3:6])):
                    for role, port in zip(("ROUTER", "LAUNCHER", "NAT"), selected):
                        env[prefix + role + "_PORT"] = str(port)
                args = service.configuration(env)
            finally:
                for sock in reservations:
                    sock.close()
            task = asyncio.create_task(service.serve(args))
            clients = []
            try:
                for attempt in range(100):
                    if task.done():
                        await task
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", ports[6])
                        writer.write(b"GET /gsinit.php?dp=RODPS2US HTTP/1.1\r\n"
                                     b"HOST: gsconnect.ubisoft.com\r\n\r\n")
                        await writer.drain()
                        response = await asyncio.wait_for(reader.read(), 2)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except ConnectionRefusedError:
                        if attempt == 99:
                            raise
                        await asyncio.sleep(0.01)
                self.assertIn(b"200 OK", response)
                body = response.split(b"\r\n\r\n", 1)[1]
                self.assertEqual(body, ROD.bootstrap("127.0.0.1", *ports[3:5], "127.0.0.1", ports[5]))
                private = router.load_key(args.private_key)

                async def connect(port):
                    client = GSClient(*await asyncio.open_connection("127.0.0.1", port))
                    clients.append(client)
                    await client.handshake(private)
                    return client

                xiii = await connect(ports[0])
                await xiii.send(1, ["1.0", "SharedFixture", "fixture-only", "", "", "", ""], encrypted=True)
                header, fields = await xiii.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x01"]))
                rod = await connect(ports[3])
                await rod.send(0x66, ["SharedFixture", "fixture-only", ROD.sdk_version, b"\1"], encrypted=True)
                header, fields = await rod.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x66"]))
                await rod.send(0x5d)
                _, fields = await rod.receive()
                self.assertEqual(fields, [b"\x5d", ["127.0.0.1", ports[3].to_bytes(4, "little")]])
                wrong = await connect(ports[0])
                await wrong.send(0x4d, ["SharedFixture"])
                self.assertEqual(await asyncio.wait_for(wrong.reader.read(), 2), b"")
                wait = await connect(ports[3])
                await wait.send(0x4d, ["SharedFixture"])
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x4d"]))
                await wait.send(0xd1, ["21", [ROD.product]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["38", ["21"]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["53", ["1", "256", ["0"], [ROD.parent_lobby()]]])
                await wait.send(0x3a)
                header, fields = await wait.receive()
                self.assertEqual((header.message_type, fields), (0x3a, []))
                # 109 re-adds the parent, even with no hosted games. There
                # is no synthetic room and no guessed 109-success envelope.
                await wait.send(0xd1, ["109", [ROD.product]])
                _, fields = await wait.receive()
                self.assertEqual(fields, ["53", ["1", "256", ["0"], [ROD.parent_lobby()]]])
                # A foreign product is rejected at this listener.
                await wait.send(0xd1, ["109", [XIII.product]])
                self.assertEqual(await asyncio.wait_for(wait.reader.read(), 2), b"")
                # XIII still authenticates the shared account afterward.
                xiii = await connect(ports[0])
                await xiii.send(0x66, ["SharedFixture", "fixture-only", XIII.sdk_version, b"\1"], encrypted=True)
                header, fields = await xiii.receive()
                self.assertEqual((header.message_type, fields), (0x26, [b"\x66"]))
            finally:
                for client in clients:
                    client.writer.close()
                for client in clients:
                    await client.writer.wait_closed()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
