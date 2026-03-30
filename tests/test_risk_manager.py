"""Tests for Risk Manager."""

import time
import pytest
from exchange.risk_manager import RiskManager, RiskLimits, ClientPosition
from exchange.order_book import Order, OrderType, OrderStatus, Side, Execution


def make_order(side=Side.BUY, price=100.0, qty=10.0, client="alice", order_type=OrderType.LIMIT):
    return Order(
        order_id=f"O-{time.time_ns()}",
        symbol="BTC-USD",
        side=side,
        order_type=order_type,
        quantity=qty,
        price=price,
        timestamp_ns=time.time_ns(),
        client_id=client,
    )


def make_execution(price=100.0, qty=10.0, buyer="alice", seller="bob"):
    return Execution(
        execution_id=f"E-{time.time_ns()}",
        symbol="BTC-USD",
        price=price,
        quantity=qty,
        buyer_order_id="B1",
        seller_order_id="S1",
        buyer_client_id=buyer,
        seller_client_id=seller,
        timestamp_ns=time.time_ns(),
        aggressor_side=Side.BUY,
    )


class TestRiskManager:
    def setup_method(self):
        self.rm = RiskManager(limits=RiskLimits(
            max_order_quantity=100.0,
            max_order_value=50_000.0,
            price_band_pct=0.10,
            max_position_per_symbol=500.0,
        ))

    def test_allow_normal_order(self):
        ok, reason = self.rm.pre_trade_check(make_order(qty=50.0))
        assert ok is True

    def test_reject_oversized(self):
        ok, reason = self.rm.pre_trade_check(make_order(qty=200.0))
        assert ok is False
        assert "Quantity" in reason

    def test_reject_overvalued(self):
        ok, reason = self.rm.pre_trade_check(make_order(price=1000.0, qty=100.0))
        assert ok is False
        assert "value" in reason

    def test_price_band(self):
        self.rm._last_prices["BTC-USD"] = 100.0
        ok, _ = self.rm.pre_trade_check(make_order(price=105.0))
        assert ok is True  # 5% within band
        ok, reason = self.rm.pre_trade_check(make_order(price=120.0))
        assert ok is False  # 20% outside band

    def test_position_tracking(self):
        exe = make_execution(price=100.0, qty=10.0)
        self.rm.post_trade_update(exe)
        pos = self.rm.get_client_positions("alice")
        assert "BTC-USD" in pos
        assert pos["BTC-USD"].quantity == 10.0

    def test_position_limit(self):
        # Build up position
        for _ in range(49):
            self.rm.post_trade_update(make_execution(qty=10.0))

        # This should fail position check
        ok, reason = self.rm.pre_trade_check(make_order(qty=100.0, client="alice"))
        assert ok is False
        assert "Position limit" in reason

    def test_risk_summary(self):
        self.rm.post_trade_update(make_execution())
        summary = self.rm.get_risk_summary()
        assert summary["total_clients"] >= 1
        assert "BTC-USD" in summary["tracked_symbols"]
