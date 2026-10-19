"""UUIDv7 primary keys: time-ordered, so inserts stay index-friendly."""

import os
import time
import uuid

try:
    from uuid import uuid7 as _uuid7
except ImportError:  # Python < 3.14

    def _uuid7() -> uuid.UUID:
        millis = time.time_ns() // 1_000_000
        value = (millis << 80) | int.from_bytes(os.urandom(10), "big")
        value &= ~(0xF << 76)
        value |= 0x7 << 76  # version
        value &= ~(0x3 << 62)
        value |= 0x2 << 62  # variant
        return uuid.UUID(int=value)


def new_id() -> uuid.UUID:
    return _uuid7()
