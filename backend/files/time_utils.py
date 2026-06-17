"""Time utilities. All timestamps are UTC.

Per the roadmap Part IV.C schema rule: never mix local and UTC ambiguously.
This module exposes a single source of truth for "now" and a clock-drift
check used by the bootstrap health check.
"""

from __future__ import annotations

import socket
import struct
import time
from datetime import datetime, timezone


def now_utc() -> datetime:
    """Timezone-aware UTC ``datetime``. Use this everywhere internally."""
    return datetime.now(tz=timezone.utc)


def now_utc_iso() -> str:
    """ISO-8601 UTC timestamp string with millisecond resolution."""
    return now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def measure_clock_drift_ms(ntp_server: str = "pool.ntp.org", timeout_s: float = 2.0) -> int:
    """Return the absolute drift between local clock and an NTP reference, in milliseconds.

    Uses raw SNTP query (RFC 4330) to avoid additional dependencies. If the
    NTP server is unreachable the function raises; callers should treat that
    as a soft warning rather than a hard failure unless policy requires NTP.
    """
    NTP_PORT = 123
    NTP_PACKET_FORMAT = "!12I"
    NTP_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client.settimeout(timeout_s)
        data = b"\x1b" + 47 * b"\0"  # LI=0, VN=3, Mode=3 (client)
        send_time = time.time()
        client.sendto(data, (ntp_server, NTP_PORT))
        data, _ = client.recvfrom(1024)
        recv_time = time.time()
    finally:
        client.close()

    if not data:
        raise RuntimeError("Empty NTP response")

    unpacked = struct.unpack(NTP_PACKET_FORMAT, data[:48])
    # Transmit timestamp from server.
    transmit_seconds = unpacked[10] - NTP_DELTA
    transmit_fraction = unpacked[11] / 2**32
    server_time = transmit_seconds + transmit_fraction
    # One-way delay approximation: half the round-trip.
    rtt = recv_time - send_time
    estimated_local_when_server_sent = recv_time - (rtt / 2)
    drift_s = estimated_local_when_server_sent - server_time
    return int(abs(drift_s) * 1000)


__all__ = ["now_utc", "now_utc_iso", "measure_clock_drift_ms"]
