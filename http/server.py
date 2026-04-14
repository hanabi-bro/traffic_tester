"""
HTTP Traffic Test Server
========================
Usage:
    python server.py <port> [options]

Endpoints:
    GET  /download  -> Server streams dummy data to client
    POST /upload    -> Server reads and discards client body

Options:
    --bind ADDR       Bind address (default: 0.0.0.0)
    --timeout SEC     Request timeout in seconds (default: 30)
    --interval SEC    Stats log interval in seconds (default: 1)
    --blocksize N     Send/recv block size in bytes (default: 65536)
    --logdir DIR      Log directory (default: ./log_traffic)
"""

from __future__ import annotations

import argparse
import http.server
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.logger import (
    EVENT_CONNECT, EVENT_DATA, EVENT_DISCONNECT, EVENT_ERROR, EVENT_TIMEOUT,
    TrafficLogger,
)
from common.rich_output import RichTrafficOutput
from common.stats import StatsTracker

PROTO = "HTTP"

# Module-level server config (set before serving)
_config: dict = {}


class TrafficHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for traffic test."""

    # Suppress default request log (we handle logging ourselves)
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass

    def _make_logger(self) -> TrafficLogger:
        client_ip, client_port = self.client_address[:2]
        # Use connection's local address for accurate server IP (not bind address)
        try:
            server_ip, server_port = self.connection.getsockname()[:2]
        except OSError:
            server_ip, server_port = self.server.server_address[:2]
        return TrafficLogger(
            logdir=_config["logdir"],
            proto=PROTO,
            server_ip=server_ip,
            server_port=server_port,
            client_ip=client_ip,
            client_port=client_port,
            role="server",
            connect_time=datetime.now(),
            rich_output=_config.get("rich_output"),
        )

    # ------------------------------------------------------------------
    # GET /download  -> stream dummy data
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path not in ("/download", "/"):
            self.send_error(404)
            return

        logger = self._make_logger()
        stats = StatsTracker()
        stop_event = threading.Event()
        blocksize: int = _config["blocksize"]
        interval: float = _config["interval"]
        timeout: float = _config["timeout_sec"]

        logger.log(EVENT_CONNECT, message=f"GET {self.path} from {self.client_address[0]}", mode=_config["mode"])

        # Send headers
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        # Stats reporter
        reporter = threading.Thread(target=self._report_loop,
                                    args=(logger, stats, stop_event, interval), daemon=True)
        reporter.start()

        reader = DummyReader(blocksize)
        event_type = EVENT_DISCONNECT
        msg = "Session ended"
        start = time.monotonic()

        try:
            self.connection.settimeout(timeout)
            while not stop_event.is_set():
                chunk = reader.read()
                # HTTP chunked encoding
                self.wfile.write(f"{len(chunk):X}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                stats.add_sent(len(chunk))

        except TimeoutError:
            event_type = EVENT_TIMEOUT
            msg = "Connection timed out"
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # Client closed connection (normal for --duration / Ctrl+C)
            event_type = EVENT_DISCONNECT
            # Suppress OS-specific error text; just note client disconnected
            msg = "Client disconnected"
        except OSError as e:
            event_type = EVENT_ERROR
            msg = str(e)
        finally:
            stop_event.set()
            reporter.join(timeout=interval + 1)
            elapsed = stats.elapsed()
            sent, recv = stats.totals()
            logger.log(event_type, elapsed_sec=elapsed,
                       bytes_sent=sent, bytes_recv=recv, message=msg, mode=_config["mode"])
            logger.close()

    # ------------------------------------------------------------------
    # POST /upload  -> receive and discard body (B案)
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        if self.path != "/upload":
            self.send_error(404)
            return

        logger = self._make_logger()
        stats = StatsTracker()
        stop_event = threading.Event()
        blocksize: int = _config["blocksize"]
        interval: float = _config["interval"]
        timeout: float = _config["timeout_sec"]

        logger.log(EVENT_CONNECT, message=f"POST {self.path} from {self.client_address[0]}", mode=_config["mode"])

        # Stats reporter
        reporter = threading.Thread(target=self._report_loop,
                                    args=(logger, stats, stop_event, interval), daemon=True)
        reporter.start()

        # Content-Length or chunked transfer
        content_length_str = self.headers.get("Content-Length")
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()

        event_type = EVENT_DISCONNECT
        msg = "Session ended"

        try:
            self.connection.settimeout(timeout)

            if transfer_encoding == "chunked":
                # Read chunked body and discard
                self._read_chunked(stats, blocksize, stop_event)
            elif content_length_str:
                remaining = int(content_length_str)
                while remaining > 0 and not stop_event.is_set():
                    to_read = min(blocksize, remaining)
                    data = self.rfile.read(to_read)
                    if not data:
                        break
                    stats.add_recv(len(data))
                    remaining -= len(data)
            else:
                # Read until connection closes
                while not stop_event.is_set():
                    data = self.rfile.read(blocksize)
                    if not data:
                        break
                    stats.add_recv(len(data))

            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        except TimeoutError:
            event_type = EVENT_TIMEOUT
            msg = "Connection timed out"
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            event_type = EVENT_DISCONNECT
            msg = "Client disconnected"
        except OSError as e:
            event_type = EVENT_ERROR
            msg = str(e)
        finally:
            stop_event.set()
            reporter.join(timeout=interval + 1)
            elapsed = stats.elapsed()
            sent, recv = stats.totals()
            logger.log(event_type, elapsed_sec=elapsed,
                       bytes_sent=sent, bytes_recv=recv, message=msg, mode=_config["mode"])
            logger.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_chunked(self, stats: StatsTracker, blocksize: int, stop: threading.Event) -> None:
        """Read HTTP chunked transfer body and discard."""
        while not stop.is_set():
            # Read chunk size line
            size_line = self.rfile.readline().strip()
            if not size_line:
                break
            try:
                chunk_size = int(size_line, 16)
            except ValueError:
                break
            if chunk_size == 0:
                self.rfile.read(2)  # trailing CRLF
                break
            # Read chunk data
            remaining = chunk_size
            while remaining > 0:
                to_read = min(blocksize, remaining)
                data = self.rfile.read(to_read)
                if not data:
                    return
                stats.add_recv(len(data))
                remaining -= len(data)
            self.rfile.read(2)  # CRLF after chunk

    @staticmethod
    def _report_loop(logger: TrafficLogger, stats: StatsTracker,
                     stop: threading.Event, interval: float) -> None:
        while not stop.wait(timeout=interval):
            snap = stats.snapshot()
            logger.log(
                "DATA",
                elapsed_sec=snap.elapsed_sec,
                bytes_sent=snap.total_bytes_sent,
                bytes_recv=snap.total_bytes_recv,
                bps_sent=snap.bps_sent,
                bps_recv=snap.bps_recv,
                message=f"interval {interval}s",
                mode=_config["mode"],
            )


class ThreadingHTTPServer(http.server.HTTPServer):
    """Multi-threaded HTTP server."""

    # Mix in threading support
    from socketserver import ThreadingMixIn
    # Apply threading
    pass


# Monkey-patch threading support
import socketserver
ThreadingHTTPServer = type(
    "ThreadingHTTPServer",
    (socketserver.ThreadingMixIn, http.server.HTTPServer),
    {"daemon_threads": True, "allow_reuse_address": True},
)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HTTP Traffic Test Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("port", type=int, help="Listen port")
    p.add_argument("--bind", default="0.0.0.0", help="Bind address")
    p.add_argument("--timeout", type=float, default=30.0, dest="timeout_sec",
                   help="Request timeout (seconds)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Stats log interval (seconds)")
    p.add_argument("--blocksize", type=int, default=65536,
                   help="Send/recv block size (bytes)")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download",
                   help="Transfer direction: download=server sends to client, upload=server receives")
    p.add_argument("--threshold", type=int, default=1000,
                   help="Data transfer rate threshold for warnings (bytes/sec)")
    p.add_argument("--table-format", action="store_true",
                   help="Use table format for console output instead of CSV")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Initialize rich output handler
    rich_output = RichTrafficOutput(threshold=args.threshold, use_table_format=args.table_format)

    # Populate module-level config (shared with handler)
    _config.update({
        "logdir": args.logdir,
        "timeout_sec": args.timeout_sec,
        "interval": args.interval,
        "blocksize": args.blocksize,
        "mode": args.mode,
        "rich_output": rich_output,
    })

    server = ThreadingHTTPServer((args.bind, args.port), TrafficHTTPHandler)

    rich_output.print_message(f"[HTTP Server] Listening on {args.bind}:{args.port}", "INFO")
    rich_output.print_message(f"[HTTP Server] GET /download  -> stream to client", "INFO")
    rich_output.print_message(f"[HTTP Server] POST /upload   -> receive from client", "INFO")
    rich_output.print_message(f"[HTTP Server] timeout={args.timeout_sec}s  interval={args.interval}s  "
          f"blocksize={args.blocksize}B", "INFO")
    rich_output.print_message("[HTTP Server] Press Ctrl+C to stop.", "INFO")

    def _shutdown(sig, frame):
        rich_output.print_message("\n[HTTP Server] Shutting down...", "INFO")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    server.serve_forever()
    rich_output.print_message("[HTTP Server] Stopped.", "INFO")


if __name__ == "__main__":
    main()
