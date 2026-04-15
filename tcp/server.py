"""
TCP Traffic Test Server
=======================
Usage:
    python server.py <port> [options]

Options:
    --bind ADDR       Bind address (default: 0.0.0.0)
    --timeout SEC     Client idle timeout in seconds (default: 30)
    --interval SEC    Stats log interval in seconds (default: 1)
    --blocksize N     Send block size in bytes (default: 65536)
    --mode MODE       download | upload | both (default: download)
    --logdir DIR      Log directory (default: ./log_traffic)
"""

from __future__ import annotations

import argparse
import signal
import socketserver
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.ip_utils import tcp_no_delay
from common.logger import (
    EVENT_CONNECT, EVENT_DATA, EVENT_DISCONNECT, EVENT_ERROR, EVENT_TIMEOUT,
    TrafficLogger,
)
from common.rich_output import RichTrafficOutput
from common.stats import StatsTracker

PROTO = "TCP"


class TCPClientHandler(socketserver.BaseRequestHandler):
    """Handles one TCP client connection in its own thread."""

    def setup(self) -> None:
        self.connect_time = datetime.now()
        client_ip, client_port = self.request.getpeername()[:2]
        server_ip, server_port = self.request.getsockname()[:2]

        tcp_no_delay(self.request)

        self.logger = TrafficLogger(
            logdir=self.server.logdir,
            proto=PROTO,
            server_ip=server_ip,
            server_port=server_port,
            client_ip=client_ip,
            client_port=client_port,
            role="server",
            connect_time=self.connect_time,
            rich_output=self.server.rich_output,
        )
        self.stats = StatsTracker()
        self._stop_event = threading.Event()
        # Guard: handle() sets True when it already wrote a terminal log row.
        # finish() checks this to avoid writing a duplicate DISCONNECT.
        self._terminal_logged = False

        self.logger.log(EVENT_CONNECT, message=f"Client connected from {client_ip}:{client_port}", mode=self.server.mode)

    def handle(self) -> None:
        sock = self.request
        mode: str = self.server.mode
        timeout: float = self.server.timeout_sec
        blocksize: int = self.server.blocksize
        interval: float = self.server.interval

        sock.settimeout(timeout)

        reporter = threading.Thread(target=self._report_loop, args=(interval,), daemon=True)
        reporter.start()

        try:
            if mode == "download":
                self._send_loop(sock, blocksize)
            elif mode == "upload":
                self._recv_loop(sock, blocksize)
            else:  # both
                t_send = threading.Thread(
                    target=self._send_loop, args=(sock, blocksize), daemon=True
                )
                t_recv = threading.Thread(
                    target=self._recv_loop, args=(sock, blocksize), daemon=True
                )
                t_send.start()
                t_recv.start()
                t_send.join()
                t_recv.join()

        except TimeoutError:
            elapsed = self.stats.elapsed()
            sent, recv = self.stats.totals()
            self.logger.log(EVENT_TIMEOUT, elapsed_sec=elapsed,
                            bytes_sent=sent, bytes_recv=recv,
                            message="Connection timed out", mode=self.server.mode)
            self._terminal_logged = True

        except (ConnectionResetError, BrokenPipeError) as e:
            elapsed = self.stats.elapsed()
            sent, recv = self.stats.totals()
            self.logger.log(EVENT_DISCONNECT, elapsed_sec=elapsed,
                            bytes_sent=sent, bytes_recv=recv,
                            message=f"Client disconnected ({e})", mode=self.server.mode)
            self._terminal_logged = True

        except OSError as e:
            elapsed = self.stats.elapsed()
            sent, recv = self.stats.totals()
            self.logger.log(EVENT_ERROR, elapsed_sec=elapsed,
                            bytes_sent=sent, bytes_recv=recv,
                            message=str(e), mode=self.server.mode)
            self._terminal_logged = True

        finally:
            self._stop_event.set()
            reporter.join(timeout=interval + 1)

    def finish(self) -> None:
        """Called after handle(). Write DISCONNECT only if handle() didn't already."""
        elapsed = self.stats.elapsed()
        sent, recv = self.stats.totals()
        if not self._terminal_logged:
            self.logger.log(EVENT_DISCONNECT, elapsed_sec=elapsed,
                            bytes_sent=sent, bytes_recv=recv,
                            message="Connection closed", mode=self.server.mode)
        self.logger.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_loop(self, sock, blocksize: int) -> None:
        reader = DummyReader(blocksize)
        while not self._stop_event.is_set():
            chunk = reader.read()
            try:
                sock.sendall(chunk)
            except OSError:
                break
            self.stats.add_sent(len(chunk))

    def _recv_loop(self, sock, blocksize: int) -> None:
        while not self._stop_event.is_set():
            try:
                data = sock.recv(blocksize)
            except OSError:
                break
            if not data:
                break
            self.stats.add_recv(len(data))

    def _report_loop(self, interval: float) -> None:
        while not self._stop_event.wait(timeout=interval):
            snap = self.stats.snapshot()
            self.logger.log(
                EVENT_DATA,
                elapsed_sec=snap.elapsed_sec,
                bytes_sent=snap.total_bytes_sent,
                bytes_recv=snap.total_bytes_recv,
                bps_sent=snap.bps_sent,
                bps_recv=snap.bps_recv,
                message=f"interval {interval}s",
                mode=self.server.mode,
            )


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        bind_addr: str,
        port: int,
        logdir: Path,
        timeout_sec: float,
        interval: float,
        blocksize: int,
        mode: str,
        rich_output=None,
    ) -> None:
        self.logdir = logdir
        self.timeout_sec = timeout_sec
        self.interval = interval
        self.blocksize = blocksize
        self.mode = mode
        self.rich_output = rich_output
        super().__init__((bind_addr, port), TCPClientHandler)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TCP Traffic Test Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("port", type=int, help="Listen port")
    p.add_argument("--bind", default="0.0.0.0", help="Bind address")
    p.add_argument("--timeout", type=float, default=30.0, dest="timeout_sec",
                   help="Client idle timeout (seconds)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Stats log interval (seconds)")
    p.add_argument("--blocksize", type=int, default=65536,
                   help="Send/recv block size (bytes)")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download",
                   help="Transfer direction: download=server sends to client, upload=server receives")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    p.add_argument("--threshold", type=int, default=1000,
                   help="Data transfer rate threshold for warnings (bytes/sec)")
    p.add_argument("--csv", action="store_true",
                   help="Use CSV format for console output (default: table format)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Initialize rich output handler
    rich_output = RichTrafficOutput(threshold=args.threshold, use_table_format=not args.csv)

    server = ThreadedTCPServer(
        bind_addr=args.bind,
        port=args.port,
        logdir=args.logdir,
        timeout_sec=args.timeout_sec,
        interval=args.interval,
        blocksize=args.blocksize,
        mode=args.mode,
        rich_output=rich_output,
    )

    rich_output.print_message(f"[TCP Server] Listening on {args.bind}:{args.port}  mode={args.mode}  "
          f"timeout={args.timeout_sec}s  interval={args.interval}s  blocksize={args.blocksize}B", "INFO")
    rich_output.print_message("[TCP Server] Press Ctrl+C to stop.", "INFO")

    _shutdown_called = threading.Event()

    def _shutdown(sig, frame):
        if not _shutdown_called.is_set():
            _shutdown_called.set()
            rich_output.print_message("\n[TCP Server] Shutting down...", "INFO")
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    server.serve_forever()
    rich_output.print_message("[TCP Server] Stopped.", "INFO")


if __name__ == "__main__":
    main()
