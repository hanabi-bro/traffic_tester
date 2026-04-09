"""
CSV Logger for traffic test tool.
Handles file creation per connection and stdout output.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

# CSV column order
CSV_FIELDS = [
    "datetime",
    "event_type",
    "proto",
    "server_ip",
    "server_port",
    "client_ip",
    "client_port",
    "elapsed_sec",
    "bytes_sent",
    "bytes_recv",
    "bps_sent",
    "bps_recv",
    "message",
    "pkt_seq",
    "pkt_loss",
    "pkt_ooo",
    "mode",
    "role",
]

# Event types
EVENT_CONNECT    = "CONNECT"
EVENT_DATA       = "DATA"
EVENT_DISCONNECT = "DISCONNECT"
EVENT_TIMEOUT    = "TIMEOUT"
EVENT_ERROR      = "ERROR"

# UDP-specific event types
EVENT_LOSS           = "LOSS"
EVENT_OUT_OF_ORDER   = "OUT_OF_ORDER"
EVENT_LATE_ARRIVAL   = "LATE_ARRIVAL"


def _log_dir(logdir: Path) -> Path:
    """Ensure log directory exists and return it."""
    logdir.mkdir(parents=True, exist_ok=True)
    return logdir


def _log_filename(
    proto: str,
    server_port: int,
    client_ip: str,
    server_ip: str,
    role: str = "client",
    ts: Optional[datetime] = None,
) -> str:
    """
    Build log filename.
    Format: yyyymmdd_HHMMSS_{role}_{proto}_{server_port}_{client_ip}_{server_ip}.log
    role: 'client' or 'server'
    IP colons (IPv6) are replaced with '-'.
    """
    if ts is None:
        ts = datetime.now()
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    c_ip = client_ip.replace(":", "-")
    s_ip = server_ip.replace(":", "-")
    return f"{ts_str}_{role}_{proto}_{server_port}_{c_ip}_{s_ip}.log"


class TrafficLogger:
    """
    Per-connection CSV logger.

    Usage:
        logger = TrafficLogger(logdir, proto, server_ip, server_port, client_ip, client_port)
        logger.log(EVENT_CONNECT, ...)
        logger.close()
    """

    def __init__(
        self,
        logdir: Path,
        proto: str,
        server_ip: str,
        server_port: int,
        client_ip: str,
        client_port: int,
        role: str = "client",
        connect_time: Optional[datetime] = None,
        rich_output=None,
    ) -> None:
        self.proto = proto
        self.server_ip = server_ip
        self.server_port = server_port
        self.client_ip = client_ip
        self.client_port = client_port
        self.role = role
        self.connect_time = connect_time or datetime.now()
        self.rich_output = rich_output

        # Build file path
        logdir = _log_dir(logdir)
        filename = _log_filename(
            proto, server_port, client_ip, server_ip, role, self.connect_time
        )
        self.filepath = logdir / filename

        # Open file and write header
        self._file = self.filepath.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(
        self,
        event_type: str,
        elapsed_sec: float = 0.0,
        bytes_sent: int = 0,
        bytes_recv: int = 0,
        bps_sent: float = 0.0,
        bps_recv: float = 0.0,
        message: str = "",
        pkt_seq: int|str = "",
        pkt_loss: int|str = "",
        pkt_ooo: int|str = "",
        mode: str = "",
    ) -> None:
        """Write one CSV row to file and stdout."""
        row = {
            "datetime":    datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "event_type":  event_type,
            "proto":       self.proto,
            "server_ip":   self.server_ip,
            "server_port": self.server_port,
            "client_ip":   self.client_ip,
            "client_port": self.client_port,
            "elapsed_sec": f"{elapsed_sec:.3f}",
            "bytes_sent":  bytes_sent,
            "bytes_recv":  bytes_recv,
            "bps_sent":    f"{bps_sent:.0f}",
            "bps_recv":    f"{bps_recv:.0f}",
            "message":     message,
            "pkt_seq":     pkt_seq,
            "pkt_loss":    pkt_loss,
            "pkt_ooo":     pkt_ooo,
            "mode":        mode,
            "role":        self.role,
        }
        self._writer.writerow(row)
        self._file.flush()
        self._print_row(row)

    def close(self) -> None:
        """Close the log file."""
        try:
            self._file.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _print_row(self, row: dict) -> None:
        """Print CSV row to stdout with rich formatting if available."""
        if self.rich_output:
            self.rich_output.print_row(row)
        else:
            # Fallback to regular print
            values = [str(row[f]) for f in CSV_FIELDS]
            print(",".join(values), flush=True)
