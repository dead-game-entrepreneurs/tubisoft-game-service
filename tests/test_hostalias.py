"""Docker-host clients are reported at the advertised address."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ubisoft import hostalias, nat, service

ROUTES = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
          "eth0\t00000000\t017100CB\t0003\t0\t0\t0\t00000000\n"
          "eth0\t007100CB\t00000000\t0001\t0\t0\t0\t0000FFFF\n")


class HostAliasTests(unittest.TestCase):
    def test_default_gateway_is_read_from_the_route_table(self):
        self.assertEqual(hostalias.default_gateway(ROUTES), "203.0.113.1")
        self.assertIsNone(hostalias.default_gateway("Iface\tDestination\tGateway\n"))

    def test_only_the_gateway_is_rewritten(self):
        alias = hostalias.HostAlias("203.0.113.1", "198.51.100.10")
        self.assertEqual(alias("203.0.113.1"), "198.51.100.10")
        self.assertEqual(alias("198.51.100.20"), "198.51.100.20")

    def test_configuration_modes(self):
        with TemporaryDirectory() as directory:
            routes = Path(directory, "route")
            routes.write_text(ROUTES)
            marker = Path(directory, "dockerenv")
            # Outside a container, auto does nothing.
            self.assertIsNone(hostalias.configure("auto", "198.51.100.10", route_path=str(routes),
                                                  container_marker=str(marker)))
            marker.write_text("")
            alias = hostalias.configure("auto", "198.51.100.10", route_path=str(routes),
                                        container_marker=str(marker))
            self.assertEqual(alias("203.0.113.1"), "198.51.100.10")
        self.assertIsNone(hostalias.configure("off", "198.51.100.10"))
        self.assertEqual(hostalias.configure("203.0.113.9", "198.51.100.10")("203.0.113.9"), "198.51.100.10")
        with self.assertRaises(ValueError):
            hostalias.configure("not-an-address", "198.51.100.10")
        with self.assertRaises(ValueError):
            service.configuration({"TUBISOFT_ADVERTISE_IP": "198.51.100.10", "TUBISOFT_HOST_ALIAS": "bad"})

    def test_compose_forwards_the_setting(self):
        # Compose reads .env only for interpolation; the service environment
        # must name the variable for it to reach the container.
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
        self.assertIn("TUBISOFT_HOST_ALIAS: ${TUBISOFT_HOST_ALIAS:-auto}", compose)

    def test_nat_reports_the_alias_but_replies_to_the_real_source(self):
        discovery = nat.Discovery("0.0.0.0/0", host_alias=hostalias.HostAlias("203.0.113.1", "198.51.100.10"))
        self.assertEqual(discovery.host_alias("203.0.113.1"), "198.51.100.10")


if __name__ == "__main__":
    unittest.main()
