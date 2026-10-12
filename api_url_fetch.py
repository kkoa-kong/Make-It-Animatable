from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit


class DownloadError(ValueError):
    """A remote input URL is invalid or could not be downloaded safely."""



class DownloadUnavailable(DownloadError):
    """A safe URL could not be reached or its response could not be read."""

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5
_CHUNK_BYTES = 64 * 1024
# Opt-in for deployments with a trusted TUN fake-IP gateway. Never grant this
# exception to arbitrary hostnames, literal IP URLs, or other private networks.
_TRUSTED_EGRESS_HOSTS = frozenset(
    host.strip().lower().rstrip(".")
    for host in os.getenv("MIA_TRUSTED_EGRESS_HOSTS", "").split(",")
    if host.strip()
)
_TUN_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _connect_public(addresses: list[str], port: int, timeout: float) -> socket.socket:
    last_error: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout)
        except OSError as exc:
            last_error = exc
    raise DownloadUnavailable(f"could not connect to the public input host: {last_error}")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, addresses: list[str], timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._resolved_addresses = addresses

    def connect(self) -> None:
        self.sock = _connect_public(self._resolved_addresses, self.port, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, addresses: list[str], timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._resolved_addresses = addresses

    def connect(self) -> None:
        sock = _connect_public(self._resolved_addresses, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _parse_and_resolve(raw_url: str, timeout: float) -> tuple[object, list[str]]:
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise DownloadError("invalid input URL") from exc

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise DownloadError("input URL must use http or https and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise DownloadError("credentials in input URLs are not allowed")
    if parsed.fragment:
        raise DownloadError("URL fragments are not allowed")

    host = parsed.hostname.rstrip(".")
    try:
        ipaddress.ip_address(host)
        trusted_gateway_host = False
    except ValueError:
        trusted_gateway_host = host.lower() in _TRUSTED_EGRESS_HOSTS
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadUnavailable(f"cannot resolve input URL host: {exc}") from exc
    if not answers:
        raise DownloadError("input URL host has no addresses")

    addresses: list[str] = []
    for answer in answers:
        address = answer[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DownloadError("input URL host resolved to an invalid address") from exc
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        trusted_fake_ip = trusted_gateway_host and ip in _TUN_FAKE_IP_NETWORK
        if not trusted_fake_ip and (
            not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local
        ):
            raise DownloadError("input URL cannot resolve to a non-public IP address")
        addresses.append(str(ip))

    # Pin every validated address. Trying IPv4 after an unreachable IPv6
    # address preserves DNS safety without depending on host Happy Eyeballs.
    return parsed, list(dict.fromkeys(addresses))


def _connection(parsed: object, addresses: list[str], timeout: float) -> http.client.HTTPConnection:
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    if scheme == "https":
        return _PinnedHTTPSConnection(host, port, addresses, timeout)
    return _PinnedHTTPConnection(host, port, addresses, timeout)


def download_url(
    raw_url: str,
    destination: str | Path,
    *,
    max_bytes: int,
    timeout_seconds: float = 30,
    allowed_extensions: set[str] | frozenset[str],
    fallback_extension: str | None = None,
) -> Path:
    """Download a public HTTP(S) URL, pinning validated DNS and bounding bytes."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    try:
        initial = urlsplit(raw_url)
    except ValueError as exc:
        raise DownloadError("invalid input URL") from exc
    extension = Path(initial.path).suffix.lower()
    if not extension:
        extension = (fallback_extension or "").lower()
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise DownloadError(f"input URL must identify a supported file ({allowed})")

    current_url = raw_url
    target_dir = Path(destination)
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="mia-input-", suffix=extension, dir=target_dir)
    temp_path = Path(temp_name)
    total = 0

    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            parsed, addresses = _parse_and_resolve(current_url, timeout_seconds)
            connection = _connection(parsed, addresses, timeout_seconds)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            try:
                try:
                    connection.request(
                        "GET",
                        path,
                        headers={"User-Agent": "MIA-V2-API/1.0", "Accept": "*/*", "Connection": "close"},
                    )
                    response = connection.getresponse()
                except (OSError, http.client.HTTPException) as exc:
                    raise DownloadUnavailable(f"failed to download input URL: {exc}") from exc
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    response.read(1024)
                    if not location or redirect_count == _MAX_REDIRECTS:
                        raise DownloadError("input URL has too many or invalid redirects")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status < 200 or response.status >= 300:
                    raise DownloadError(f"input URL returned HTTP {response.status}")
                content_length = response.getheader("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > max_bytes:
                            raise DownloadError(f"input exceeds the {max_bytes}-byte download limit")
                    except ValueError as exc:
                        raise DownloadError("input URL returned an invalid Content-Length") from exc

                with os.fdopen(fd, "wb") as output:
                    while True:
                        chunk = response.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise DownloadError(f"input exceeds the {max_bytes}-byte download limit")
                        output.write(chunk)
                return temp_path
            finally:
                connection.close()

        raise DownloadError("input URL has too many redirects")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
