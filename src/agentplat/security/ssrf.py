"""SSRF defence for agent-controlled URLs.

An agent that can fetch URLs can be tricked (by a user or by injected content)
into fetching internal endpoints: cloud metadata (169.254.169.254), admin panels
on localhost, databases on the VPC. Rules:

  * only http/https, only allowed ports, no credentials in the URL
  * resolve DNS ourselves and reject if ANY resolved address is non-public
  * connect to the exact IP we validated (defeats DNS rebinding: a second
    lookup can't swap in 127.0.0.1 after the check)
  * follow redirects manually and re-validate every hop
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]

BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT
        "169.254.0.0/16",  # link-local incl. cloud metadata
        "192.0.0.0/24",
        "198.18.0.0/15",
        "fd00::/8",
        "fe80::/10",
    )
]


class SSRFError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    scheme: str
    host: str
    port: int
    ip: str
    path: str  # path + query

    @property
    def connect_url(self) -> str:
        host = f"[{self.ip}]" if ":" in self.ip else self.ip
        return f"{self.scheme}://{host}:{self.port}{self.path}"


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(raw: str) -> bool:
    ip = ipaddress.ip_address(raw.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return not any(ip in net for net in BLOCKED_NETWORKS)


async def validate_url(
    url: str,
    resolver: Resolver = system_resolver,
    *,
    allowed_ports: frozenset[int] = frozenset({80, 443}),
) -> ValidatedTarget:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SSRFError(f"scheme {scheme or '(none)'!r} not allowed")
    if parts.username or parts.password:
        raise SSRFError("credentials in URL not allowed")
    host = parts.hostname
    if not host:
        raise SSRFError("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise SSRFError("invalid port") from exc
    if port not in allowed_ports:
        raise SSRFError(f"port {port} not allowed")

    try:  # literal IP (also catches decimal/hex forms normalised by ipaddress)
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        if host.isdigit() or host.lower().startswith("0x"):
            raise SSRFError("numeric host encodings not allowed") from None
        try:
            addresses = await resolver(host, port)
        except OSError as exc:
            raise SSRFError(f"cannot resolve {host}") from exc
    if not addresses:
        raise SSRFError(f"no addresses for {host}")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise SSRFError(f"{host} resolves to non-public address {bad[0]}")

    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    return ValidatedTarget(scheme, host, port, addresses[0], path)
