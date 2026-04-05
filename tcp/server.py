"""
TCP サーバ。

使用例:
    python tcp/server.py 5001
    python tcp/server.py 5001 --mode upload
    python tcp/server.py 5001 --mode both
"""
from __future__ import annotations

import argparse
import socketserver
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# パッケージ外から直接実行できるようにパスを通す
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.ip_utils import set_keepalive
from common.logger import TrafficLogger
from common.stats import StatsTracker

PROTO = "TCP"


class TCPHandler(socketserver.BaseRequestHandler):
    """クライアント接続ごとに生成されるハンドラ。"""

    # サーバから注入される設定（クラス変数）
    cfg: argparse.Namespace

    def handle(self) -> None:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        sock = self.request
        set_keepalive(sock)
        sock.settimeout(cfg.timeout)

        client_ip, client_port = self.client_address
        server_ip = sock.getsockname()[0]
        server_port = cfg.port
        connect_time = datetime.now()

        logger = TrafficLogger(
            logdir=cfg.logdir,
            role="server",
            proto=PROTO,
            server_ip=server_ip,
            server_port=server_port,
            client_ip=client_ip,
            client_port=client_port,
            connect_time=connect_time,
        )
        stats = StatsTracker()
        stop_event = threading.Event()

        logger.write("CONNECT", message=f"Client connected {client_ip}:{client_port}")

        # interval レポートスレッド
        def report_loop():
            while not stop_event.wait(cfg.interval):
                s = stats.snapshot()
                logger.write(
                    "DATA",
                    bytes_sent=s["bytes_sent"],
                    bytes_recv=s["bytes_recv"],
                    bps_sent=s["bps_sent"],
                    bps_recv=s["bps_recv"],
                    message=f"interval {s['interval_sec']}s",
                )

        report_thread = threading.Thread(target=report_loop, daemon=True)
        report_thread.start()

        try:
            mode = cfg.mode  # "download" / "upload" / "both"

            if mode in ("download", "both"):
                send_thread = threading.Thread(
                    target=self._send_loop,
                    args=(sock, cfg.blocksize, stats, stop_event),
                    daemon=True,
                )
                send_thread.start()
            else:
                send_thread = None

            if mode in ("upload", "both"):
                self._recv_loop(sock, stats, stop_event)
            else:
                # download専用: ソケットが閉じるまで待機
                while not stop_event.is_set():
                    try:
                        data = sock.recv(4096)
                        if not data:
                            break
                    except OSError:
                        break

            stop_event.set()
            if send_thread:
                send_thread.join(timeout=2)

            s = stats.snapshot()
            logger.write(
                "DISCONNECT",
                bytes_sent=s["bytes_sent"],
                bytes_recv=s["bytes_recv"],
                message="Session ended",
            )
        except TimeoutError:
            stop_event.set()
            s = stats.snapshot()
            logger.write(
                "TIMEOUT",
                bytes_sent=s["bytes_sent"],
                bytes_recv=s["bytes_recv"],
                message=f"Timeout after {cfg.timeout}s",
            )
        except OSError as e:
            stop_event.set()
            s = stats.snapshot()
            logger.write(
                "ERROR",
                bytes_sent=s["bytes_sent"],
                bytes_recv=s["bytes_recv"],
                message=str(e),
            )
        finally:
            report_thread.join(timeout=cfg.interval + 1)
            logger.close()

    def _send_loop(
        self,
        sock,
        blocksize: int,
        stats: StatsTracker,
        stop_event: threading.Event,
    ) -> None:
        dummy = DummyReader(blocksize)
        try:
            while not stop_event.is_set():
                chunk = dummy.read()
                sock.sendall(chunk)
                stats.add_sent(len(chunk))
        except OSError:
            pass

    def _recv_loop(
        self,
        sock,
        stats: StatsTracker,
        stop_event: threading.Event,
    ) -> None:
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                stats.add_recv(len(data))
        except OSError:
            pass


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cfg: argparse.Namespace):
        self.cfg = cfg
        super().__init__((cfg.bind, cfg.port), TCPHandler)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TCP traffic test server")
    p.add_argument("port", type=int, help="Listen port")
    p.add_argument("--bind", default="0.0.0.0", metavar="ADDR", help="Bind address")
    p.add_argument("--timeout", type=int, default=30, metavar="SEC")
    p.add_argument("--interval", type=float, default=1.0, metavar="SEC")
    p.add_argument("--blocksize", type=int, default=65536, metavar="N")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download")
    p.add_argument("--logdir", default="./log_traffic", metavar="DIR")
    return p.parse_args()


def main() -> None:
    cfg = parse_args()
    print(f"[TCP server] Listening on {cfg.bind}:{cfg.port} mode={cfg.mode}", flush=True)
    with ThreadingTCPServer(cfg) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[TCP server] Stopped.", flush=True)


if __name__ == "__main__":
    main()
