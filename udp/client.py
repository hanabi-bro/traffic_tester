"""
UDP クライアント。
アプリケーション層でハンドシェイク・シーケンス管理を実装。

使用例:
    python udp/client.py 192.168.1.1 5005 --duration 30
    python udp/client.py 192.168.1.1 5005 --mode upload --duration 30
    python udp/client.py 192.168.1.1 5005 --mode both --duration 30
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.ip_utils import resolve_server_ip
from common.logger import TrafficLogger
from common.stats import StatsTracker
from udp.frame import (
    DATA, FIN, FINACK, SYN, SYNACK,
    MODE_MAP, MODE_NAME,
    Frame, pack, unpack,
)

PROTO = "UDP"
RECV_BUFSIZE = 65536


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UDP traffic test client")
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("--timeout", type=int, default=30, metavar="SEC")
    p.add_argument("--interval", type=float, default=1.0, metavar="SEC")
    p.add_argument("--duration", type=int, default=0, metavar="SEC")
    p.add_argument("--blocksize", type=int, default=1400, metavar="N")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download")
    p.add_argument("--logdir", default="./log_traffic", metavar="DIR")
    return p.parse_args()


class UDPClient:
    def __init__(self, cfg: argparse.Namespace, server_ip: str) -> None:
        self.cfg = cfg
        self.server_ip = server_ip
        self.server_addr = (server_ip, cfg.port)
        self.session_id = os.urandom(4)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(cfg.timeout / 3)  # リトライ間隔用

        self.stats = StatsTracker()
        self.stop_event = threading.Event()

        # シーケンス管理
        self.tx_seq = 1           # クライアント送信（upload）
        self.tx_seq_lock = threading.Lock()
        self.rx_next_expected = 1  # クライアント受信（download）
        self.pkt_loss_total = 0
        self.pkt_ooo_total = 0

        self.logger: TrafficLogger | None = None

    def run(self) -> None:
        cfg = self.cfg
        self.sock.bind(("", 0))
        client_ip, client_port = self.sock.getsockname()
        connect_time = datetime.now()

        self.logger = TrafficLogger(
            logdir=cfg.logdir,
            role="client",
            proto=PROTO,
            server_ip=self.server_ip,
            server_port=cfg.port,
            client_ip=client_ip,
            client_port=client_port,
            connect_time=connect_time,
        )

        mode_byte = MODE_MAP[cfg.mode]

        # ---- ハンドシェイク（SYN→SYNACK）----
        if not self._handshake(mode_byte):
            self.logger.write(
                "TIMEOUT",
                pkt_loss=0,
                pkt_ooo=0,
                message="Handshake failed: no SYNACK received",
            )
            self.logger.close()
            self.sock.close()
            return

        self.logger.write(
            "CONNECT",
            pkt_loss=0,
            pkt_ooo=0,
            message=f"Connected session_id={self.session_id.hex()} mode={cfg.mode}",
        )

        # ---- interval レポートスレッド ----
        report_thread = threading.Thread(target=self._report_loop, daemon=True)
        report_thread.start()

        deadline = time.monotonic() + cfg.duration if cfg.duration > 0 else None

        # ---- データ転送スレッド起動 ----
        threads: list[threading.Thread] = []
        if cfg.mode in ("upload", "both"):
            t = threading.Thread(target=self._send_loop, args=(deadline,), daemon=True)
            t.start()
            threads.append(t)

        if cfg.mode in ("download", "both"):
            t = threading.Thread(target=self._recv_loop, args=(deadline,), daemon=True)
            t.start()
            threads.append(t)
        elif cfg.mode == "upload":
            # upload専用: 送信ループが終われば stop
            pass

        # duration タイマー
        if deadline:
            def timer():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                self.stop_event.set()
            threading.Thread(target=timer, daemon=True).start()

        try:
            self.stop_event.wait()
        except KeyboardInterrupt:
            self.stop_event.set()

        # ---- FIN 送信 ----
        self._send_fin()

        report_thread.join(timeout=cfg.interval + 1)

        s = self.stats.snapshot()
        rx_total = self.rx_next_expected - 1
        tx_total = self.tx_seq - 1
        self.logger.write(
            "DISCONNECT",
            bytes_sent=s["bytes_sent"],
            bytes_recv=s["bytes_recv"],
            pkt_loss=self.pkt_loss_total,
            pkt_ooo=self.pkt_ooo_total,
            message=(
                f"Session ended "
                f"pkt_sent={tx_total} pkt_recv={rx_total} "
                f"loss={self.pkt_loss_total} ooo={self.pkt_ooo_total}"
            ),
        )
        self.logger.close()
        self.sock.close()

    # ------------------------------------------------------------------
    # ハンドシェイク
    # ------------------------------------------------------------------
    def _handshake(self, mode_byte: int) -> bool:
        cfg = self.cfg
        syn_frame = Frame(SYN, self.session_id, 0, bytes([mode_byte]))
        syn_bytes = pack(syn_frame)
        retry_interval = cfg.timeout / 3

        for attempt in range(3):
            self.sock.sendto(syn_bytes, self.server_addr)
            deadline = time.monotonic() + retry_interval
            while time.monotonic() < deadline:
                try:
                    data, _ = self.sock.recvfrom(RECV_BUFSIZE)
                except socket.timeout:
                    break
                frame = unpack(data)
                if (frame and frame.type == SYNACK
                        and frame.session_id == self.session_id):
                    # ソケットタイムアウトをデータ受信用に設定
                    self.sock.settimeout(1.0)
                    return True
        return False

    # ------------------------------------------------------------------
    # FIN 送信
    # ------------------------------------------------------------------
    def _send_fin(self) -> None:
        fin_bytes = pack(Frame(FIN, self.session_id, 0))
        for _ in range(3):
            try:
                self.sock.sendto(fin_bytes, self.server_addr)
                deadline = time.monotonic() + self.cfg.timeout / 3
                while time.monotonic() < deadline:
                    try:
                        data, _ = self.sock.recvfrom(RECV_BUFSIZE)
                    except socket.timeout:
                        break
                    frame = unpack(data)
                    if (frame and frame.type == FINACK
                            and frame.session_id == self.session_id):
                        return
            except OSError:
                break

    # ------------------------------------------------------------------
    # DATA 送信ループ（upload / both）
    # ------------------------------------------------------------------
    def _send_loop(self, deadline: float | None) -> None:
        cfg = self.cfg
        dummy = DummyReader(cfg.blocksize)
        try:
            while not self.stop_event.is_set():
                if deadline and time.monotonic() >= deadline:
                    self.stop_event.set()
                    break
                chunk = dummy.read()
                with self.tx_seq_lock:
                    seq = self.tx_seq
                    self.tx_seq += 1
                frame_bytes = pack(Frame(DATA, self.session_id, seq, chunk))
                try:
                    self.sock.sendto(frame_bytes, self.server_addr)
                    self.stats.add_sent(len(chunk))
                except OSError:
                    self.stop_event.set()
                    break
        except Exception:
            self.stop_event.set()

    # ------------------------------------------------------------------
    # DATA 受信ループ（download / both）
    # ------------------------------------------------------------------
    def _recv_loop(self, deadline: float | None) -> None:
        try:
            while not self.stop_event.is_set():
                if deadline and time.monotonic() >= deadline:
                    self.stop_event.set()
                    break
                try:
                    data, addr = self.sock.recvfrom(RECV_BUFSIZE)
                except socket.timeout:
                    continue
                except OSError:
                    self.stop_event.set()
                    break

                frame = unpack(data)
                if frame is None or frame.session_id != self.session_id:
                    continue
                if frame.type != DATA:
                    continue

                payload_len = len(frame.payload)
                self.stats.add_recv(payload_len)
                self._process_seq(frame.seq_no)

        except Exception:
            self.stop_event.set()

    def _process_seq(self, recv_seq: int) -> None:
        """シーケンス番号を評価してgap/遅延到着を検出する。"""
        exp = self.rx_next_expected
        s = self.stats

        if recv_seq == exp:
            self.rx_next_expected += 1
        elif recv_seq > exp:
            missing_count = recv_seq - exp
            self.pkt_loss_total += missing_count
            self.pkt_ooo_total += 1
            self.logger.write(
                "LOSS",
                bytes_sent=s.bytes_sent,
                bytes_recv=s.bytes_recv,
                pkt_loss=self.pkt_loss_total,
                pkt_ooo=self.pkt_ooo_total,
                message=f"LOSS: seq {exp}-{recv_seq - 1} missing",
            )
            self.logger.write(
                "OUT_OF_ORDER",
                bytes_sent=s.bytes_sent,
                bytes_recv=s.bytes_recv,
                pkt_seq=recv_seq,
                pkt_loss=self.pkt_loss_total,
                pkt_ooo=self.pkt_ooo_total,
                message=f"OUT_OF_ORDER: expected={exp} got={recv_seq}",
            )
            self.rx_next_expected = recv_seq + 1
        else:
            self.pkt_ooo_total += 1
            self.logger.write(
                "LATE_ARRIVAL",
                bytes_sent=s.bytes_sent,
                bytes_recv=s.bytes_recv,
                pkt_seq=recv_seq,
                pkt_loss=self.pkt_loss_total,
                pkt_ooo=self.pkt_ooo_total,
                message=f"LATE_ARRIVAL: seq={recv_seq}",
            )

    # ------------------------------------------------------------------
    # interval レポートループ
    # ------------------------------------------------------------------
    def _report_loop(self) -> None:
        cfg = self.cfg
        while not self.stop_event.wait(cfg.interval):
            s = self.stats.snapshot()
            self.logger.write(
                "DATA",
                bytes_sent=s["bytes_sent"],
                bytes_recv=s["bytes_recv"],
                bps_sent=s["bps_sent"],
                bps_recv=s["bps_recv"],
                pkt_loss=self.pkt_loss_total,
                pkt_ooo=self.pkt_ooo_total,
                message=f"interval {s['interval_sec']}s",
            )


def main() -> None:
    cfg = parse_args()
    server_ip = resolve_server_ip(cfg.host)
    client = UDPClient(cfg, server_ip)
    try:
        client.run()
    except KeyboardInterrupt:
        client.stop_event.set()


if __name__ == "__main__":
    main()
