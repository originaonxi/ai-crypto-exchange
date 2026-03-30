"""
Integration tests for the Matching Engine.

Tests the full pipeline: order submission -> matching -> WAL -> risk -> callbacks.
"""

import os
import tempfile
import time
import pytest

from exchange.matching_engine import MatchingEngine, EngineStats
from exchange.order_book import Order, OrderType, OrderStatus, Side, Execution
from exchange.wal import WAL
from exchange.risk_manager import RiskManager, RiskLimits


class TestMatchingEngine:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.wal = WAL(os.path.join(self.tmpdir, "test.wal"))
        self.wal.open()
        self.engine = MatchingEngine(
            symbols=["BTC-USD", "ETH-USD"],
            wal=self.wal,
        )

    def teardown_method(self):
        self.wal.close()

    def test_submit_and_match(self):
        self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 100.0, "alice")
        sell = self.engine.create_order("BTC-USD", Side.SELL, OrderType.LIMIT, 10.0, 100.0, "bob")
        assert sell.status == OrderStatus.FILLED
        assert self.engine.stats.total_executions == 1

    def test_reject_unknown_symbol(self):
        order = self.engine.create_order("FAKE-USD", Side.BUY, OrderType.LIMIT, 10.0, 100.0)
        assert order.status == OrderStatus.REJECTED

    def test_reject_zero_quantity(self):
        order = self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 0, 100.0)
        assert order.status == OrderStatus.REJECTED

    def test_reject_zero_price_limit(self):
        order = self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 0)
        assert order.status == OrderStatus.REJECTED

    def test_halt_rejects_orders(self):
        self.engine.halt_trading("test halt")
        order = self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 100.0)
        assert order.status == OrderStatus.REJECTED
        self.engine.resume_trading()
        order2 = self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 100.0)
        assert order2.status != OrderStatus.REJECTED

    def test_cancel_order(self):
        order = self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 100.0)
        result = self.engine.cancel_order(order.order_id)
        assert result is not None
        assert result.status == OrderStatus.CANCELLED

    def test_execution_callbacks(self):
        executions = []
        self.engine.on_execution(lambda e: executions.append(e))

        self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 100.0, "alice")
        self.engine.create_order("BTC-USD", Side.SELL, OrderType.LIMIT, 10.0, 100.0, "bob")

        assert len(executions) == 1
        assert executions[0].quantity == 10.0

    def test_stats_tracking(self):
        self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 50.0, "a")
        self.engine.create_order("BTC-USD", Side.SELL, OrderType.LIMIT, 10.0, 50.0, "b")

        assert self.engine.stats.orders_processed == 2
        assert self.engine.stats.orders_matched >= 1
        assert self.engine.stats.total_volume == 500.0  # 10 * 50

    def test_wal_records(self):
        self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 10.0, 100.0, "a")
        self.engine.create_order("BTC-USD", Side.SELL, OrderType.LIMIT, 10.0, 100.0, "b")
        self.wal.close()

        wal2 = WAL(os.path.join(self.tmpdir, "test.wal"))
        entries = wal2.replay()
        assert len(entries) >= 3  # 2 orders + 1 execution

    def test_multiple_symbols(self):
        self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 5.0, 100.0)
        self.engine.create_order("ETH-USD", Side.BUY, OrderType.LIMIT, 20.0, 3.0)

        btc_snap = self.engine.get_book_snapshot("BTC-USD")
        eth_snap = self.engine.get_book_snapshot("ETH-USD")

        assert btc_snap.best_bid == 100.0
        assert eth_snap.best_bid == 3.0


class TestWithRiskManager:
    def setup_method(self):
        self.risk_mgr = RiskManager(
            limits=RiskLimits(max_order_quantity=100.0),
        )

        def risk_check(order, execs):
            ok, reason = self.risk_mgr.pre_trade_check(order)
            return ok

        self.engine = MatchingEngine(
            symbols=["BTC-USD"],
            risk_check=risk_check,
        )

    def test_risk_rejects_large_order(self):
        order = self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 500.0, 100.0)
        assert order.status == OrderStatus.REJECTED

    def test_risk_allows_normal_order(self):
        order = self.engine.create_order("BTC-USD", Side.BUY, OrderType.LIMIT, 50.0, 100.0)
        assert order.status != OrderStatus.REJECTED
