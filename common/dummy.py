"""
Dummy data generator for traffic test tool.
Server uses this to generate data dynamically without real files.
"""

from __future__ import annotations

import os

# Pre-generate a chunk of random bytes once at import time.
# Reused to avoid repeated os.urandom() calls in hot paths.
_POOL_SIZE = 1 * 1024 * 1024  # 1 MiB pool
_POOL = os.urandom(_POOL_SIZE)


def get_chunk(size: int) -> bytes:
    """
    Return `size` bytes of dummy data.
    Uses a pre-generated pool (wraps around) for efficiency.
    """
    if size <= 0:
        return b""
    if size <= _POOL_SIZE:
        return _POOL[:size]
    # For sizes larger than pool, repeat pool
    repeats, remainder = divmod(size, _POOL_SIZE)
    return _POOL * repeats + _POOL[:remainder]


class DummyReader:
    """
    Infinite dummy data reader.
    Yields chunks of `block_size` bytes on each call to read().
    Tracks total bytes generated.
    """

    def __init__(self, block_size: int = 65536) -> None:
        self.block_size = block_size
        self.total_generated: int = 0

    def read(self) -> bytes:
        """Return one block of dummy data."""
        chunk = get_chunk(self.block_size)
        self.total_generated += len(chunk)
        return chunk
