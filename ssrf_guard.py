"""Shared SSRF / internal-target guard (security review H2, extended) + a
name-resolution helper with a hard timeout.

A monitor is a standing invitation to fetch/connect to whatever host a user
types, on a schedule, forever. This blocks targets that resolve to addresses a
monitor must never reach: RFC1918 private, link-local (incl. the cloud metadata
endpoint 169.254.169.254), carrier-grade NAT (100.64.0.0/10, which
`ipaddress.is_private` does NOT flag before Python 3.13), and other
reserved/multicast/unspecified ranges.

Loopback (127.0.0.0/8, ::1) is deliberately exempt: monitoring a service on the
same box the scheduler runs on is a legitimate, already-tested use case.

Lives in its own module so both monitor_engine (HTTP/TCP/DNS checks) and
ssl_check can import it -- monitor_engine imports ssl_check, so the guard can't
live in monitor_engine without an import cycle.
"""
import concurrent.futures
import ipaddress
import socket

# Carrier-grade NAT / shared address space -- not covered by is_private on
# Python < 3.13, but a monitor must never be pointed at it.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

DNS_TIMEOUT_SEC = 5  # hard wall-clock cap on a single name resolution

# socket.getaddrinfo takes no timeout and ignores socket.setdefaulttimeout, so
# resolutions run here and are abandoned if they blow the cap -- that's what
# keeps one hung lookup from stalling a whole scheduler tick.
# ponytail: abandoned-thread ceiling -- a flood of simultaneously-hanging names
# could pile up threads; fine at this scale (tens of monitors), swap for an
# async resolver if it ever isn't.
_RESOLVER = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="dns")


class DNSTimeout(Exception):
    """Raised when a name doesn't resolve within the cap."""


def resolve(hostname, timeout=DNS_TIMEOUT_SEC):
    """Resolve `hostname` to a set of IP strings, capped at `timeout` seconds.
    Empty set if the name doesn't exist (gaierror). Raises DNSTimeout if the
    lookup exceeds the cap -- the underlying getaddrinfo thread is abandoned and
    ends on its own when the OS resolver finally returns."""
    future = _RESOLVER.submit(socket.getaddrinfo, hostname, None)
    try:
        infos = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise DNSTimeout(hostname)
    except socket.gaierror:
        return set()
    return {info[4][0] for info in infos}


def _ip_is_blocked(ip):
    if ip.is_loopback:
        return False
    if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    return ip in _CGNAT if ip.version == 4 else False


def addrs_blocked(addrs):
    """True if any address in `addrs` (IP strings) is one a monitor must not
    reach. Used with resolve() by callers that need to bound DNS themselves."""
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_blocked(ip):
            return True
    return False


def is_blocked_host(hostname):
    """True if `hostname` resolves to any blocked address. Unresolvable or
    slow-to-resolve -> False (the check itself then reports the DNS
    failure/timeout). Callers that must bound DNS use resolve() directly."""
    try:
        return addrs_blocked(resolve(hostname))
    except DNSTimeout:
        return False
