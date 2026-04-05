"""
UDP フレーム定義・pack/unpack・定数。

フレームフォーマット:
  | type (1B) | session_id (4B) | seq_no (4B) | payload (0〜blocksize B) |
  合計ヘッダサイズ: 9 bytes
  エンコード: big-endian (struct format: !B4sI)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# ---- フレームタイプ定数 ----
SYN    = 0x01
SYNACK = 0x02
DATA   = 0x03
FIN    = 0x04
FINACK = 0x05

# ---- SYN payload（モード指定） ----
MODE_DOWNLOAD = 0x01
MODE_UPLOAD   = 0x02
MODE_BOTH     = 0x03

MODE_MAP = {
    "download": MODE_DOWNLOAD,
    "upload":   MODE_UPLOAD,
    "both":     MODE_BOTH,
}
MODE_NAME = {v: k for k, v in MODE_MAP.items()}

# ---- ヘッダ ----
HEADER_SIZE = 9
_STRUCT = struct.Struct("!B4sI")


@dataclass
class Frame:
    type: int        # SYN / SYNACK / DATA / FIN / FINACK
    session_id: bytes  # 4バイト
    seq_no: int      # 32bit unsigned
    payload: bytes = b""


def pack(frame: Frame) -> bytes:
    """Frame → bytes"""
    header = _STRUCT.pack(frame.type, frame.session_id, frame.seq_no)
    return header + frame.payload


def unpack(data: bytes) -> Frame | None:
    """bytes → Frame。不正データは None を返す。"""
    if len(data) < HEADER_SIZE:
        return None
    try:
        ftype, session_id, seq_no = _STRUCT.unpack(data[:HEADER_SIZE])
    except struct.error:
        return None
    return Frame(type=ftype, session_id=session_id, seq_no=seq_no, payload=data[HEADER_SIZE:])
