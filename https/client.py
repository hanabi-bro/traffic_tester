"""
HTTPS Traffic Test Client
=========================
Usage:
    python client.py <host> <port> [options]

Certificate verification is disabled by default (suitable for self-signed certs
used in FW policy testing). Use --verify to enable verification.

Options:
    --timeout SEC     Connection/read timeout in seconds (default: 30)
    --interval SEC    Stats log interval in seconds (default: 1)
    --duration SEC    Run duration in seconds, 0=unlimited (default: 0)
    --blocksize N     Read/send block size in bytes (default: 65536)
    --mode MODE       download | upload | both (default: download)
    --verify          Enable TLS certificate verification (default: disabled)
    --logdir DIR      Log directory (default: ./log_traffic)
"""

from __future__ import annotations

import argparse
import http.client
import signal
import ssl
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

PROTO = "HTTPS"



def _drain_and_close(conn: http.client.HTTPSConnection, resp, drain_timeout: float = 2.0) -> None:
    """
    Gracefully close an HTTPS download connection (FIN instead of RST).

    Simply calling conn.close() while the server is streaming sends a TCP RST
    (WinError 10054 / ECONNRESET on the server side).

    Sequence:
      1. shutdown(SHUT_WR) to half-close our write side.
      2. Drain remaining inbound data briefly so the server finishes cleanly.
      3. close() the socket.
    """
    import socket as _socket
    try:
        if conn.sock:
            conn.sock.shutdown(_socket.SHUT_WR)
    except OSError:
        pass
    deadline = time.monotonic() + drain_timeout
    try:
        if resp and conn.sock:
            conn.sock.settimeout(0.1)
            while time.monotonic() < deadline:
                chunk = resp.read(65536)
                if not chunk:
                    break
    except OSError:
        pass
    finally:
        conn.close()


def _make_ssl_context(verify: bool) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _make_connection(
    server_ip: str, server_port: int, timeout: float, verify: bool
) -> http.client.HTTPSConnection:
    ctx = _make_ssl_context(verify)
    return http.client.HTTPSConnection(server_ip, server_port, timeout=timeout, context=ctx)


def run_download(
    server_ip: str,
    server_port: int,
    args: argparse.Namespace,
    logger: TrafficLogger,
    stats: StatsTracker,
    stop_event: threading.Event,
    connect_logged: threading.Event,
) -> tuple[str, str]:
    conn = _make_connection(server_ip, server_port, args.timeout_sec, args.verify)
    resp = None
    event_type = EVENT_DISCONNECT
    msg = "Download ended"
    try:
        conn.request("GET", "/download")

        if conn.sock:
            logger.client_port = conn.sock.getsockname()[1]

        if not connect_logged.is_set():
            verify_str = "on" if args.verify else "off (self-signed OK)"
            logger.log(EVENT_CONNECT,
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode} verify={verify_str}")
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
    except ssl.SSLError as e:
        event_type = EVENT_ERROR
        msg = f"SSL error: {e}"
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        event_type = EVENT_ERROR
        msg = str(e)
    finally:
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
    reader = DummyReader(args.blocksize)
    deadline = (time.monotonic() + args.duration) if args.duration > 0 else None

    conn = _make_connection(server_ip, server_port, args.timeout_sec, args.verify)
    _chunked_open = False
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
            verify_str = "on" if args.verify else "off (self-signed OK)"
            logger.log(EVENT_CONNECT,
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode} verify={verify_str}")
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
        _chunked_open = False
    except ssl.SSLError as e:
        event_type = EVENT_ERROR
        msg = f"SSL error: {e}"
        _chunked_open = False
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        event_type = EVENT_ERROR
        msg = str(e)
        _chunked_open = False
    finally:
        if _chunked_open:
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
    connect_logged = threading.Event()

    def _shutdown(sig=None, frame=None):
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    def _report_loop():
        while not stop_event.wait(timeout=args.interval):
            if not connect_logged.is_set():
                continue
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
        description="HTTPS Traffic Test Client",
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
    p.add_argument("--verify", action="store_true", default=False,
                   help="Enable TLS certificate verification (default: disabled)")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    verify_str = "on" if args.verify else "off"
    print(f"[HTTPS Client] Connecting to {args.host}:{args.port}  mode={args.mode}  "
          f"duration={args.duration}s  interval={args.interval}s  blocksize={args.blocksize}B  "
          f"verify={verify_str}")
    print("[HTTPS Client] Press Ctrl+C to stop.")
    run_client(args)
    print("[HTTPS Client] Done.")


if __name__ == "__main__":
    main()
