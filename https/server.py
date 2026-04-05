"""
HTTPS Traffic Test Server
=========================
Usage:
    python server.py <port> [options]

If --cert / --key are not specified, a self-signed certificate is generated
dynamically at startup using the cryptography library.

Endpoints:
    GET  /download  -> Server streams dummy data to client
    POST /upload    -> Server reads and discards client body

Options:
    --bind ADDR       Bind address (default: 0.0.0.0)
    --timeout SEC     Request timeout in seconds (default: 30)
    --interval SEC    Stats log interval in seconds (default: 1)
    --blocksize N     Send/recv block size in bytes (default: 65536)
    --cert FILE       Path to PEM certificate file (optional)
    --key FILE        Path to PEM private key file (optional)
    --logdir DIR      Log directory (default: ./log_traffic)
"""

from __future__ import annotations

import argparse
import http.server
import signal
import socketserver
import ssl
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.logger import (
    EVENT_CONNECT, EVENT_DATA, EVENT_DISCONNECT, EVENT_ERROR, EVENT_TIMEOUT,
    TrafficLogger,
)
from common.stats import StatsTracker
from https.cert_utils import generate_self_signed_cert

PROTO = "HTTPS"

_config: dict = {}


class TrafficHTTPSHandler(http.server.BaseHTTPRequestHandler):
    """HTTPS request handler — identical logic to HTTP handler but runs over TLS."""

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass

    def _make_logger(self) -> TrafficLogger:
        client_ip, client_port = self.client_address[:2]
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
        )

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

        logger.log(EVENT_CONNECT, message=f"GET {self.path} from {self.client_address[0]}")

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        reporter = threading.Thread(
            target=self._report_loop,
            args=(logger, stats, stop_event, interval),
            daemon=True,
        )
        reporter.start()

        reader = DummyReader(blocksize)
        event_type = EVENT_DISCONNECT
        msg = "Session ended"

        try:
            self.connection.settimeout(timeout)
            while not stop_event.is_set():
                chunk = reader.read()
                self.wfile.write(f"{len(chunk):X}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                stats.add_sent(len(chunk))

        except TimeoutError:
            event_type = EVENT_TIMEOUT
            msg = "Connection timed out"
        except ssl.SSLError as e:
            # SSL EOF without close_notify = client disconnected abruptly (normal in test env)
            if "EOF" in str(e) or "ZERO_RETURN" in str(e):
                event_type = EVENT_DISCONNECT
                msg = f"Client disconnected (SSL EOF)"
            else:
                event_type = EVENT_ERROR
                msg = str(e)
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
                       bytes_sent=sent, bytes_recv=recv, message=msg)
            logger.close()

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

        logger.log(EVENT_CONNECT, message=f"POST {self.path} from {self.client_address[0]}")

        reporter = threading.Thread(
            target=self._report_loop,
            args=(logger, stats, stop_event, interval),
            daemon=True,
        )
        reporter.start()

        content_length_str = self.headers.get("Content-Length")
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()

        event_type = EVENT_DISCONNECT
        msg = "Session ended"

        try:
            self.connection.settimeout(timeout)

            if transfer_encoding == "chunked":
                self._read_chunked(stats, blocksize, stop_event)
            elif content_length_str:
                remaining = int(content_length_str)
                while remaining > 0 and not stop_event.is_set():
                    data = self.rfile.read(min(blocksize, remaining))
                    if not data:
                        break
                    stats.add_recv(len(data))
                    remaining -= len(data)
            else:
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
        except ssl.SSLError as e:
            if "EOF" in str(e) or "ZERO_RETURN" in str(e):
                event_type = EVENT_DISCONNECT
                msg = "Client disconnected (SSL EOF)"
            else:
                event_type = EVENT_ERROR
                msg = str(e)
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
                       bytes_sent=sent, bytes_recv=recv, message=msg)
            logger.close()

    def _read_chunked(self, stats: StatsTracker, blocksize: int, stop: threading.Event) -> None:
        while not stop.is_set():
            size_line = self.rfile.readline().strip()
            if not size_line:
                break
            try:
                chunk_size = int(size_line, 16)
            except ValueError:
                break
            if chunk_size == 0:
                self.rfile.read(2)
                break
            remaining = chunk_size
            while remaining > 0:
                data = self.rfile.read(min(blocksize, remaining))
                if not data:
                    return
                stats.add_recv(len(data))
                remaining -= len(data)
            self.rfile.read(2)

    @staticmethod
    def _report_loop(logger: TrafficLogger, stats: StatsTracker,
                     stop: threading.Event, interval: float) -> None:
        while not stop.wait(timeout=interval):
            snap = stats.snapshot()
            logger.log(
                EVENT_DATA,
                elapsed_sec=snap.elapsed_sec,
                bytes_sent=snap.total_bytes_sent,
                bytes_recv=snap.total_bytes_recv,
                bps_sent=snap.bps_sent,
                bps_recv=snap.bps_recv,
                message=f"interval {interval}s",
            )


ThreadingHTTPSServer = type(
    "ThreadingHTTPSServer",
    (socketserver.ThreadingMixIn, http.server.HTTPServer),
    {"daemon_threads": True, "allow_reuse_address": True},
)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HTTPS Traffic Test Server",
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
    p.add_argument("--cert", type=Path, default=None,
                   help="PEM certificate file path (auto-generated if omitted)")
    p.add_argument("--key", type=Path, default=None,
                   help="PEM private key file path (auto-generated if omitted)")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Certificate setup
    temp_cert: Path | None = None
    temp_key: Path | None = None

    if args.cert and args.key:
        cert_path = args.cert
        key_path = args.key
        print(f"[HTTPS Server] Using certificate: {cert_path}")
    else:
        print("[HTTPS Server] Generating self-signed certificate...")
        temp_cert, temp_key = generate_self_signed_cert(common_name=args.bind)
        cert_path = temp_cert
        key_path = temp_key
        print(f"[HTTPS Server] Temp cert: {cert_path}")

    # SSL context
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    _config.update({
        "logdir": args.logdir,
        "timeout_sec": args.timeout_sec,
        "interval": args.interval,
        "blocksize": args.blocksize,
    })

    server = ThreadingHTTPSServer((args.bind, args.port), TrafficHTTPSHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    print(f"[HTTPS Server] Listening on {args.bind}:{args.port}")
    print(f"[HTTPS Server] GET /download  -> stream to client")
    print(f"[HTTPS Server] POST /upload   -> receive from client")
    print(f"[HTTPS Server] timeout={args.timeout_sec}s  interval={args.interval}s  "
          f"blocksize={args.blocksize}B")
    print("[HTTPS Server] Press Ctrl+C to stop.")

    def _shutdown(sig, frame):
        print("\n[HTTPS Server] Shutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        # Clean up temp cert files
        if temp_cert and temp_cert.exists():
            temp_cert.unlink()
        if temp_key and temp_key.exists():
            temp_key.unlink()

    print("[HTTPS Server] Stopped.")


if __name__ == "__main__":
    main()
