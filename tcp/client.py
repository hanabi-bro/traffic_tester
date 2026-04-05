"""
TCP クライアント。

使用例:
    python tcp/client.py 192.168.1.1 5001 --duration 30
    python tcp/client.py 192.168.1.1 5001 --mode upload --duration 30
    python tcp/client.py 192.168.1.1 5001 --mode both --duration 30
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.ip_utils import resolve_server_ip, set_keepalive
from common.logger import TrafficLogger
from common.stats import StatsTracker

PROTO = "TCP"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TCP traffic test client")
    p.add_argument("host", help="Server host or IP")
    p.add_argument("port", type=int, help="Server port")
    p.add_argument("--timeout", type=int, default=30, metavar="SEC")
    p.add_argument("--interval", type=float, default=1.0, metavar="SEC")
    p.add_argument("--duration", type=int, default=0, metavar="SEC")
    p.add_argument("--blocksize", type=int, default=65536, metavar="N")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download")
    p.add_argument("--logdir", default="./log_traffic", metavar="DIR")
    return p.parse_args()


def main() -> None:
    cfg = parse_args()
    server_ip = resolve_server_ip(cfg.host)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    set_keepalive(sock)
    sock.settimeout(cfg.timeout)

    try:
        sock.connect((server_ip, cfg.port))
    except (OSError, TimeoutError) as e:
        print(f"[TCP client] Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    client_ip, client_port = sock.getsockname()
    connect_time = datetime.now()

    logger = TrafficLogger(
        logdir=cfg.logdir,
        role="client",
        proto=PROTO,
        server_ip=server_ip,
        server_port=cfg.port,
        client_ip=client_ip,
        client_port=client_port,
        connect_time=connect_time,
    )
    stats = StatsTracker()
    stop_event = threading.Event()
    deadline = time.monotonic() + cfg.duration if cfg.duration > 0 else None

    logger.write("CONNECT", message=f"Connected to {server_ip}:{cfg.port}")

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

    def send_loop():
        dummy = DummyReader(cfg.blocksize)
        try:
            while not stop_event.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                chunk = dummy.read()
                sock.sendall(chunk)
                stats.add_sent(len(chunk))
        except OSError:
            pass
        finally:
            stop_event.set()

    def recv_loop():
        try:
            while not stop_event.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                try:
                    data = sock.recv(cfg.blocksize)
                except socket.timeout:
                    continue
                if not data:
                    break
                stats.add_recv(len(data))
        except OSError:
            pass
        finally:
            stop_event.set()

    # duration タイマー
    def duration_timer():
        if deadline:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            stop_event.set()

    try:
        threads: list[threading.Thread] = []

        if cfg.mode in ("upload", "both"):
            t = threading.Thread(target=send_loop, daemon=True)
            t.start()
            threads.append(t)

        if cfg.mode in ("download", "both"):
            t = threading.Thread(target=recv_loop, daemon=True)
            t.start()
            threads.append(t)

        if deadline:
            dt = threading.Thread(target=duration_timer, daemon=True)
            dt.start()

        # メインスレッドは stop_event を待機
        stop_event.wait()

    except KeyboardInterrupt:
        stop_event.set()
    finally:
        sock.close()
        report_thread.join(timeout=cfg.interval + 1)
        s = stats.snapshot()
        logger.write(
            "DISCONNECT",
            bytes_sent=s["bytes_sent"],
            bytes_recv=s["bytes_recv"],
            message="Session ended",
        )
        logger.close()


if __name__ == "__main__":
    main()
