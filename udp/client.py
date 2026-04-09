"""
UDP Traffic Test Client
=======================
Usage:
    python client.py <host> <port> [options]

Options:
    --timeout SEC     Connection/idle timeout in seconds (default: 30)
    --interval SEC    Stats log interval in seconds (default: 1)
    --duration SEC    Run duration in seconds, 0=unlimited (default: 0)
    --blocksize N     Send block size in bytes (default: 1400)
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
from common.ip_utils import get_source_ip, resolve_host
from common.logger import (
    EVENT_CONNECT, EVENT_DATA, EVENT_DISCONNECT, EVENT_TIMEOUT,
    EVENT_LOSS, EVENT_OUT_OF_ORDER, EVENT_LATE_ARRIVAL,
    TrafficLogger,
)
from common.stats import StatsTracker
from udp.frame import (
    SYN, SYNACK, DATA, FIN, FINACK,
    MODE_DOWNLOAD, MODE_UPLOAD, MODE_BOTH,
    UDPFrame, pack_frame, unpack_frame,
    generate_session_id, string_to_mode, mode_to_string
)

PROTO = "UDP"


class UDPClient:
    """UDP client with session management and threading."""
    
    def __init__(
        self,
        server_host: str,
        server_port: int,
        timeout_sec: float,
        interval: float,
        duration: float,
        blocksize: int,
        mode: str,
        logdir: Path,
    ) -> None:
        self.server_ip = resolve_host(server_host)
        self.server_port = server_port
        self.timeout_sec = timeout_sec
        self.interval = interval
        self.duration = duration
        self.blocksize = blocksize
        self.mode_val = string_to_mode(mode)
        self.logdir = logdir
        
        # Client socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout_sec)
        
        # Session state
        self.session_id = generate_session_id()
        self.client_ip = get_source_ip(self.server_ip, self.server_port)
        self.client_port = 0  # Will be set after first send
        self.connect_time = datetime.now()
        
        # Sequence tracking
        self.tx_seq = 1  # Client send sequence
        self.rx_next_expected = 1  # Next expected from server
        self.pkt_loss_total = 0
        self.pkt_ooo_total = 0
        
        # Accumulated events for interval logging
        self.interval_loss_events = []  # List of (seq, missing_count) tuples
        self.interval_ooo_events = []   # List of (expected, got) tuples
        self.interval_late_events = []  # List of seq numbers
        
        # Logging and stats
        self.logger = None  # Will be initialized after getting client port
        self.stats = StatsTracker()
        
        # Control
        self.stop_event = threading.Event()
        self.connected = False
        self.server_addr = (self.server_ip, self.server_port)
    
    def run(self) -> None:
        """Run the UDP client."""
        print(f"[UDP Client] Connecting to {self.server_ip}:{self.server_port}  "
              f"mode={mode_to_string(self.mode_val)}  duration={self.duration}s  "
              f"interval={self.interval}s  blocksize={self.blocksize}B")
        print("[UDP Client] Press Ctrl+C to stop.")
        
        # Perform handshake
        if not self._handshake():
            return
        
        # Setup signal handlers
        def _shutdown(sig=None, frame=None):
            self.stop_event.set()
        
        signal.signal(signal.SIGINT, _shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _shutdown)
        
        # Start background threads
        threads = []
        
        # Receive thread
        recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        recv_thread.start()
        threads.append(recv_thread)
        
        # Send thread (if upload/both)
        if self.mode_val in (MODE_UPLOAD, MODE_BOTH):
            send_thread = threading.Thread(target=self._send_loop, daemon=True)
            send_thread.start()
            threads.append(send_thread)
        
        # Stats reporter thread
        reporter_thread = threading.Thread(target=self._report_loop, daemon=True)
        reporter_thread.start()
        threads.append(reporter_thread)
        
        # Duration deadline
        deadline: float | None = None
        if self.duration > 0:
            deadline = time.monotonic() + self.duration
        
        # Main loop - wait for stop or deadline
        try:
            while not self.stop_event.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            
            # Wait for threads
            for thread in threads:
                thread.join(timeout=2)
            
            # Send FIN and wait for FINACK
            self._send_fin()
            
            # Final log
            elapsed = self.stats.elapsed()
            sent, recv = self.stats.totals()
            if self.logger:
                # Log summary of any remaining accumulated events before DISCONNECT
                if self.interval_loss_events:
                    loss_count = len(self.interval_loss_events)
                    total_missing = sum(missing_count for _, missing_count in self.interval_loss_events)
                    self.logger.log(
                        EVENT_LOSS,
                        elapsed_sec=elapsed,
                        bytes_sent=sent,
                        bytes_recv=recv,
                        pkt_loss=self.pkt_loss_total,
                        pkt_ooo=self.pkt_ooo_total,
                        message=f"LOSS: {loss_count} loss events, {total_missing} packets missing final interval"
                    )
                
                if self.interval_ooo_events:
                    ooo_count = len(self.interval_ooo_events)
                    self.logger.log(
                        EVENT_OUT_OF_ORDER,
                        elapsed_sec=elapsed,
                        bytes_sent=sent,
                        bytes_recv=recv,
                        pkt_loss=self.pkt_loss_total,
                        pkt_ooo=self.pkt_ooo_total,
                        message=f"OUT_OF_ORDER: {ooo_count} out-of-order packets final interval"
                    )
                
                if self.interval_late_events:
                    late_count = len(self.interval_late_events)
                    self.logger.log(
                        EVENT_LATE_ARRIVAL,
                        elapsed_sec=elapsed,
                        bytes_sent=sent,
                        bytes_recv=recv,
                        pkt_loss=self.pkt_loss_total,
                        pkt_ooo=self.pkt_ooo_total,
                        message=f"LATE_ARRIVAL: {late_count} late arrivals final interval"
                    )
                
                self.logger.log(
                    EVENT_DISCONNECT,
                    elapsed_sec=elapsed,
                    bytes_sent=sent,
                    bytes_recv=recv,
                    pkt_loss=self.pkt_loss_total,
                    pkt_ooo=self.pkt_ooo_total,
                    message="Session ended"
                )
                self.logger.close()
            self.sock.close()
    
    def _handshake(self) -> bool:
        """Perform UDP handshake with server."""
        # Send SYN
        syn_frame = create_syn(self.session_id, self.mode_val)
        syn_data = pack_frame(syn_frame)
        
        retry_interval = self.timeout_sec / 3
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                self.sock.sendto(syn_data, self.server_addr)
                
                # Get client port after first send
                if self.client_port == 0:
                    self.client_port = self.sock.getsockname()[1]
                    # Initialize logger now that we have the client port
                    self.logger = TrafficLogger(
                        logdir=self.logdir,
                        proto=PROTO,
                        server_ip=self.server_ip,
                        server_port=self.server_port,
                        client_ip=self.client_ip,
                        client_port=self.client_port,
                        connect_time=self.connect_time,
                    )
                
                # Wait for SYNACK
                start_time = time.monotonic()
                while time.monotonic() - start_time < retry_interval:
                    try:
                        data, _ = self.sock.recvfrom(65535)
                        frame = unpack_frame(data)
                        
                        if frame and frame.type == SYNACK and frame.session_id == self.session_id:
                            # Handshake successful
                            self.connected = True
                            self.logger.log(
                                EVENT_CONNECT,
                                message=f"Connected session_id={self.session_id.hex()} mode={mode_to_string(self.mode_val)}"
                            )
                            return True
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                        
            except OSError:
                pass
        
        # Handshake failed
        if self.logger:
            self.logger.log(
                EVENT_TIMEOUT,
                message="Handshake failed - no SYNACK received"
            )
            self.logger.close()
        print("[UDP Client] Handshake failed", file=sys.stderr)
        return False
    
    def _receive_loop(self) -> None:
        """Receive loop for incoming packets."""
        while not self.stop_event.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
                frame = unpack_frame(data)
                
                if frame is None or frame.session_id != self.session_id:
                    continue
                
                if frame.type == DATA:
                    self._handle_data(frame)
                elif frame.type == FINACK:
                    # Server acknowledged our FIN
                    break
                    
            except socket.timeout:
                continue
            except OSError:
                break
    
    def _handle_data(self, frame: UDPFrame) -> None:
        """Handle incoming DATA frame."""
        self.stats.add_recv(len(frame.payload))
        
        # Sequence tracking for download/both modes
        if self.mode_val in (MODE_DOWNLOAD, MODE_BOTH):
            recv_seq = frame.seq_no
            expected = self.rx_next_expected
            
            if recv_seq == expected:
                # Normal packet
                self.rx_next_expected += 1
            elif recv_seq > expected:
                # Gap detected - packet loss
                missing_count = recv_seq - expected
                self.pkt_loss_total += missing_count
                self.pkt_ooo_total += 1
                self.rx_next_expected = recv_seq + 1
                
                # Accumulate loss event for interval logging
                self.interval_loss_events.append((recv_seq, missing_count))
                
                # Accumulate out-of-order event for interval logging
                self.interval_ooo_events.append((expected, recv_seq))
                
            elif recv_seq < expected:
                # Late arrival
                self.pkt_ooo_total += 1
                # Accumulate late arrival event for interval logging
                self.interval_late_events.append(recv_seq)
    
    def _send_loop(self) -> None:
        """Send loop for upload/both modes."""
        reader = DummyReader(self.blocksize)
        
        while not self.stop_event.is_set():
            chunk = reader.read()
            
            # Create and send data frame
            data_frame = create_data(self.session_id, self.tx_seq, chunk)
            try:
                self.sock.sendto(pack_frame(data_frame), self.server_addr)
                self.stats.add_sent(len(chunk))
                self.tx_seq += 1
            except OSError:
                break  # Server may be down
    
    def _report_loop(self) -> None:
        """Stats reporting loop."""
        while not self.stop_event.wait(timeout=self.interval):
            snap = self.stats.snapshot()
            
            # Log regular DATA interval
            self.logger.log(
                EVENT_DATA,
                elapsed_sec=snap.elapsed_sec,
                bytes_sent=snap.total_bytes_sent,
                bytes_recv=snap.total_bytes_recv,
                bps_sent=snap.bps_sent,
                bps_recv=snap.bps_recv,
                pkt_loss=self.pkt_loss_total,
                pkt_ooo=self.pkt_ooo_total,
                message=f"interval {self.interval}s"
            )
            
            # Log summary of accumulated events instead of individual events
            if self.interval_loss_events:
                loss_count = len(self.interval_loss_events)
                total_missing = sum(missing_count for _, missing_count in self.interval_loss_events)
                self.logger.log(
                    EVENT_LOSS,
                    elapsed_sec=snap.elapsed_sec,
                    bytes_sent=snap.total_bytes_sent,
                    bytes_recv=snap.total_bytes_recv,
                    pkt_loss=self.pkt_loss_total,
                    pkt_ooo=self.pkt_ooo_total,
                    message=f"LOSS: {loss_count} loss events, {total_missing} packets missing this interval"
                )
            
            if self.interval_ooo_events:
                ooo_count = len(self.interval_ooo_events)
                self.logger.log(
                    EVENT_OUT_OF_ORDER,
                    elapsed_sec=snap.elapsed_sec,
                    bytes_sent=snap.total_bytes_sent,
                    bytes_recv=snap.total_bytes_recv,
                    pkt_loss=self.pkt_loss_total,
                    pkt_ooo=self.pkt_ooo_total,
                    message=f"OUT_OF_ORDER: {ooo_count} out-of-order packets this interval"
                )
            
            if self.interval_late_events:
                late_count = len(self.interval_late_events)
                self.logger.log(
                    EVENT_LATE_ARRIVAL,
                    elapsed_sec=snap.elapsed_sec,
                    bytes_sent=snap.total_bytes_sent,
                    bytes_recv=snap.total_bytes_recv,
                    pkt_loss=self.pkt_loss_total,
                    pkt_ooo=self.pkt_ooo_total,
                    message=f"LATE_ARRIVAL: {late_count} late arrivals this interval"
                )
            
            # Clear accumulated events for next interval
            self.interval_loss_events.clear()
            self.interval_ooo_events.clear()
            self.interval_late_events.clear()
    
    def _send_fin(self) -> None:
        """Send FIN and wait for FINACK."""
        fin_frame = create_fin(self.session_id)
        fin_data = pack_frame(fin_frame)
        
        retry_interval = self.timeout_sec / 3
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                self.sock.sendto(fin_data, self.server_addr)
                
                # Wait for FINACK
                start_time = time.monotonic()
                while time.monotonic() - start_time < retry_interval:
                    try:
                        data, _ = self.sock.recvfrom(65535)
                        frame = unpack_frame(data)
                        
                        if frame and frame.type == FINACK and frame.session_id == self.session_id:
                            return  # Clean shutdown
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                        
            except OSError:
                pass


def create_syn(session_id: bytes, mode: int) -> UDPFrame:
    """Create a SYN frame."""
    return UDPFrame(
        type=SYN,
        session_id=session_id,
        seq_no=0,
        payload=bytes([mode])
    )


def create_data(session_id: bytes, seq_no: int, payload: bytes) -> UDPFrame:
    """Create a DATA frame."""
    return UDPFrame(
        type=DATA,
        session_id=session_id,
        seq_no=seq_no,
        payload=payload
    )


def create_fin(session_id: bytes) -> UDPFrame:
    """Create a FIN frame."""
    return UDPFrame(
        type=FIN,
        session_id=session_id,
        seq_no=0,
        payload=b""
    )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="UDP Traffic Test Client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("host", help="Server hostname or IP address")
    p.add_argument("port", type=int, help="Server port")
    p.add_argument("--timeout", type=float, default=30.0, dest="timeout_sec",
                   help="Connection/idle timeout (seconds)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Stats log interval (seconds)")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Run duration (seconds, 0=unlimited)")
    p.add_argument("--blocksize", type=int, default=1400,
                   help="Send block size (bytes)")
    p.add_argument("--mode", choices=["download", "upload", "both"], default="download",
                   help="Transfer direction (client perspective)")
    p.add_argument("--logdir", type=Path, default=Path("./log_traffic"),
                   help="Log output directory")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    
    client = UDPClient(
        server_host=args.host,
        server_port=args.port,
        timeout_sec=args.timeout_sec,
        interval=args.interval,
        duration=args.duration,
        blocksize=args.blocksize,
        mode=args.mode,
        logdir=args.logdir,
    )
    
    client.run()
    print("[UDP Client] Done.")


if __name__ == "__main__":
    main()
