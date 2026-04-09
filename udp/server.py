"""
UDP Traffic Test Server
=======================
Usage:
    python server.py <port> [options]

Options:
    --bind ADDR       Bind address (default: 0.0.0.0)
    --timeout SEC     Client idle timeout in seconds (default: 30)
    --interval SEC    Stats log interval in seconds (default: 1)
    --logdir DIR      Log directory (default: ./log_traffic)
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.dummy import DummyReader
from common.logger import (
    EVENT_CONNECT, EVENT_DATA, EVENT_DISCONNECT, EVENT_ERROR, EVENT_TIMEOUT,
    EVENT_LOSS, EVENT_OUT_OF_ORDER, EVENT_LATE_ARRIVAL,
    TrafficLogger,
)
from common.stats import StatsTracker
from udp.frame import (
    SYN, SYNACK, DATA, FIN, FINACK,
    MODE_DOWNLOAD, MODE_UPLOAD, MODE_BOTH,
    UDPFrame, pack_frame, unpack_frame,
    mode_to_string, type_to_string
)

PROTO = "UDP"


@dataclass
class SessionState:
    """Per-client session state."""
    client_addr: tuple[str, int]           # (ip, port)
    mode: int                               # MODE_DOWNLOAD/MODE_UPLOAD/MODE_BOTH
    tx_seq: int = 1                         # Server send sequence counter
    rx_next_expected: int = 1                # Next expected sequence from client
    pkt_loss_total: int = 0                 # Total lost packets
    pkt_ooo_total: int = 0                  # Total out-of-order packets
    logger: TrafficLogger = field(init=False)
    stats: StatsTracker = field(default_factory=StatsTracker)
    last_recv_time: float = field(default_factory=time.monotonic)
    send_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    connect_time: datetime = field(default_factory=datetime.now)


class UDPServer:
    """UDP server with session management and threading."""
    
    def __init__(
        self,
        bind_addr: str,
        port: int,
        logdir: Path,
        timeout_sec: float,
        interval: float,
    ) -> None:
        self.bind_addr = bind_addr
        self.port = port
        self.logdir = logdir
        self.timeout_sec = timeout_sec
        self.interval = interval
        
        # Session management
        self.sessions: Dict[bytes, SessionState] = {}
        self.sessions_lock = threading.Lock()
        
        # Server socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(1.0)  # Add 1-second timeout for quick shutdown
        self.sock.bind((bind_addr, port))
        
        # Shutdown control
        self._shutdown_event = threading.Event()
        
    def run(self) -> None:
        """Main server loop."""
        print(f"[UDP Server] Listening on {self.bind_addr}:{self.port}  "
              f"timeout={self.timeout_sec}s  interval={self.interval}s")
        print("[UDP Server] Press Ctrl+C to stop.")
        
        # Start background threads
        timeout_thread = threading.Thread(target=self._timeout_monitor, daemon=True)
        timeout_thread.start()
        
        try:
            self._receive_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
    
    def _receive_loop(self) -> None:
        """Main receive loop for all incoming packets."""
        while not self._shutdown_event.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)  # Max UDP size
                self._handle_packet(data, addr)
            except socket.timeout:
                # Timeout is normal - allows checking shutdown event
                continue
            except OSError:
                if not self._shutdown_event.is_set():
                    continue
                break
    
    def _handle_packet(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming packet."""
        frame = unpack_frame(data)
        if frame is None:
            return  # Invalid frame, ignore
        
        with self.sessions_lock:
            session = self.sessions.get(frame.session_id)
            
            if frame.type == SYN:
                self._handle_syn(frame, addr)
            elif session is None:
                # Packet for unknown session, ignore
                return
            elif frame.type == DATA:
                self._handle_data(session, frame)
            elif frame.type == FIN:
                self._handle_fin(session, frame)
            else:
                # Unknown or unexpected frame type
                session.logger.log(
                    EVENT_ERROR,
                    elapsed_sec=time.monotonic() - session.connect_time.timestamp(),
                    message=f"Unexpected frame type: {type_to_string(frame.type)}"
                )
    
    def _handle_syn(self, frame: UDPFrame, addr: tuple[str, int]) -> None:
        """Handle SYN packet - create new session."""
        if frame.session_id in self.sessions:
            return  # Session already exists
        
        mode = frame.mode
        if mode is None:
            return  # Invalid SYN
        
        # Create logger
        server_ip, server_port = self.sock.getsockname()
        logger = TrafficLogger(
            logdir=self.logdir,
            proto=PROTO,
            server_ip=server_ip,
            server_port=server_port,
            client_ip=addr[0],
            client_port=addr[1],
            role="server",
        )
        
        # Create session
        session = SessionState(
            client_addr=addr,
            mode=mode,
        )
        session.logger = logger
        
        self.sessions[frame.session_id] = session
        
        # Log connection
        logger.log(
            EVENT_CONNECT,
            message=f"Connected session_id={frame.session_id.hex()} mode={mode_to_string(mode)}"
        )
        
        # Send SYNACK
        synack_frame = create_synack(frame.session_id)
        self.sock.sendto(pack_frame(synack_frame), addr)
        
        # Start data sender thread if needed
        if mode in (MODE_DOWNLOAD, MODE_BOTH):
            session.send_thread = threading.Thread(
                target=self._send_loop,
                args=(session, frame.session_id),
                daemon=True
            )
            session.send_thread.start()
        
        # Start stats reporter
        threading.Thread(
            target=self._report_loop,
            args=(session,),
            daemon=True
        ).start()
    
    def _handle_data(self, session: SessionState, frame: UDPFrame) -> None:
        """Handle DATA packet - update sequence tracking."""
        session.last_recv_time = time.monotonic()
        session.stats.add_recv(len(frame.payload))
        
        # Sequence tracking for upload/both modes
        if session.mode in (MODE_UPLOAD, MODE_BOTH):
            recv_seq = frame.seq_no
            expected = session.rx_next_expected
            
            if recv_seq == expected:
                # Normal packet
                session.rx_next_expected += 1
            elif recv_seq > expected:
                # Gap detected - packet loss
                missing_count = recv_seq - expected
                session.pkt_loss_total += missing_count
                session.pkt_ooo_total += 1
                session.rx_next_expected = recv_seq + 1
                
                # Log loss
                missing_seq = list(range(expected, recv_seq))
                session.logger.log(
                    EVENT_LOSS,
                    elapsed_sec=time.monotonic() - session.connect_time.timestamp(),
                    bytes_sent=session.stats.totals()[0],
                    bytes_recv=session.stats.totals()[1],
                    pkt_seq=recv_seq,
                    pkt_loss=session.pkt_loss_total,
                    pkt_ooo=session.pkt_ooo_total,
                    message=f"LOSS: seq {missing_seq[0]}-{missing_seq[-1]} missing"
                )
                
                # Log out of order
                session.logger.log(
                    EVENT_OUT_OF_ORDER,
                    elapsed_sec=time.monotonic() - session.connect_time.timestamp(),
                    bytes_sent=session.stats.totals()[0],
                    bytes_recv=session.stats.totals()[1],
                    pkt_seq=recv_seq,
                    pkt_loss=session.pkt_loss_total,
                    pkt_ooo=session.pkt_ooo_total,
                    message=f"OUT_OF_ORDER: expected={expected} got={recv_seq}"
                )
            elif recv_seq < expected:
                # Late arrival
                session.pkt_ooo_total += 1
                session.logger.log(
                    EVENT_LATE_ARRIVAL,
                    elapsed_sec=time.monotonic() - session.connect_time.timestamp(),
                    bytes_sent=session.stats.totals()[0],
                    bytes_recv=session.stats.totals()[1],
                    pkt_seq=recv_seq,
                    pkt_loss=session.pkt_loss_total,
                    pkt_ooo=session.pkt_ooo_total,
                    message=f"LATE_ARRIVAL: seq={recv_seq}"
                )
    
    def _handle_fin(self, session: SessionState, frame: UDPFrame) -> None:
        """Handle FIN packet - close session."""
        # Send FINACK
        finack_frame = create_finack(frame.session_id)
        try:
            self.sock.sendto(pack_frame(finack_frame), session.client_addr)
        except OSError:
            pass  # Client may have already closed
        
        # Log disconnection
        elapsed = time.monotonic() - session.connect_time.timestamp()
        sent, recv = session.stats.totals()
        session.logger.log(
            EVENT_DISCONNECT,
            elapsed_sec=elapsed,
            bytes_sent=sent,
            bytes_recv=recv,
            pkt_loss=session.pkt_loss_total,
            pkt_ooo=session.pkt_ooo_total,
            message=f"Session ended pkt_sent={session.tx_seq-1} pkt_recv={session.rx_next_expected-1} "
                   f"loss={session.pkt_loss_total} ooo={session.pkt_ooo_total}"
        )
        
        # Stop and cleanup
        session.stop_event.set()
        if session.send_thread:
            session.send_thread.join(timeout=0.5)  # Reduced timeout
        session.logger.close()
        
        # Remove from sessions
        self.sessions.pop(frame.session_id, None)
    
    def _send_loop(self, session: SessionState, session_id: bytes) -> None:
        """Send data loop for download/both modes."""
        reader = DummyReader(1400)  # UDP default blocksize
        
        while not session.stop_event.is_set():
            chunk = reader.read()
            
            # Create and send data frame
            data_frame = create_data(session_id, session.tx_seq, chunk)
            try:
                self.sock.sendto(pack_frame(data_frame), session.client_addr)
                session.stats.add_sent(len(chunk))
                session.tx_seq += 1
            except OSError:
                break  # Client may have closed
    
    def _report_loop(self, session: SessionState) -> None:
        """Stats reporting loop."""
        while not session.stop_event.wait(timeout=self.interval):
            snap = session.stats.snapshot()
            session.logger.log(
                EVENT_DATA,
                elapsed_sec=snap.elapsed_sec,
                bytes_sent=snap.total_bytes_sent,
                bytes_recv=snap.total_bytes_recv,
                bps_sent=snap.bps_sent,
                bps_recv=snap.bps_recv,
                pkt_loss=session.pkt_loss_total,
                pkt_ooo=session.pkt_ooo_total,
                message=f"interval {self.interval}s"
            )
    
    def _timeout_monitor(self) -> None:
        """Background thread to monitor session timeouts."""
        while not self._shutdown_event.wait(timeout=1):
            now = time.monotonic()
            timeout_sessions = []
            
            with self.sessions_lock:
                for session_id, session in list(self.sessions.items()):
                    if now - session.last_recv_time > self.timeout_sec:
                        timeout_sessions.append((session_id, session))
            
            for session_id, session in timeout_sessions:
                # Log timeout
                elapsed = time.monotonic() - session.connect_time.timestamp()
                sent, recv = session.stats.totals()
                session.logger.log(
                    EVENT_TIMEOUT,
                    elapsed_sec=elapsed,
                    bytes_sent=sent,
                    bytes_recv=recv,
                    pkt_loss=session.pkt_loss_total,
                    pkt_ooo=session.pkt_ooo_total,
                    message="Session timeout"
                )
                
                # Cleanup
                session.stop_event.set()
                if session.send_thread:
                    session.send_thread.join(timeout=0.5)  # Reduced timeout
                session.logger.close()
                
                with self.sessions_lock:
                    self.sessions.pop(session_id, None)
    
    def _shutdown(self) -> None:
        """Shutdown server and cleanup all sessions."""
        self._shutdown_event.set()
        
        # Close all sessions quickly
        with self.sessions_lock:
            for session in self.sessions.values():
                session.stop_event.set()
                if session.send_thread:
                    session.send_thread.join(timeout=0.5)  # Reduced timeout
                session.logger.close()
            self.sessions.clear()
        
        # Close socket
        self.sock.close()
        print("[UDP Server] Stopped.")


def create_synack(session_id: bytes) -> UDPFrame:
    """Create a SYNACK frame."""
    return UDPFrame(
        type=SYNACK,
        session_id=session_id,
        seq_no=0,
        payload=b""
    )


def create_finack(session_id: bytes) -> UDPFrame:
    """Create a FINACK frame."""
    return UDPFrame(
        type=FINACK,
        session_id=session_id,
        seq_no=0,
        payload=b""
    )


def create_data(session_id: bytes, seq_no: int, payload: bytes) -> UDPFrame:
    """Create a DATA frame."""
    return UDPFrame(
        type=DATA,
        session_id=session_id,
        seq_no=seq_no,
        payload=payload
    )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="UDP Traffic Test Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("port", type=int, help="Listen port")
    p.add_argument("--bind", default="0.0.0.0", help="Bind address")
    p.add_argument("--timeout", type=float, default=30.0, dest="timeout_sec",
                   help="Client idle timeout (seconds)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Stats log interval (seconds)")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    
    server = UDPServer(
        bind_addr=args.bind,
        port=args.port,
        logdir=args.logdir,
        timeout_sec=args.timeout_sec,
        interval=args.interval,
    )
    
    def _shutdown(sig=None, frame=None):
        print("\n[UDP Server] Shutting down...", flush=True)
        server._shutdown_event.set()
    
    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)
    
    server.run()


if __name__ == "__main__":
    main()
