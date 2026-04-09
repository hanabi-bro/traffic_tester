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
from common.rich_output import RichTrafficOutput
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
    """Create HTTPS connection with enhanced socket options."""
    import socket
    ctx = _make_ssl_context(verify)
    conn = http.client.HTTPSConnection(server_ip, server_port, timeout=timeout, context=ctx)
    
    # Apply socket options after connection is established
    def connect_with_opts():
        original_connect = conn.connect
        def enhanced_connect():
            original_connect()
            if conn.sock:
                # TCP keepalive settings
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                try:
                    conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                    conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                    conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                except (OSError, AttributeError):
                    pass
                # Buffer optimization
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        conn.connect = enhanced_connect
    
    return conn


def _make_resilient_request(
    server_ip: str,
    server_port: int,
    args: argparse.Namespace,
    method: str,
    url: str,
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
    """Make HTTPS request with automatic retry on connection failure."""
    for attempt in range(max_retries + 1):
        try:
            conn = _make_connection(server_ip, server_port, args.timeout_sec, args.verify)
            conn.request(method, url)
            resp = conn.getresponse()
            
            if resp.status == 200:
                return conn, resp
            else:
                conn.close()
                if attempt < max_retries:
                    print(f"[HTTPS Client] Server returned {resp.status}, retrying in {retry_delay}s...", file=sys.stderr)
                    time.sleep(retry_delay)
                    continue
                else:
                    raise http.client.HTTPError(f"HTTP {resp.status}")
                    
        except (http.client.HTTPException, ConnectionError, OSError, TimeoutError, ssl.SSLError) as e:
            if conn:
                conn.close()
            if attempt < max_retries:
                print(f"[HTTPS Client] Request failed: {e}, retrying in {retry_delay}s... ({attempt + 1}/{max_retries})", file=sys.stderr)
                time.sleep(retry_delay)
            else:
                raise
    
    raise ConnectionError("Max retries reached")


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
    GET /download and discard received data with reconnection support.
    Writes CONNECT log after the socket is established (to capture real client_port).
    Returns (event_type, message).
    """
    conn = None
    resp = None
    event_type = EVENT_DISCONNECT
    msg = "Download ended"
    
    try:
        # Make resilient request with retry
        conn, resp = _make_resilient_request(server_ip, server_port, args, "GET", "/download")

        if conn.sock:
            logger.client_port = conn.sock.getsockname()[1]

        if not connect_logged.is_set():
            verify_str = "on" if args.verify else "off (self-signed OK)"
            logger.log(EVENT_CONNECT,
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode} verify={verify_str}", mode=args.mode)
            connect_logged.set()

        blocksize = args.blocksize
        deadline = (time.monotonic() + args.duration) if args.duration > 0 else None

        while not stop_event.is_set():
            if deadline and time.monotonic() >= deadline:
                stop_event.set()
                break
            
            try:
                data = resp.read(blocksize)
                if not data:
                    break
                stats.add_recv(len(data))
            except http.client.IncompleteRead as e:
                # Server disconnected during chunked transfer - normal when server stops
                print(f"[HTTPS Client] Server disconnected during transfer: {e}", file=sys.stderr)
                break
            except (ConnectionResetError, BrokenPipeError, OSError, ssl.SSLError) as e:
                print(f"[HTTPS Client] Data receive error: {e}", file=sys.stderr)
                # Try to reconnect and resume
                try:
                    conn.close()
                    conn, resp = _make_resilient_request(server_ip, server_port, args, "GET", "/download")
                    print("[HTTPS Client] Reconnected and resumed download", file=sys.stderr)
                    continue
                except Exception as reconnect_error:
                    print(f"[HTTPS Client] Reconnection failed: {reconnect_error}", file=sys.stderr)
                    break

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
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode} verify={verify_str}", mode=args.mode)
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
    # Initialize rich output handler
    rich_output = RichTrafficOutput(threshold=args.threshold)
    
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
        rich_output=rich_output,
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
                mode=args.mode,
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
                   bytes_recv=recv, message=msg, mode=args.mode)
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
    p.add_argument("--threshold", type=int, default=1000,
                   help="Data transfer rate threshold for warnings (bytes/sec)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    verify_str = "on" if args.verify else "off"
    rich_output = RichTrafficOutput(threshold=args.threshold)
    rich_output.print_message(f"[HTTPS Client] Connecting to {args.host}:{args.port}  mode={args.mode}  "
          f"duration={args.duration}s  interval={args.interval}s  blocksize={args.blocksize}B  "
          f"verify={verify_str}", "INFO")
    rich_output.print_message("[HTTPS Client] Press Ctrl+C to stop.", "INFO")
    run_client(args)
    rich_output.print_message("[HTTPS Client] Done.", "INFO")


if __name__ == "__main__":
    main()
