"""
Statistics tracker for traffic test tool.
Tracks bytes sent/received and calculates bps over intervals.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class IntervalStats:
    """Stats snapshot for one interval."""
    interval_bytes_sent: int = 0
    interval_bytes_recv: int = 0
    bps_sent: float = 0.0
    bps_recv: float = 0.0
    total_bytes_sent: int = 0
    total_bytes_recv: int = 0
    elapsed_sec: float = 0.0


class StatsTracker:
    """
    Thread-safe statistics tracker.

    Call add_sent() / add_recv() from IO threads.
    Call snapshot() at each interval to get stats and reset counters.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._last_snapshot_time = self._start_time

        self._total_sent: int = 0
        self._total_recv: int = 0
        self._interval_sent: int = 0
        self._interval_recv: int = 0

    # ------------------------------------------------------------------
    # IO thread calls
    # ------------------------------------------------------------------

    def add_sent(self, n: int) -> None:
        with self._lock:
            self._total_sent += n
            self._interval_sent += n

    def add_recv(self, n: int) -> None:
        with self._lock:
            self._total_recv += n
            self._interval_recv += n

    # ------------------------------------------------------------------
    # Interval snapshot (called from stats/logger thread)
    # ------------------------------------------------------------------

    def snapshot(self) -> IntervalStats:
        """
        Return stats for the elapsed interval and reset interval counters.
        """
        now = time.monotonic()
        with self._lock:
            interval_sec = now - self._last_snapshot_time
            if interval_sec <= 0:
                interval_sec = 1e-9  # avoid division by zero

            bps_sent = (self._interval_sent * 8) / interval_sec
            bps_recv = (self._interval_recv * 8) / interval_sec

            snap = IntervalStats(
                interval_bytes_sent=self._interval_sent,
                interval_bytes_recv=self._interval_recv,
                bps_sent=bps_sent,
                bps_recv=bps_recv,
                total_bytes_sent=self._total_sent,
                total_bytes_recv=self._total_recv,
                elapsed_sec=now - self._start_time,
            )

            # Reset interval counters
            self._interval_sent = 0
            self._interval_recv = 0
            self._last_snapshot_time = now

        return snap

    def elapsed(self) -> float:
        """Return elapsed seconds since tracker creation."""
        return time.monotonic() - self._start_time

    def totals(self) -> tuple[int, int]:
        """Return (total_bytes_sent, total_bytes_recv)."""
        with self._lock:
            return self._total_sent, self._total_recv
