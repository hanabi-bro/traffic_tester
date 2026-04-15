"""
TCP Traffic Test Client
=======================
Usage:
    python client.py <host> <port> [options]

Options:
    --handshake-timeout SEC  Handshake timeout in seconds (default: 30)
    --data-timeout SEC       Data transfer timeout in seconds (default: 1)
    --interval SEC    Stats log interval in seconds (default: 1)
    --duration SEC    Run duration in seconds, 0=unlimited (default: 0)
    --blocksize N     Send block size in bytes (default: 65536)
    --mode MODE       download | upload | both (default: download)
    --logdir DIR      Log directory (default: ./log_traffic)
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.ip_utils import get_source_ip, resolve_host, tcp_no_delay
from common.logger import (
    EVENT_CONNECT, EVENT_DATA, EVENT_DISCONNECT, EVENT_ERROR, EVENT_TIMEOUT,
    TrafficLogger,
)
from common.rich_output import RichTrafficOutput
from common.stats import StatsTracker

PROTO = "TCP"


class ResilientSocket:
    """Socket wrapper with automatic reconnection capability."""
    
    def __init__(self, server_info: tuple[str, int], handshake_timeout: float = 30.0, data_timeout: float = 1.0, rich_output=None):
        self.server_info = server_info
        self.handshake_timeout = handshake_timeout
        self.data_timeout = data_timeout
        self.rich_output = rich_output
        self._sock = self._create_socket()
        self._lock = threading.Lock()
        
    def _create_socket(self) -> socket.socket:
        """Create a new socket with all optimal settings."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.handshake_timeout)
        tcp_no_delay(sock)
        
        # TCP keepalive settings
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except (OSError, AttributeError):
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        
        sock.connect(self.server_info)
        # After connection is established, switch to data transfer timeout
        sock.settimeout(self.data_timeout)
        return sock
    
    def _reconnect(self) -> bool:
        """Attempt to reconnect the socket."""
        try:
            with self._lock:
                if self._sock:
                    self._sock.close()
                self._sock = self._create_socket()
                if self.rich_output:
                    self.rich_output.print_message("[TCP Client] Reconnected successfully", "INFO")
                else:
                    print("[TCP Client] Reconnected successfully", file=sys.stderr)
                return True
        except OSError as e:
            if self.rich_output:
                self.rich_output.print_message(f"[TCP Client] Reconnection failed: {e}", "ERROR")
            else:
                print(f"[TCP Client] Reconnection failed: {e}", file=sys.stderr)
            return False
    
    def sendall(self, data: bytes, max_retries: int = 3, retry_delay: float = 1.0) -> bool:
        """Send data with automatic retry on connection failure."""
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                with self._lock:
                    if self._sock:
                        self._sock.sendall(data)
                        return True
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                if self.rich_output:
                    self.rich_output.print_message(f"[TCP Client] Send error: {e}", "ERROR")
                else:
                    print(f"[TCP Client] Send error: {e}", file=sys.stderr)
                retry_count += 1
                
                if retry_count <= max_retries:
                    if self.rich_output:
                        self.rich_output.print_message(f"[TCP Client] Retrying connection in {retry_delay}s... ({retry_count}/{max_retries})", "INFO")
                    else:
                        print(f"[TCP Client] Retrying connection in {retry_delay}s... ({retry_count}/{max_retries})", file=sys.stderr)
                    time.sleep(retry_delay)
                    if self._reconnect():
                        continue
                break
        
        if self.rich_output:
            self.rich_output.print_message("[TCP Client] Max retries reached, giving up", "ERROR")
        else:
            print("[TCP Client] Max retries reached, giving up", file=sys.stderr)
        return False
    
    def recv(self, bufsize: int, max_retries: int = 3, retry_delay: float = 1.0) -> bytes | None:
        """Receive data with automatic retry on connection failure."""
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                with self._lock:
                    if self._sock:
                        data = self._sock.recv(bufsize)
                        return data
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                if self.rich_output:
                    self.rich_output.print_message(f"[TCP Client] Receive error: {e}", "ERROR")
                else:
                    print(f"[TCP Client] Receive error: {e}", file=sys.stderr)
                retry_count += 1
                
                if retry_count <= max_retries:
                    if self.rich_output:
                        self.rich_output.print_message(f"[TCP Client] Retrying connection in {retry_delay}s... ({retry_count}/{max_retries})", "INFO")
                    else:
                        print(f"[TCP Client] Retrying connection in {retry_delay}s... ({retry_count}/{max_retries})", file=sys.stderr)
                    time.sleep(retry_delay)
                    if self._reconnect():
                        continue
                break
        
        if self.rich_output:
            self.rich_output.print_message("[TCP Client] Max retries reached, giving up", "ERROR")
        else:
            print("[TCP Client] Max retries reached, giving up", file=sys.stderr)
        return None
    
    def getsockname(self) -> tuple[str, int]:
        """Get socket's own address."""
        with self._lock:
            if self._sock:
                return self._sock.getsockname()
            raise OSError("Socket not connected")
    
    def close(self) -> None:
        """Close the socket."""
        with self._lock:
            if self._sock:
                self._sock.close()
                self._sock = None


def run_client(args: argparse.Namespace) -> None:
    server_ip = resolve_host(args.host)
    server_port: int = args.port
    client_ip = get_source_ip(server_ip, server_port)

    # Initialize rich output handler
    rich_output = RichTrafficOutput(threshold=args.threshold, use_table_format=not args.csv)

    # Create resilient socket with automatic reconnection
    server_info = (server_ip, server_port)
    try:
        sock = ResilientSocket(server_info, handshake_timeout=args.handshake_timeout_sec, data_timeout=args.data_timeout_sec, rich_output=rich_output)
    except (TimeoutError, OSError) as e:
        if rich_output:
            rich_output.print_message(f"[TCP Client] Connection failed: {e}", "ERROR")
        else:
            print(f"[TCP Client] Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    client_port = sock.getsockname()[1]
    
    logger = TrafficLogger(
        logdir=args.logdir,
        proto=PROTO,
        server_ip=server_ip,
        server_port=server_port,
        client_ip=client_ip,
        client_port=client_port,
        connect_time=datetime.now(),
        rich_output=rich_output,
    )
    stats = StatsTracker()
    stop_event = threading.Event()

    logger.log(EVENT_CONNECT, message=f"Connected to {server_ip}:{server_port}")

    # Duration deadline
    deadline: float | None = None
    if args.duration > 0:
        deadline = time.monotonic() + args.duration

    def _shutdown(sig=None, frame=None):
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    # Stats reporter thread
    def _report_loop():
        while not stop_event.wait(timeout=args.interval):
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
    error_msg = ""

    try:
        mode = args.mode

        if mode == "download":
            _recv_loop(sock, args.blocksize, stats, stop_event, deadline)
        elif mode == "upload":
            _send_loop(sock, args.blocksize, stats, stop_event, deadline, server_info)
        else:  # both
            t_send = threading.Thread(
                target=_send_loop, args=(sock, args.blocksize, stats, stop_event, deadline, server_info), daemon=True
            )
            t_recv = threading.Thread(
                target=_recv_loop, args=(sock, args.blocksize, stats, stop_event, deadline), daemon=True
            )
            t_send.start()
            t_recv.start()
            t_send.join()
            t_recv.join()

    except TimeoutError:
        event_type = EVENT_TIMEOUT
        error_msg = "Connection timed out"
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        event_type = EVENT_ERROR
        error_msg = str(e)
    finally:
        stop_event.set()
        reporter.join(timeout=args.interval + 1)
        sock.close()

        elapsed = stats.elapsed()
        sent, recv = stats.totals()
        logger.log(
            event_type,
            elapsed_sec=elapsed,
            bytes_sent=sent,
            bytes_recv=recv,
            message=error_msg or "Session ended",
            mode=args.mode,
        )
        logger.close()


def _send_loop(
    sock: ResilientSocket,
    blocksize: int,
    stats: StatsTracker,
    stop: threading.Event,
    deadline: float | None,
    server_info: tuple[str, int],
) -> None:
    """Upload dummy data to server with automatic reconnection."""
    reader = DummyReader(blocksize)
    
    while not stop.is_set():
        if deadline and time.monotonic() >= deadline:
            stop.set()
            break
        
        chunk = reader.read()
        if not sock.sendall(chunk):
            # sendall failed and reconnection attempts exhausted
            stop.set()
            break
        
        stats.add_sent(len(chunk))


def _recv_loop(
    sock: ResilientSocket,
    blocksize: int,
    stats: StatsTracker,
    stop: threading.Event,
    deadline: float | None,
) -> None:
    """Receive and discard data from server with automatic reconnection."""
    while not stop.is_set():
        if deadline and time.monotonic() >= deadline:
            stop.set()
            break
        
        data = sock.recv(blocksize)
        if data is None:
            # recv failed and reconnection attempts exhausted
            stop.set()
            break
        elif not data:
            # Connection closed by server
            if sock.rich_output:
                sock.rich_output.print_message("[TCP Client] Server closed connection", "DISCONNECT")
            else:
                print("[TCP Client] Server closed connection", file=sys.stderr)
            break
        
        stats.add_recv(len(data))


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TCP Traffic Test Client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("host", help="Server hostname or IP address")
    p.add_argument("port", type=int, help="Server port")
    p.add_argument("--handshake-timeout", type=float, default=30.0, dest="handshake_timeout_sec",
                   help="Handshake timeout (seconds)")
    p.add_argument("--data-timeout", type=float, default=1.0, dest="data_timeout_sec",
                   help="Data transfer timeout (seconds)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Stats log interval (seconds)")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Run duration (seconds, 0=unlimited)")
    p.add_argument("--blocksize", type=int, default=65536,
                   help="Send/recv block size (bytes)")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download",
                   help="Transfer direction (client perspective)")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    p.add_argument("--threshold", type=int, default=1000,
                   help="Data transfer rate threshold for warnings (bytes/sec)")
    p.add_argument("--csv", action="store_true",
                   help="Use CSV format for console output (default: table format)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rich_output = RichTrafficOutput(threshold=args.threshold, use_table_format=not args.csv)
    rich_output.print_message(f"[TCP Client] Connecting to {args.host}:{args.port}  mode={args.mode}  "
          f"duration={args.duration}s  interval={args.interval}s  blocksize={args.blocksize}B", "INFO")
    rich_output.print_message("[TCP Client] Press Ctrl+C to stop.", "INFO")
    run_client(args)
    rich_output.print_message("[TCP Client] Done.", "INFO")


if __name__ == "__main__":
    main()
