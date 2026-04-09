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
from common.rich_output import RichTrafficOutput
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
    import socket
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
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    conn.close()


def _make_connection(server_ip: str, server_port: int, timeout: float) -> http.client.HTTPConnection:
    """Create HTTP connection with enhanced socket options."""
    import socket
    conn = http.client.HTTPConnection(server_ip, server_port, timeout=timeout)
    
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
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Make HTTP request with automatic retry on connection failure."""
    for attempt in range(max_retries + 1):
        try:
            conn = _make_connection(server_ip, server_port, args.timeout_sec)
            conn.request(method, url)
            resp = conn.getresponse()
            
            if resp.status == 200:
                return conn, resp
            else:
                conn.close()
                if attempt < max_retries:
                    print(f"[HTTP Client] Server returned {resp.status}, retrying in {retry_delay}s...", file=sys.stderr)
                    time.sleep(retry_delay)
                    continue
                else:
                    raise http.client.HTTPError(f"HTTP {resp.status}")
                    
        except (http.client.HTTPException, ConnectionError, OSError, TimeoutError) as e:
            if conn:
                conn.close()
            if attempt < max_retries:
                print(f"[HTTP Client] Request failed: {e}, retrying in {retry_delay}s... ({attempt + 1}/{max_retries})", file=sys.stderr)
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

        # Socket is now connected - capture actual ephemeral port
        if conn.sock:
            logger.client_port = conn.sock.getsockname()[1]

        if not connect_logged.is_set():
            logger.log(EVENT_CONNECT,
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode}", mode=args.mode)
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
                print(f"[HTTP Client] Server disconnected during transfer: {e}", file=sys.stderr)
                break
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                print(f"[HTTP Client] Data receive error: {e}", file=sys.stderr)
                # Try to reconnect and resume
                try:
                    conn.close()
                    conn, resp = _make_resilient_request(server_ip, server_port, args, "GET", "/download")
                    print("[HTTP Client] Reconnected and resumed download", file=sys.stderr)
                    continue
                except Exception as reconnect_error:
                    print(f"[HTTP Client] Reconnection failed: {reconnect_error}", file=sys.stderr)
                    break

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
                       message=f"Connected to {server_ip}:{server_port} mode={args.mode}", mode=args.mode)
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
    # Initialize rich output handler
    rich_output = RichTrafficOutput(threshold=args.threshold)
    
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
        rich_output=rich_output,
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
    p.add_argument("--threshold", type=int, default=1000,
                   help="Data transfer rate threshold for warnings (bytes/sec)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rich_output = RichTrafficOutput(threshold=args.threshold)
    rich_output.print_message(f"[HTTP Client] Connecting to {args.host}:{args.port}  mode={args.mode}  "
          f"duration={args.duration}s  interval={args.interval}s  blocksize={args.blocksize}B", "INFO")
    rich_output.print_message("[HTTP Client] Press Ctrl+C to stop.", "INFO")
    run_client(args)
    rich_output.print_message("[HTTP Client] Done.", "INFO")


if __name__ == "__main__":
    main()
