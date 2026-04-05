"""
UDP サーバ。
アプリケーション層で擬似セッション管理を実装。
1つのソケットで複数クライアントを処理する。

使用例:
    python udp/server.py 5005
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.logger import TrafficLogger
from common.stats import StatsTracker
from udp.frame import (
    DATA, FIN, FINACK, SYN, SYNACK,
    MODE_BOTH, MODE_DOWNLOAD, MODE_UPLOAD,
    MODE_NAME,
    Frame, pack, unpack,
)

PROTO = "UDP"
RECV_BUFSIZE = 65536


@dataclass
class SessionState:
    client_addr: tuple
    mode: int
    tx_seq: int = 1
    rx_next_expected: int = 1
    pkt_loss_total: int = 0
    pkt_ooo_total: int = 0
    logger: TrafficLogger = field(default=None)   # type: ignore[assignment]
    stats: StatsTracker = field(default_factory=StatsTracker)
    last_recv_time: float = field(default_factory=time.monotonic)
    send_thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    report_thread: threading.Thread | None = None
    tx_seq_lock: threading.Lock = field(default_factory=threading.Lock)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UDP traffic test server")
    p.add_argument("port", type=int)
    p.add_argument("--bind", default="0.0.0.0", metavar="ADDR")
    p.add_argument("--timeout", type=int, default=30, metavar="SEC")
    p.add_argument("--interval", type=float, default=1.0, metavar="SEC")
    p.add_argument("--logdir", default="./log_traffic", metavar="DIR")
    return p.parse_args()


class UDPServer:
    def __init__(self, cfg: argparse.Namespace) -> None:
        self.cfg = cfg
        self.sessions: dict[bytes, SessionState] = {}
        self.sessions_lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((cfg.bind, cfg.port))

    def run(self) -> None:
        cfg = self.cfg
        print(f"[UDP server] Listening on {cfg.bind}:{cfg.port}", flush=True)

        # タイムアウト監視スレッド
        timeout_thread = threading.Thread(target=self._timeout_watcher, daemon=True)
        timeout_thread.start()

        try:
            while True:
                try:
                    data, addr = self.sock.recvfrom(RECV_BUFSIZE)
                except OSError:
                    break
                self._dispatch(data, addr)
        except KeyboardInterrupt:
            print("\n[UDP server] Stopped.", flush=True)
        finally:
            self.sock.close()

    # ------------------------------------------------------------------
    # フレーム振り分け
    # ------------------------------------------------------------------
    def _dispatch(self, data: bytes, addr: tuple) -> None:
        frame = unpack(data)
        if frame is None:
            return

        if frame.type == SYN:
            self._handle_syn(frame, addr)
        elif frame.type == FIN:
            self._handle_fin(frame, addr)
        elif frame.type == DATA:
            self._handle_data(frame, addr)

    # ------------------------------------------------------------------
    # SYN 受信 → セッション開始
    # ------------------------------------------------------------------
    def _handle_syn(self, frame: Frame, addr: tuple) -> None:
        cfg = self.cfg
        sid = frame.session_id
        mode_byte = frame.payload[0] if frame.payload else MODE_DOWNLOAD

        client_ip, client_port = addr
        server_ip = self.sock.getsockname()[0]
        if server_ip == "0.0.0.0":
            server_ip = self._resolve_server_ip(client_ip)

        connect_time = datetime.now()
        logger = TrafficLogger(
            logdir=cfg.logdir,
            role="server",
            proto=PROTO,
            server_ip=server_ip,
            server_port=cfg.port,
            client_ip=client_ip,
            client_port=client_port,
            connect_time=connect_time,
        )

        with self.sessions_lock:
            if sid in self.sessions:
                # 重複 SYN: SYNACK だけ再送
                self.sock.sendto(pack(Frame(SYNACK, sid, 0)), addr)
                return

            sess = SessionState(
                client_addr=addr,
                mode=mode_byte,
                logger=logger,
            )
            self.sessions[sid] = sess

        mode_name = MODE_NAME.get(mode_byte, "unknown")
        logger.write(
            "CONNECT",
            pkt_loss=0,
            pkt_ooo=0,
            message=f"SYN received session_id={sid.hex()} mode={mode_name} from {client_ip}:{client_port}",
        )

        # SYNACK 送信
        self.sock.sendto(pack(Frame(SYNACK, sid, 0)), addr)

        # 送信スレッド起動（download / both）
        if mode_byte in (MODE_DOWNLOAD, MODE_BOTH):
            t = threading.Thread(
                target=self._send_loop,
                args=(sid,),
                daemon=True,
            )
            t.start()
            with self.sessions_lock:
                if sid in self.sessions:
                    self.sessions[sid].send_thread = t

        # interval レポートスレッド
        r = threading.Thread(target=self._report_loop, args=(sid,), daemon=True)
        r.start()
        with self.sessions_lock:
            if sid in self.sessions:
                self.sessions[sid].report_thread = r

    # ------------------------------------------------------------------
    # DATA 受信
    # ------------------------------------------------------------------
    def _handle_data(self, frame: Frame, addr: tuple) -> None:
        sid = frame.session_id
        with self.sessions_lock:
            sess = self.sessions.get(sid)
        if sess is None:
            return

        sess.last_recv_time = time.monotonic()
        recv_seq = frame.seq_no
        payload_len = len(frame.payload)
        sess.stats.add_recv(payload_len)

        # シーケンス判定
        exp = sess.rx_next_expected
        if recv_seq == exp:
            sess.rx_next_expected += 1
        elif recv_seq > exp:
            # gap 検出
            missing_count = recv_seq - exp
            sess.pkt_loss_total += missing_count
            sess.pkt_ooo_total += 1
            sess.logger.write(
                "LOSS",
                bytes_sent=sess.stats.bytes_sent,
                bytes_recv=sess.stats.bytes_recv,
                pkt_loss=sess.pkt_loss_total,
                pkt_ooo=sess.pkt_ooo_total,
                message=f"LOSS: seq {exp}-{recv_seq - 1} missing",
            )
            sess.logger.write(
                "OUT_OF_ORDER",
                bytes_sent=sess.stats.bytes_sent,
                bytes_recv=sess.stats.bytes_recv,
                pkt_seq=recv_seq,
                pkt_loss=sess.pkt_loss_total,
                pkt_ooo=sess.pkt_ooo_total,
                message=f"OUT_OF_ORDER: expected={exp} got={recv_seq}",
            )
            sess.rx_next_expected = recv_seq + 1
        else:
            # 遅延到着
            sess.pkt_ooo_total += 1
            sess.logger.write(
                "LATE_ARRIVAL",
                bytes_sent=sess.stats.bytes_sent,
                bytes_recv=sess.stats.bytes_recv,
                pkt_seq=recv_seq,
                pkt_loss=sess.pkt_loss_total,
                pkt_ooo=sess.pkt_ooo_total,
                message=f"LATE_ARRIVAL: seq={recv_seq}",
            )

    # ------------------------------------------------------------------
    # FIN 受信 → セッション終了
    # ------------------------------------------------------------------
    def _handle_fin(self, frame: Frame, addr: tuple) -> None:
        sid = frame.session_id
        with self.sessions_lock:
            sess = self.sessions.pop(sid, None)
        if sess is None:
            self.sock.sendto(pack(Frame(FINACK, sid, 0)), addr)
            return

        sess.stop_event.set()
        self.sock.sendto(pack(Frame(FINACK, sid, 0)), addr)

        s = sess.stats.snapshot()
        sess.logger.write(
            "DISCONNECT",
            bytes_sent=s["bytes_sent"],
            bytes_recv=s["bytes_recv"],
            pkt_loss=sess.pkt_loss_total,
            pkt_ooo=sess.pkt_ooo_total,
            message=(
                f"Session ended "
                f"pkt_recv={sess.rx_next_expected - 1} "
                f"loss={sess.pkt_loss_total} ooo={sess.pkt_ooo_total}"
            ),
        )
        sess.logger.close()

    # ------------------------------------------------------------------
    # DATA 送信ループ（download / both モード）
    # ------------------------------------------------------------------
    def _send_loop(self, sid: bytes) -> None:
        with self.sessions_lock:
            sess = self.sessions.get(sid)
        if sess is None:
            return

        blocksize = 1400  # UDP デフォルト blocksize
        dummy = DummyReader(blocksize)
        addr = sess.client_addr

        try:
            while not sess.stop_event.is_set():
                chunk = dummy.read()
                with sess.tx_seq_lock:
                    seq = sess.tx_seq
                    sess.tx_seq += 1
                frame_bytes = pack(Frame(DATA, sid, seq, chunk))
                try:
                    self.sock.sendto(frame_bytes, addr)
                    sess.stats.add_sent(len(chunk))
                except OSError:
                    break
        except Exception:
            pass

    # ------------------------------------------------------------------
    # interval レポートループ
    # ------------------------------------------------------------------
    def _report_loop(self, sid: bytes) -> None:
        with self.sessions_lock:
            sess = self.sessions.get(sid)
        if sess is None:
            return

        cfg = self.cfg
        while not sess.stop_event.wait(cfg.interval):
            with self.sessions_lock:
                alive = sid in self.sessions
            if not alive:
                break
            s = sess.stats.snapshot()
            sess.logger.write(
                "DATA",
                bytes_sent=s["bytes_sent"],
                bytes_recv=s["bytes_recv"],
                bps_sent=s["bps_sent"],
                bps_recv=s["bps_recv"],
                pkt_loss=sess.pkt_loss_total,
                pkt_ooo=sess.pkt_ooo_total,
                message=f"interval {s['interval_sec']}s",
            )

    # ------------------------------------------------------------------
    # タイムアウト監視
    # ------------------------------------------------------------------
    def _timeout_watcher(self) -> None:
        cfg = self.cfg
        while True:
            time.sleep(1)
            now = time.monotonic()
            timed_out: list[bytes] = []
            with self.sessions_lock:
                for sid, sess in list(self.sessions.items()):
                    if now - sess.last_recv_time > cfg.timeout:
                        timed_out.append(sid)
                for sid in timed_out:
                    self.sessions.pop(sid, None)

            for sid in timed_out:
                # セッションはすでに削除済みだが logger は残っている場合がある
                # タイムアウト前に取得したセッション情報でログ出力
                pass  # 下記で再取得は不要（sessions から既に削除）

            # タイムアウト処理（削除済みセッションのログは _handle_timeout_session で）

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------
    def _resolve_server_ip(self, client_ip: str) -> str:
        """bind が 0.0.0.0 の場合、クライアントへの経路から送信元 IP を推定する。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((client_ip, 1))
                return s.getsockname()[0]
        except Exception:
            return "0.0.0.0"


def main() -> None:
    cfg = parse_args()
    server = UDPServer(cfg)
    server.run()


if __name__ == "__main__":
    main()
