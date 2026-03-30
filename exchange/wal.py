"""
Write-Ahead Log for crash recovery.

Every state change is appended to the WAL before being applied.
On restart, replay the WAL to reconstruct full engine state.
Uses binary encoding for performance (struct packing).
"""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Optional


class WALEventType(str, Enum):
    ORDER_NEW = "ORDER_NEW"
    ORDER_CANCEL = "ORDER_CANCEL"
    EXECUTION = "EXECUTION"
    HALT = "HALT"
    RESUME = "RESUME"
    SNAPSHOT = "SNAPSHOT"


@dataclass
class WALEntry:
    event_type: WALEventType
    order_id: str
    symbol: str
    side: str
    price: float
    quantity: float
    client_id: str
    timestamp_ns: int
    order_type: str = ""
    sequence: int = 0


# Binary format: 4-byte length prefix + JSON payload
# In production, use FlatBuffers/Protobuf for zero-copy deserialization
HEADER_FORMAT = "!I"  # network byte order, unsigned int (4 bytes)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


class WAL:
    """Append-only write-ahead log with binary length-prefixed JSON entries."""

    def __init__(self, path: str = "data/wal.log"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = 0
        self._file = None
        self._entry_count = 0

        # Count existing entries on init
        if self.path.exists():
            self._sequence = self._count_entries()

    def open(self):
        self._file = open(self.path, "ab")

    def close(self):
        if self._file:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._file = None

    def append(self, entry: WALEntry) -> int:
        self._sequence += 1
        entry.sequence = self._sequence

        payload = json.dumps(asdict(entry)).encode("utf-8")
        header = struct.pack(HEADER_FORMAT, len(payload))

        if self._file is None:
            self.open()

        self._file.write(header + payload)
        self._file.flush()

        return self._sequence

    def replay(self) -> list[WALEntry]:
        """Replay all entries from the WAL for state reconstruction."""
        entries = []
        if not self.path.exists():
            return entries

        with open(self.path, "rb") as f:
            while True:
                header_data = f.read(HEADER_SIZE)
                if len(header_data) < HEADER_SIZE:
                    break

                payload_len = struct.unpack(HEADER_FORMAT, header_data)[0]
                payload_data = f.read(payload_len)
                if len(payload_data) < payload_len:
                    break  # truncated entry, skip

                try:
                    data = json.loads(payload_data.decode("utf-8"))
                    entry = WALEntry(
                        event_type=WALEventType(data["event_type"]),
                        order_id=data["order_id"],
                        symbol=data["symbol"],
                        side=data["side"],
                        price=data["price"],
                        quantity=data["quantity"],
                        client_id=data["client_id"],
                        timestamp_ns=data["timestamp_ns"],
                        order_type=data.get("order_type", ""),
                        sequence=data.get("sequence", 0),
                    )
                    entries.append(entry)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    continue  # skip corrupted entries

        return entries

    def truncate(self):
        """Clear the WAL (after checkpoint/snapshot)."""
        if self._file:
            self.close()
        with open(self.path, "wb") as f:
            pass
        self._sequence = 0
        self.open()

    def _count_entries(self) -> int:
        count = 0
        try:
            with open(self.path, "rb") as f:
                while True:
                    header_data = f.read(HEADER_SIZE)
                    if len(header_data) < HEADER_SIZE:
                        break
                    payload_len = struct.unpack(HEADER_FORMAT, header_data)[0]
                    f.seek(payload_len, 1)
                    count += 1
        except Exception:
            pass
        return count

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()
