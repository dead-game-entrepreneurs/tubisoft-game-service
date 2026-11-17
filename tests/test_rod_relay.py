"""187 relay admission, isolation, lifecycle and real GS/NAT/UDP forwarding."""
import asyncio
import ipaddress
import socket
import struct
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase

from ubisoft import data, gsxor, service
from ubisoft.frame import GSHeader, HEADER_SIZE
from ubisoft.games.registry import ROD
from ubisoft.rod_relay import Config, Relay, MAX_PACKET, PACKETS_PER_SECOND
from ubisoft.rooms import LobbyMember, RoomRegistry
from ubisoft.srp_probe import HEADER, WINDOW, IDENT, SYN, ACK, FIN, finish_datagram
import test_rod_rooms as rod_fixtures
from test_rod_rooms import create_fields, player_info, session_info


class Sink:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, packet, peer):
        self.sent.append((packet, peer))

    def close(self):
        self.closed = True


class RelayConfigTests(TestCase):
    def test_default_off_and_explicit_bounded_nonoverlapping_pool(self):
        base = {"TUBISOFT_ADVERTISE_IP": "192.0.2.1", "TUBISOFT_GAMES": "xiii,rod"}
        self.assertIsNone(service.configuration(base).rod_relay_config)
        enabled = base | {"TUBISOFT_ROD_RELAY_MODE": "relay"}
        self.assertEqual(service.configuration(enabled).rod_relay_config,
                         Config("0.0.0.0", "192.0.2.1", 30400, 30431))
        for extra in ({"TUBISOFT_ROD_RELAY_MODE": "adaptive"}, {"TUBISOFT_GAMES": "xiii"},
                      {"TUBISOFT_ROD_RELAY_FIRST_PORT": "30040"},
                      {"TUBISOFT_ROD_RELAY_LAST_PORT": "65536"},
                      {"TUBISOFT_ROD_RELAY_LAST_PORT": "30528"},
                      {"TUBISOFT_ROD_RELAY_ADVERTISE_IP": "0.0.0.0"},
                      {"TUBISOFT_ROD_RELAY_ADVERTISE_IP": "127.0.0.1"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                service.configuration(enabled | extra)


class RelayAdmissionTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = RoomRegistry()
        self.clock = [time.monotonic()]
        self.relay = Relay(Config("127.0.0.1", "203.0.113.9", 30400, 30403),
                           self.registry, now=lambda: self.clock[0])
        self.relay.transports = {p: Sink() for p in range(30400, 30404)}
        self.addCleanup(self.relay.close)
        self.host = self.member("Host", "192.0.2.10")
        self.guest = self.member("Guest", "192.0.2.20")

    def member(self, name, address):
        member = LobbyMember(name, ROD.product, address, "", lambda event: None)
        self.registry.lobby_join(object(), member)
        return member

    def proof(self, member, port):
        request = SimpleNamespace(username=member.name, product=ROD.product, sdk=ROD.sdk_version)
        return self.relay.observe(request, (member.address, port))

    def admitted(self, member, port):
        address, relayed = self.proof(member, port)
        member.info = player_info(address, relayed)
        self.relay.confirm(member, member.info)
        return relayed

    def room(self, *members):
        room = self.registry.register(self.host.name.casefold(), self.host.address, create_fields(), ROD)
        self.registry.activate(room.room_id, room.owner, room.peer_ip, ROD.product)
        for member in members:
            member.group = str(room.room_id)
            room.members.add((member.address, member.name.casefold()))
        return room

    async def test_tcp_confirmation_and_same_room_required_then_opaque_bidirectional(self):
        hp = self.admitted(self.host, 41000)
        address, gp = self.proof(self.guest, 42000)
        self.relay.forward(hp, b"unconfirmed", (self.guest.address, 42000))
        self.assertFalse(self.relay.transports[gp].sent)
        self.guest.info = player_info(address, gp)
        self.relay.confirm(self.guest, self.guest.info)
        self.relay.forward(hp, b"parent only", (self.guest.address, 42000))
        self.assertFalse(self.relay.transports[gp].sent)
        room = self.room(self.host, self.guest)
        self.relay.room(room)
        self.assertEqual(room.peer_ip, self.host.address)  # ownership remains real
        self.assertEqual(room.group_fields()[18:20], [address, address])
        self.assertEqual(struct.unpack_from("<H", room.info, 32)[0], hp)
        packet = bytes(range(256)) * 8
        self.relay.forward(hp, packet, (self.guest.address, 42000))
        # The host has not opened a socket path to the guest's relay yet.
        self.assertFalse(self.relay.transports[gp].sent)
        self.relay.forward(gp, b"", (self.host.address, 41000))
        self.assertEqual(self.relay.transports[gp].sent, [(packet, (self.host.address, 41000))])
        self.assertEqual(self.relay.transports[hp].sent, [(b"", (self.guest.address, 42000))])
        self.guest.group = "1"
        self.relay.forward(hp, b"after leave", (self.guest.address, 42000))
        self.assertEqual(len(self.relay.transports[gp].sent), 1)

    async def test_spoofed_claim_same_nat_cannot_replace_confirmed_mapping(self):
        hp = self.admitted(self.host, 41000)
        # Another token request can claim the same name/IP, but only the
        # owner's authenticated TCP publication can confirm that candidate.
        _, pending = self.proof(self.host, 49999)
        with self.assertRaises(ValueError):
            self.relay.confirm(self.guest, player_info(self.relay.config.address, pending))
        self.assertTrue(self.relay.leases[hp].confirmed)
        self.assertFalse(self.relay.leases[pending].confirmed)
        self.admitted(self.guest, 42000)
        self.room(self.host, self.guest)
        self.relay.forward(hp, b"spoofed port", (self.guest.address, 42001))
        self.relay.forward(hp, b"unconfirmed claim", (self.host.address, 49999))
        self.assertFalse(any(s.sent for s in self.relay.transports.values()))

    async def test_shared_nat_distinct_ports_and_per_viewer_member_addresses(self):
        self.guest.address = self.host.address
        hp, gp = self.admitted(self.host, 41000), self.admitted(self.guest, 42000)
        self.room(self.host, self.guest)
        self.relay.forward(gp, b"permit", (self.host.address, 41000))
        self.relay.forward(hp, b"shared NAT", (self.guest.address, 42000))
        self.assertEqual(self.relay.transports[gp].sent, [(b"shared NAT", (self.host.address, 41000))])
        event = ROD.lobby_member_event(self.host)
        viewed = self.relay.member_event(event, self.guest.name)
        self.assertEqual(viewed[1][3:5], [self.relay.config.address] * 2)
        self_view = self.relay.member_event(event, self.host.name.upper())
        self.assertEqual(ROD.player_port(self_view[1][5]), hp)
        self.assertEqual(self_view[1][5][4:8], ipaddress.IPv4Address(self.host.address).packed[::-1])
        self.assertEqual(event[1][5], self.host.info)
        self.assertEqual(self.host.address, "192.0.2.10")
        self.assertEqual(event[1][3:5], [self.host.address] * 2)

    async def test_self_nat_view_prevents_false_same_nat_and_round_trips_canonically(self):
        hp, gp = self.admitted(self.host, 41000), self.admitted(self.guest, 42000)
        own = self.relay.member_event(ROD.lobby_member_event(self.host), self.host.name)
        other = self.relay.member_event(ROD.lobby_member_event(self.guest), self.host.name)
        own_info, other_info = (ROD.parent_player_info([event[1][5]]) for event in (own, other))
        self.assertNotEqual(own_info[4:8], other_info[4:8])
        # An equal pair triggers native 0x00463560's local-address/3658
        # announcement, overriding a successfully negotiated relay endpoint.
        self.assertEqual(other_info, self.guest.info)
        published = bytearray(own_info)
        published[0] = 2
        canonical = self.relay.confirm(self.host, bytes(published))
        self.assertEqual(canonical, bytes([2]) + self.host.info[1:])
        with self.assertRaises(ValueError):
            self.relay.confirm(self.host, player_info(self.guest.address, hp))
        _, pending = self.proof(self.host, 49999)
        with self.assertRaises(ValueError):
            self.relay.confirm(self.host, player_info(self.host.address, pending))

    async def test_another_room_cannot_use_a_confirmed_endpoint(self):
        hp = self.admitted(self.host, 41000)
        self.admitted(self.guest, 42000)
        self.room(self.host, self.guest)
        other = self.member("Other", "192.0.2.30")
        op = self.admitted(other, 43000)
        room = self.registry.register(other.name.casefold(), other.address, create_fields(), ROD)
        self.registry.activate(room.room_id, room.owner, room.peer_ip, ROD.product)
        other.group = str(room.room_id)
        room.members.add((other.address, other.name.casefold()))
        self.relay.forward(op, b"foreign room", (self.host.address, 41000))
        self.relay.forward(hp, b"foreign room", (other.address, 43000))
        self.assertFalse(any(s.sent for s in self.relay.transports.values()))

    async def test_room_removal_size_rate_expiry_and_disconnect_cleanup(self):
        hp, gp = self.admitted(self.host, 41000), self.admitted(self.guest, 42000)
        room = self.room(self.host, self.guest)
        self.relay.forward(gp, b"permit", (self.host.address, 41000))
        self.relay.forward(hp, bytes(MAX_PACKET + 1), (self.guest.address, 42000))
        for _ in range(PACKETS_PER_SECOND + 5):
            self.relay.forward(hp, b"game", (self.guest.address, 42000))
        self.assertEqual(len(self.relay.transports[gp].sent), PACKETS_PER_SECOND)
        self.clock[0] += 1
        self.relay.forward(hp, b"refilled", (self.guest.address, 42000))
        self.assertEqual(len(self.relay.transports[gp].sent), PACKETS_PER_SECOND + 1)
        self.registry.remove(room.room_id, room.owner, room.peer_ip)
        self.relay.forward(hp, b"room removed", (self.guest.address, 42000))
        self.assertEqual(len(self.relay.transports[gp].sent), PACKETS_PER_SECOND + 1)
        stranger = self.member("Pending", "192.0.2.30")
        _, pending = self.proof(stranger, 43000)
        self.clock[0] += 31
        self.relay.sweep()
        self.assertNotIn(pending, self.relay.leases)
        # The live owner's waiting room mapping outlives a NAT-control FIN.
        self.assertIn(hp, self.relay.leases)
        for connection, member in list(self.registry._lobby.items()):
            if member is self.guest:
                self.registry.lobby_leave(connection)
        self.relay.sweep()
        self.assertNotIn(gp, self.relay.leases)
        self.assertGreater(self.relay.reuse_after[gp], self.clock[0])

    async def test_startup_queue_is_bounded_expires_and_does_not_survive_room_changes(self):
        hp, gp = self.admitted(self.host, 41000), self.admitted(self.guest, 42000)
        self.room(self.host, self.guest)
        for index in range(20):
            self.relay.forward(hp, bytes([index]), (self.guest.address, 42000))
        self.assertFalse(self.relay.transports[gp].sent)
        self.relay.forward(gp, b"permit", (self.host.address, 41000))
        self.assertEqual([p for p, _ in self.relay.transports[gp].sent],
                         [bytes([i]) for i in range(8)])
        self.relay.membership_changed(self.guest)
        self.relay.transports[gp].sent.clear()
        self.relay.forward(hp, b"expired", (self.guest.address, 42000))
        self.clock[0] += 6
        self.relay.forward(gp, b"permit", (self.host.address, 41000))
        self.assertFalse(self.relay.transports[gp].sent)
        self.relay.forward(hp, b"fresh", (self.guest.address, 42000))
        self.assertEqual(self.relay.transports[gp].sent, [(b"fresh", (self.host.address, 41000))])
        self.relay.membership_changed(self.guest)
        self.relay.forward(hp, b"left room", (self.guest.address, 42000))
        self.relay.membership_changed(self.guest)
        self.relay.forward(gp, b"new room permit", (self.host.address, 41000))
        self.assertEqual(len(self.relay.transports[gp].sent), 1)

    async def test_pool_bound_wrong_product_unknown_member_and_same_address(self):
        request = SimpleNamespace(username="missing", product=ROD.product, sdk=ROD.sdk_version)
        self.assertIsNone(self.relay.observe(request, (self.host.address, 1)))
        request.username, request.product = self.host.name, "OTHER"
        self.assertIsNone(self.relay.observe(request, (self.host.address, 1)))
        same = self.member("Local", self.relay.config.address)
        self.assertIsNone(self.proof(same, 41000))
        for port in range(41000, 41004):
            self.assertIsNotNone(self.proof(self.host, port))
        self.assertIsNone(self.proof(self.host, 41005))


class RodRelaySocketTests(IsolatedAsyncioTestCase):
    server_address = "127.0.0.1"
    client_address = "127.0.0.1"
    relay_address = "203.0.113.9"
    relay_send_address = "127.0.0.1"
    # Reuse the existing real-GS harness, not its direct-mode test cases.
    connect = rod_fixtures.RodLifecycleTests.connect
    receive = rod_fixtures.RodLifecycleTests.receive
    wait_login = rod_fixtures.RodLifecycleTests.wait_login
    stop = rod_fixtures.RodLifecycleTests.stop
    heartbeat = rod_fixtures.RodLifecycleTests.heartbeat

    async def asyncSetUp(self):
        reservation = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        reservation.close()
        while True:
            second = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                second.bind(("127.0.0.1", port + 1))
                second.close()
                break
            except OSError:
                second.close()
                port += 2
        self.relay_environment = {"TUBISOFT_ROD_RELAY_MODE": "relay",
                                  "TUBISOFT_ROD_RELAY_BIND_IP": "127.0.0.1",
                                  "TUBISOFT_ROD_RELAY_ADVERTISE_IP": "203.0.113.9",
                                  "TUBISOFT_ROD_RELAY_FIRST_PORT": str(port),
                                  "TUBISOFT_ROD_RELAY_LAST_PORT": str(port + 1)}
        await rod_fixtures.RodLifecycleTests.asyncSetUp(self)
        self.relay_ports = (port, port + 1)

    async def player(self, name):
        wait = await self.wait_login(name)
        await wait.send(0xd1, ["3", ["1"]])
        await wait.receive()
        lobby = await self.connect()
        await lobby.send(0xd2, [name, "1", self.client_address, "0.0.0.0", "0"])
        await lobby.receive()
        await lobby.send(0xd1, ["23", ["1", "", "448"]])
        await lobby.receive()
        await lobby.receive()
        while True:
            _, event = await lobby.receive()
            if event[0] == "50" and event[1][0] == name:
                break
        # Native SYN/token/probe flow over actual UDP sockets. No direct
        # call into the relay can create or admit this mapping.
        control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        game = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for sock in (control, game):
            sock.bind((self.client_address, 0))
            sock.setblocking(False)
            self.addCleanup(sock.close)
        loop, remote = asyncio.get_running_loop(), (self.server_address, self.ports[5])
        await loop.sock_sendto(control, finish_datagram(HEADER.pack(0, 0, 8, IDENT | SYN, 0, 0)
                                                       + WINDOW.pack(10, 0x1234, 0, 536)), remote)
        await asyncio.wait_for(loop.sock_recvfrom(control, 2048), 2)
        payload = gsxor.encrypt(data.encode(["1", ["7", name, ROD.product, ROD.sdk_version]]))
        frame = GSHeader(HEADER_SIZE + len(payload), 0, 1, 0xdd, 8, 2).encode() + payload
        await loop.sock_sendto(control, finish_datagram(HEADER.pack(0, 0x1234, len(frame), IDENT, 0, 0) + frame), remote)
        packet, _ = await asyncio.wait_for(loop.sock_recvfrom(control, 2048), 2)
        h = GSHeader.decode(packet[HEADER.size:HEADER.size + HEADER_SIZE])
        fields = data.decode(gsxor.decrypt(packet[HEADER.size + HEADER_SIZE:HEADER.size + h.size]))
        token = int(fields[1][1]).to_bytes(4, "big")
        await loop.sock_sendto(control, finish_datagram(HEADER.pack(0, 0x1234, 0, IDENT | ACK, 0xffff, 1)), remote)
        await loop.sock_sendto(game, token, remote)
        echoed, _ = await asyncio.wait_for(loop.sock_recvfrom(game, 2048), 2)
        self.assertEqual(echoed, token)
        packet, _ = await asyncio.wait_for(loop.sock_recvfrom(control, 2048), 2)
        h = GSHeader.decode(packet[HEADER.size:HEADER.size + HEADER_SIZE])
        fields = data.decode(gsxor.decrypt(packet[HEADER.size + HEADER_SIZE:HEADER.size + h.size]))
        self.assertEqual(str(ipaddress.IPv4Address(int(fields[1][1]))), self.relay_address)
        port = int(fields[1][2])
        self.assertIn(port, self.relay_ports)
        self.assertNotEqual(port, game.getsockname()[1])
        await lobby.send(0xd1, ["42", [player_info(self.relay_address, port)]])
        await self.receive(lobby, ["38", ["42"]])
        await loop.sock_sendto(control, finish_datagram(HEADER.pack(0, 0x1234, 0, IDENT | FIN, 1, 2)), remote)
        lobby.name, lobby.wait, lobby.game, lobby.relay_port = name, wait, game, port
        return lobby

    async def test_authenticated_nat_proofs_room_advertisement_and_two_way_udp_without_unlocks(self):
        host = await self.player("RelayHost")
        guest = await self.player("RelayGuest")
        await host.receive()  # Guest's parent member publication.
        await host.send(0xd1, ["12", create_fields("private", session_info(host.relay_port))])
        _, result = await host.receive()
        rid = result[1][1][0]
        for client in (host, guest):
            _, event = await client.receive()
            room = event[1][3][0]
            self.assertEqual(room[18:20], [self.relay_address, self.relay_address])
            self.assertEqual(struct.unpack_from("<H", room[10], 32)[0], host.relay_port)
        await guest.receive()  # Host room membership.
        await host.send(0xd1, ["24", [rid, "private", "448", "0", "1.0"]])
        await host.receive()
        _, own = await host.receive()
        self.assertEqual(own[0], "50")
        self.assertEqual(own[1][5], player_info(self.client_address, host.relay_port))
        await host.receive()
        loop = asyncio.get_running_loop()
        # Knowing a protected room's advertisement grants no relay access.
        await loop.sock_sendto(guest.game, b"before join", (self.relay_send_address, host.relay_port))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(host.game, 2048), .05)
        await guest.send(0xd1, ["24", [rid, "wrong", "448", "0", "1.0"]])
        await self.receive(guest, ["39", ["24", ["11", rid, ""]]])
        await guest.send(0xd1, ["24", [rid, "private", "448", "0", "1.0"]])
        await guest.receive()
        _, own = await guest.receive()
        self.assertEqual(own[0], "50")
        self.assertEqual(own[1][5], player_info(self.client_address, guest.relay_port))
        await guest.receive()
        _, joined = await host.receive()
        self.assertEqual(joined[1][5], player_info(self.relay_address, guest.relay_port))
        changed = bytearray(own[1][5])
        changed[0] = 2
        await guest.send(0xd1, ["42", [bytes(changed)]])
        await self.receive(guest, ["38", ["42"]])
        canonical = bytearray(player_info(self.relay_address, guest.relay_port))
        canonical[0] = 2
        await self.receive(host, ["66", [guest.name, bytes(canonical)]])
        # PCSX2 and endpoint-dependent firewalls only admit a peer after an
        # outbound datagram opens that exact relay path. Preserve early joins
        # until the host's native permit opens its receive path.
        await loop.sock_sendto(guest.game, b"early join", (self.relay_send_address, host.relay_port))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(host.game, 2048), .05)
        await loop.sock_sendto(host.game, b"permit", (self.relay_send_address, guest.relay_port))
        delivered, source = await asyncio.wait_for(loop.sock_recvfrom(guest.game, 2048), 2)
        self.assertEqual((delivered, source), (b"permit", (self.relay_send_address, host.relay_port)))
        delivered, source = await asyncio.wait_for(loop.sock_recvfrom(host.game, 2048), 2)
        self.assertEqual((delivered, source), (b"early join", (self.relay_send_address, guest.relay_port)))
        for sender, target in ((guest, host), (host, guest)):
            for packet in (b"opaque-game-fixture\x00" + bytes(range(128)), b""):
                await loop.sock_sendto(sender.game, packet, (self.relay_send_address, target.relay_port))
                delivered, source = await asyncio.wait_for(loop.sock_recvfrom(target.game, 2048), 2)
                self.assertEqual(delivered, packet)
                self.assertEqual(source, (self.relay_send_address, sender.relay_port))
        await self.heartbeat(guest.wait)
        await guest.send(0xd1, ["8", [rid]])
        await guest.receive()
        await loop.sock_sendto(guest.game, b"after leave", (self.relay_send_address, host.relay_port))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(host.game, 2048), .05)
