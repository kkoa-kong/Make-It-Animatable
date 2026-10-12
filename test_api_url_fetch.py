import socket
import unittest
from unittest.mock import patch

from api_url_fetch import DownloadError, _parse_and_resolve


class TrustedGatewayTests(unittest.TestCase):
    def resolve(self, url, addresses, trusted=()):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]
        with patch("api_url_fetch._TRUSTED_EGRESS_HOSTS", frozenset(trusted)), patch(
            "api_url_fetch.socket.getaddrinfo", return_value=answers
        ):
            return _parse_and_resolve(url, 1)

    def test_fake_ip_requires_explicit_trusted_hostname(self):
        with self.assertRaises(DownloadError):
            self.resolve("https://assets.example/mesh.glb", ["198.19.0.70"])
        _, addresses = self.resolve(
            "https://ASSETS.example/mesh.glb", ["198.19.0.70"], ["assets.example"]
        )
        self.assertEqual(addresses, ["198.19.0.70"])

    def test_hostname_exception_does_not_match_suffix_or_subdomain(self):
        for host in ("assets.example.attacker.test", "other.assets.example"):
            with self.subTest(host=host), self.assertRaises(DownloadError):
                self.resolve("https://" + host + "/mesh.glb", ["198.19.0.70"], ["assets.example"])

    def test_literal_fake_ip_cannot_be_trusted(self):
        with self.assertRaises(DownloadError):
            self.resolve("https://198.19.0.70/mesh.glb", ["198.19.0.70"], ["198.19.0.70"])

    def test_trusted_hostname_still_rejects_internal_addresses(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1"):
            with self.subTest(address=address), self.assertRaises(DownloadError):
                self.resolve("https://assets.example/mesh.glb", [address], ["assets.example"])

    def test_one_unsafe_dns_answer_rejects_entire_host(self):
        with self.assertRaises(DownloadError):
            self.resolve(
                "https://assets.example/mesh.glb",
                ["198.19.0.70", "10.0.0.1"],
                ["assets.example"],
            )


if __name__ == "__main__":
    unittest.main()
