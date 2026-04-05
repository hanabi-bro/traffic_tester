"""
IP utility functions for traffic test tool.
Provides source IP detection and common socket helpers.
"""

from __future__ import annotations

import socket


def get_source_ip(dest_ip: str, dest_port: int = 80) -> str:
    """
    Determine the local IP address used to reach dest_ip.

    Uses UDP socket trick: connect() on a UDP socket sets the local address
    without sending any packets. Works on Windows and Linux.

    Returns "0.0.0.0" if detection fails.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((dest_ip, dest_port))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


def resolve_host(host: str) -> str:
    """
    Resolve hostname to IP address string.
    Returns the input unchanged if it is already an IP.
    Raises socket.gaierror on failure.
    """
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        raise


def is_ipv6(addr: str) -> bool:
    """Return True if addr is an IPv6 address."""
    try:
        socket.inet_pton(socket.AF_INET6, addr)
        return True
    except (socket.error, OSError):
        return False


def get_socket_family(addr: str) -> socket.AddressFamily:
    """Return AF_INET6 if addr is IPv6, else AF_INET."""
    return socket.AF_INET6 if is_ipv6(addr) else socket.AF_INET


def tcp_no_delay(sock: socket.socket) -> None:
    """Disable Nagle algorithm for lower latency."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


def set_keepalive(sock: socket.socket, idle: int = 10, interval: int = 5, count: int = 3) -> None:
    """
    Enable TCP keepalive on sock.
    Parameters are best-effort; not all options are available on all platforms.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        import platform
        if platform.system() == "Linux":
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
    except (AttributeError, OSError):
        pass  # Best-effort; Windows has different API
