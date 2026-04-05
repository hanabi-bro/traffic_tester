"""
HTTP Traffic Test Client
========================
Usage:
    python client.py <host> <port> [options]

Options:
    --timeout SEC     Connection/read timeout in seconds (default: 30)
    --interval SEC    Stats log interval in seconds (default: 1)
    --duration SEC    Run duration in seconds, 0=unlimited (default: 0)
    --blocksize N     Read/send block size in bytes (default: 65536)
    --mode MODE       download | upload | both (default: download)
    --logdir DIR      Log directory (default: ./log_traffic)
"""

from __future__ import annotations

import argparse
import http.client
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.ip_utils import get_source_ip, resolve_host
from common.logger import (
    EVENT_CONNECT, EVENT_DATA, EVENT_DISCONNECT, EVENT_ERROR, EVENT_TIMEOUT,
    TrafficLogger,
)
from common.stats import StatsTracker

PROTO = "HTTP"



def _drain_and_close(conn: http.client.HTTPConnection, resp, drain_timeout: float = 2.0) -> None:
    """
    Gracefully close a download connection (FIN instead of RST).

    Problem: The server sends chunked data indefinitely. If the client calls
    conn.close() while data is still arriving, the OS sends TCP RST, causing
    the server to log "[Errno 104] Connection reset by peer".

    Solution:
      1. Set SO_LINGER(0) to suppress RST on close — let the OS complete the
         TCP FIN handshake even after close() returns.
      2. Drain inbound data briefly (up to drain_timeout seconds) so the
         server's write-side encounters our FIN/EOF rather than a RST.
      3. close() the socket.
    """
    import socket as _socket
    import struct
    sock = conn.sock
    if sock:
        try:
            # SO_LINGER with l_onoff=1, l_linger=0 causes RST — we want the
            # opposite: keep linger OFF (default) so close() triggers FIN.
            # What actually helps here is reading the pipe dry briefly.
            sock.settimeout(0.05)
        except OSError:
            pass

        deadline = time.monotonic() + drain_timeout
        try:
            if resp:
                while time.monotonic() < deadline:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
        except OSError:
            pass

        # Half-close our write side first, then let the OS do a clean FIN
        try:
            sock.shutdown(_socket.SHUT_WR)
        except OSError:
            pass

    conn.close()


def _make_connection(server_ip: str, server_port: int, timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(server_ip, server_port, timeout=timeout)


def run_download(
    server_ip: str,
    server_port: int,
    args: argparse.Namespace,
    logger: TrafficLogger,
    stats: StatsTracker,
    stop_event: threading.Event,
    connect_logged: threading.Event,
) -> tuple[str, str]:
    """
    GET /download and discard received data.
    Writes CONNECT log after the socket is established (to capture real client_port).
    Returns (event_type, message).
    """
    conn = _make_connection(server_ip, server_port, args.timeout_sec)
    resp = None
    event_type = EVENT_DISCONNECT
    msg = "Download ended"
    try:
        conn.request("GET", "/download")

        # Socket is now connected — capture actual ephemeral port
        if conn.sock:
            logger.client_port = conn.sock.getsockname()[1]

        if not connect_logged.is_set():
            logger.log(EVENT_CONNECT,
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode}")
            connect_logged.set()

        resp = conn.getresponse()
        if resp.status != 200:
            return EVENT_ERROR, f"HTTP {resp.status}"

        blocksize = args.blocksize
        deadline = (time.monotonic() + args.duration) if args.duration > 0 else None

        while not stop_event.is_set():
            if deadline and time.monotonic() >= deadline:
                stop_event.set()
                break
            data = resp.read(blocksize)
            if not data:
                break
            stats.add_recv(len(data))

    except TimeoutError:
        event_type = EVENT_TIMEOUT
        msg = "Connection timed out"
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        event_type = EVENT_ERROR
        msg = str(e)
    finally:
        # Graceful close: FIN handshake instead of RST
        _drain_and_close(conn, resp)

    return event_type, msg


def run_upload(
    server_ip: str,
    server_port: int,
    args: argparse.Namespace,
    logger: TrafficLogger,
    stats: StatsTracker,
    stop_event: threading.Event,
    connect_logged: threading.Event,
) -> tuple[str, str]:
    """
    POST /upload with streaming chunked dummy data.
    Returns (event_type, message).
    """
    reader = DummyReader(args.blocksize)
    deadline = (time.monotonic() + args.duration) if args.duration > 0 else None

    conn = _make_connection(server_ip, server_port, args.timeout_sec)
    _chunked_open = False   # True once endheaders() succeeds
    event_type = EVENT_DISCONNECT
    msg = "Upload ended"
    try:
        conn.putrequest("POST", "/upload")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.endheaders()
        _chunked_open = True

        if conn.sock:
            logger.client_port = conn.sock.getsockname()[1]

        if not connect_logged.is_set():
            logger.log(EVENT_CONNECT,
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode}")
            connect_logged.set()

        while not stop_event.is_set():
            if deadline and time.monotonic() >= deadline:
                stop_event.set()
                break
            chunk = reader.read()
            conn.send(f"{len(chunk):X}\r\n".encode())
            conn.send(chunk)
            conn.send(b"\r\n")
            stats.add_sent(len(chunk))

    except TimeoutError:
        event_type = EVENT_TIMEOUT
        msg = "Connection timed out"
        _chunked_open = False   # don't attempt terminator on timeout
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        event_type = EVENT_ERROR
        msg = str(e)
        _chunked_open = False
    finally:
        if _chunked_open:
            # Send chunked terminator so the server can respond with 200
            # instead of seeing an abrupt RST.
            try:
                conn.send(b"0\r\n\r\n")
                conn.sock.settimeout(2.0)
                resp = conn.getresponse()
                if event_type == EVENT_DISCONNECT:
                    msg = f"Upload ended (HTTP {resp.status})"
            except OSError:
                pass
        conn.close()

    return event_type, msg


def run_client(args: argparse.Namespace) -> None:
    server_ip = resolve_host(args.host)
    server_port: int = args.port
    client_ip = get_source_ip(server_ip, server_port)

    connect_time = datetime.now()
    # client_port=0 initially; updated inside run_download/run_upload after connect
    logger = TrafficLogger(
        logdir=args.logdir,
        proto=PROTO,
        server_ip=server_ip,
        server_port=server_port,
        client_ip=client_ip,
        client_port=0,
        connect_time=connect_time,
    )
    stats = StatsTracker()
    stop_event = threading.Event()
    # Ensures CONNECT is logged exactly once across both threads in 'both' mode
    connect_logged = threading.Event()

    def _shutdown(sig=None, frame=None):
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    # Stats reporter (starts immediately; CONNECT will be logged by run_* functions)
    def _report_loop():
        while not stop_event.wait(timeout=args.interval):
            if not connect_logged.is_set():
                continue  # Don't log DATA before CONNECT
            snap = stats.snapshot()
            logger.log(
                EVENT_DATA,
                elapsed_sec=snap.elapsed_sec,
                bytes_sent=snap.total_bytes_sent,
                bytes_recv=snap.total_bytes_recv,
                bps_sent=snap.bps_sent,
                bps_recv=snap.bps_recv,
                message=f"interval {args.interval}s",
            )

    reporter = threading.Thread(target=_report_loop, daemon=True)
    reporter.start()

    event_type = EVENT_DISCONNECT
    msg = "Session ended"

    try:
        if args.mode == "download":
            event_type, msg = run_download(
                server_ip, server_port, args, logger, stats, stop_event, connect_logged
            )
        elif args.mode == "upload":
            event_type, msg = run_upload(
                server_ip, server_port, args, logger, stats, stop_event, connect_logged
            )
        else:  # both
            results: list[tuple[str, str]] = [("", ""), ("", "")]

            def _dl():
                results[0] = run_download(
                    server_ip, server_port, args, logger, stats, stop_event, connect_logged
                )

            def _ul():
                results[1] = run_upload(
                    server_ip, server_port, args, logger, stats, stop_event, connect_logged
                )

            t_dl = threading.Thread(target=_dl, daemon=True)
            t_ul = threading.Thread(target=_ul, daemon=True)
            t_dl.start()
            t_ul.start()
            t_dl.join()
            t_ul.join()

            events = {r[0] for r in results}
            if EVENT_ERROR in events:
                event_type = EVENT_ERROR
            elif EVENT_TIMEOUT in events:
                event_type = EVENT_TIMEOUT
            else:
                event_type = EVENT_DISCONNECT
            msg = " | ".join(r[1] for r in results if r[1])

    finally:
        stop_event.set()
        reporter.join(timeout=args.interval + 1)
        elapsed = stats.elapsed()
        sent, recv = stats.totals()
        logger.log(event_type, elapsed_sec=elapsed, bytes_sent=sent,
                   bytes_recv=recv, message=msg)
        logger.close()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HTTP Traffic Test Client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("host", help="Server hostname or IP address")
    p.add_argument("port", type=int, help="Server port")
    p.add_argument("--timeout", type=float, default=30.0, dest="timeout_sec",
                   help="Connection/read timeout (seconds)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Stats log interval (seconds)")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Run duration (seconds, 0=unlimited)")
    p.add_argument("--blocksize", type=int, default=65536,
                   help="Read/send block size (bytes)")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download",
                   help="Transfer direction")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(f"[HTTP Client] Connecting to {args.host}:{args.port}  mode={args.mode}  "
          f"duration={args.duration}s  interval={args.interval}s  blocksize={args.blocksize}B")
    print("[HTTP Client] Press Ctrl+C to stop.")
    run_client(args)
    print("[HTTP Client] Done.")


if __name__ == "__main__":
    main()
