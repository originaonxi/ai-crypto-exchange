"""Tests for Write-Ahead Log."""

import os
import tempfile
import pytest

from exchange.wal import WAL, WALEntry, WALEventType


class TestWAL:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.wal")

    def test_append_and_replay(self):
        wal = WAL(self.path)
        with wal:
            wal.append(WALEntry(
                event_type=WALEventType.ORDER_NEW,
                order_id="O1",
                symbol="BTC-USD",
                side="BUY",
                price=100.0,
                quantity=10.0,
                client_id="alice",
                timestamp_ns=123456789,
            ))
            wal.append(WALEntry(
                event_type=WALEventType.ORDER_NEW,
                order_id="O2",
                symbol="ETH-USD",
                side="SELL",
                price=3.0,
                quantity=50.0,
                client_id="bob",
                timestamp_ns=123456790,
            ))

        wal2 = WAL(self.path)
        entries = wal2.replay()
        assert len(entries) == 2
        assert entries[0].order_id == "O1"
        assert entries[0].price == 100.0
        assert entries[1].order_id == "O2"
        assert entries[1].symbol == "ETH-USD"

    def test_sequence_numbers(self):
        wal = WAL(self.path)
        with wal:
            seq1 = wal.append(WALEntry(
                event_type=WALEventType.ORDER_NEW,
                order_id="O1", symbol="X", side="BUY",
                price=1, quantity=1, client_id="a", timestamp_ns=0,
            ))
            seq2 = wal.append(WALEntry(
                event_type=WALEventType.ORDER_NEW,
                order_id="O2", symbol="X", side="BUY",
                price=1, quantity=1, client_id="a", timestamp_ns=0,
            ))
        assert seq2 == seq1 + 1

    def test_truncate(self):
        wal = WAL(self.path)
        with wal:
            wal.append(WALEntry(
                event_type=WALEventType.ORDER_NEW,
                order_id="O1", symbol="X", side="BUY",
                price=1, quantity=1, client_id="a", timestamp_ns=0,
            ))

        wal2 = WAL(self.path)
        wal2.truncate()
        entries = wal2.replay()
        assert len(entries) == 0
        wal2.close()

    def test_empty_wal_replay(self):
        wal = WAL(os.path.join(self.tmpdir, "empty.wal"))
        entries = wal.replay()
        assert entries == []

    def test_size_bytes(self):
        wal = WAL(self.path)
        with wal:
            wal.append(WALEntry(
                event_type=WALEventType.ORDER_NEW,
                order_id="O1", symbol="X", side="BUY",
                price=1, quantity=1, client_id="a", timestamp_ns=0,
            ))
        assert wal.size_bytes > 0
