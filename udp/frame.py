"""
UDP frame definitions and utilities for traffic test tool.
Implements the UDP application-layer protocol with sequence numbers.
"""

from __future__ import annotations

import os
import struct
from typing import NamedTuple

# Frame type constants
SYN     = 0x01  # Client→Server: Session start request
SYNACK  = 0x02  # Server→Client: Session start acknowledgment
DATA    = 0x03  # Bidirectional: Data transfer
FIN     = 0x04  # Client→Server: Session end request
FINACK  = 0x05  # Server→Client: Session end acknowledgment

# Mode constants for SYN payload
MODE_DOWNLOAD = 0x01  # Server→Client data transfer
MODE_UPLOAD   = 0x02  # Client→Server data transfer
MODE_BOTH     = 0x03  # Bidirectional data transfer

# Frame format: !B4sI (big-endian)
# type (1 byte) + session_id (4 bytes) + seq_no (4 bytes) = 9 bytes header
FRAME_HEADER_FORMAT = "!B4sI"
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER_FORMAT)

# Default UDP blocksize (payload only, not including header)
DEFAULT_BLOCKSIZE = 1400


class UDPFrame(NamedTuple):
    """UDP frame representation."""
    type: int           # Frame type (SYN, SYNACK, DATA, FIN, FINACK)
    session_id: bytes   # 4-byte session identifier
    seq_no: int         # Sequence number (32-bit unsigned)
    payload: bytes      # Payload data (0 to blocksize bytes)

    @property
    def mode(self) -> int | None:
        """Extract mode from SYN frame payload, None for other frames."""
        if self.type == SYN and len(self.payload) >= 1:
            return self.payload[0]
        return None

    @property
    def total_size(self) -> int:
        """Total frame size including header."""
        return FRAME_HEADER_SIZE + len(self.payload)


def pack_frame(frame: UDPFrame) -> bytes:
    """
    Pack UDP frame into bytes.
    
    Args:
        frame: UDPFrame to pack
        
    Returns:
        Serialized frame bytes
    """
    header = struct.pack(FRAME_HEADER_FORMAT, frame.type, frame.session_id, frame.seq_no)
    return header + frame.payload


def unpack_frame(data: bytes) -> UDPFrame | None:
    """
    Unpack bytes into UDP frame.
    
    Args:
        data: Raw frame data
        
    Returns:
        UDPFrame if valid, None if data too short
    """
    if len(data) < FRAME_HEADER_SIZE:
        return None
    
    type_val, session_id, seq_no = struct.unpack(FRAME_HEADER_FORMAT, data[:FRAME_HEADER_SIZE])
    payload = data[FRAME_HEADER_SIZE:]
    
    return UDPFrame(
        type=type_val,
        session_id=session_id,
        seq_no=seq_no,
        payload=payload
    )


def generate_session_id() -> bytes:
    """
    Generate a random 4-byte session ID.
    
    Returns:
        4-byte random identifier
    """
    return os.urandom(4)


def create_syn(session_id: bytes, mode: int) -> UDPFrame:
    """
    Create a SYN frame.
    
    Args:
        session_id: 4-byte session identifier
        mode: Transfer mode (MODE_DOWNLOAD/MODE_UPLOAD/MODE_BOTH)
        
    Returns:
        SYN frame
    """
    return UDPFrame(
        type=SYN,
        session_id=session_id,
        seq_no=0,
        payload=bytes([mode])
    )


def create_synack(session_id: bytes) -> UDPFrame:
    """
    Create a SYNACK frame.
    
    Args:
        session_id: 4-byte session identifier
        
    Returns:
        SYNACK frame
    """
    return UDPFrame(
        type=SYNACK,
        session_id=session_id,
        seq_no=0,
        payload=b""
    )


def create_data(session_id: bytes, seq_no: int, payload: bytes) -> UDPFrame:
    """
    Create a DATA frame.
    
    Args:
        session_id: 4-byte session identifier
        seq_no: Sequence number
        payload: Data payload
        
    Returns:
        DATA frame
    """
    return UDPFrame(
        type=DATA,
        session_id=session_id,
        seq_no=seq_no,
        payload=payload
    )


def create_fin(session_id: bytes) -> UDPFrame:
    """
    Create a FIN frame.
    
    Args:
        session_id: 4-byte session identifier
        
    Returns:
        FIN frame
    """
    return UDPFrame(
        type=FIN,
        session_id=session_id,
        seq_no=0,
        payload=b""
    )


def create_finack(session_id: bytes) -> UDPFrame:
    """
    Create a FINACK frame.
    
    Args:
        session_id: 4-byte session identifier
        
    Returns:
        FINACK frame
    """
    return UDPFrame(
        type=FINACK,
        session_id=session_id,
        seq_no=0,
        payload=b""
    )


def mode_to_string(mode: int) -> str:
    """
    Convert mode constant to string.
    
    Args:
        mode: Mode constant
        
    Returns:
        String representation
    """
    mode_map = {
        MODE_DOWNLOAD: "download",
        MODE_UPLOAD: "upload",
        MODE_BOTH: "both"
    }
    return mode_map.get(mode, "unknown")


def string_to_mode(mode_str: str) -> int:
    """
    Convert string to mode constant.
    
    Args:
        mode_str: Mode string ("download", "upload", "both")
        
    Returns:
        Mode constant
    """
    mode_map = {
        "download": MODE_DOWNLOAD,
        "upload": MODE_UPLOAD,
        "both": MODE_BOTH
    }
    return mode_map.get(mode_str.lower(), MODE_DOWNLOAD)


def type_to_string(type_val: int) -> str:
    """
    Convert frame type constant to string.
    
    Args:
        type_val: Frame type constant
        
    Returns:
        String representation
    """
    type_map = {
        SYN: "SYN",
        SYNACK: "SYNACK",
        DATA: "DATA",
        FIN: "FIN",
        FINACK: "FINACK"
    }
    return type_map.get(type_val, f"UNKNOWN({type_val})")
