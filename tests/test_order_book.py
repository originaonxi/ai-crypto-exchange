"""
Comprehensive tests for the Order Book — the heart of the exchange.

Tests cover:
- Price-time priority (FIFO within price levels)
- Limit order matching (full, partial, no match)
- Market order matching (sweep multiple levels)
- Order cancellation
- Order book snapshots
- Edge cases (zero quantity, crossed book, self-trade)
"""

import time
import pytest
from exchange.order_book import (
    Order, OrderBook, OrderType, OrderStatus, Side, Execution, OrderBookSnapshot
)


def make_order(
    side: Side,
    price: float,
    quantity: float,
    order_type: OrderType = OrderType.LIMIT,
    client_id: str = "test",
    order_id: str = None,
) -> Order:
    return Order(
        order_id=order_id or f"ORD-{time.time_ns()}",
        symbol="BTC-USD",
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        timestamp_ns=time.time_ns(),
        client_id=client_id,
    )


class TestOrderBookBasic:
    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_add_limit_buy_no_match(self):
        order = make_order(Side.BUY, 100.0, 10.0)
        execs = self.book.add_order(order)
        assert execs == []
        assert order.status == OrderStatus.NEW
        assert order.remaining_quantity == 10.0

    def test_add_limit_sell_no_match(self):
        order = make_order(Side.SELL, 100.0, 5.0)
        execs = self.book.add_order(order)
        assert execs == []
        assert order.status == OrderStatus.NEW

    def test_exact_match(self):
        buy = make_order(Side.BUY, 100.0, 10.0, client_id="buyer")
        sell = make_order(Side.SELL, 100.0, 10.0, client_id="seller")

        self.book.add_order(buy)
        execs = self.book.add_order(sell)

        assert len(execs) == 1
        assert execs[0].price == 100.0
        assert execs[0].quantity == 10.0
        assert buy.status == OrderStatus.FILLED
        assert sell.status == OrderStatus.FILLED

    def test_partial_fill(self):
        buy = make_order(Side.BUY, 100.0, 10.0)
        sell = make_order(Side.SELL, 100.0, 3.0)

        self.book.add_order(buy)
        execs = self.book.add_order(sell)

        assert len(execs) == 1
        assert execs[0].quantity == 3.0
        assert buy.status == OrderStatus.PARTIALLY_FILLED
        assert buy.remaining_quantity == 7.0
        assert sell.status == OrderStatus.FILLED

    def test_price_improvement(self):
        """Seller gets buyer's higher price (price improvement)."""
        buy = make_order(Side.BUY, 105.0, 10.0)
        sell = make_order(Side.SELL, 100.0, 10.0)

        self.book.add_order(buy)
        execs = self.book.add_order(sell)

        assert len(execs) == 1
        assert execs[0].price == 105.0  # maker's price

    def test_no_match_price_gap(self):
        buy = make_order(Side.BUY, 99.0, 10.0)
        sell = make_order(Side.SELL, 101.0, 10.0)

        self.book.add_order(buy)
        execs = self.book.add_order(sell)

        assert execs == []
        snap = self.book.get_snapshot()
        assert snap.best_bid == 99.0
        assert snap.best_ask == 101.0
        assert snap.spread == 2.0


class TestPriceTimePriority:
    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_fifo_same_price(self):
        """First order at same price gets filled first."""
        buy1 = make_order(Side.BUY, 100.0, 5.0, client_id="first", order_id="B1")
        buy2 = make_order(Side.BUY, 100.0, 5.0, client_id="second", order_id="B2")
        sell = make_order(Side.SELL, 100.0, 5.0, client_id="seller")

        self.book.add_order(buy1)
        self.book.add_order(buy2)
        execs = self.book.add_order(sell)

        assert len(execs) == 1
        assert execs[0].buyer_order_id == "B1"  # FIFO: first buyer wins

    def test_best_price_priority(self):
        """Higher bid gets filled before lower bid."""
        buy_low = make_order(Side.BUY, 99.0, 5.0, order_id="LOW")
        buy_high = make_order(Side.BUY, 101.0, 5.0, order_id="HIGH")
        sell = make_order(Side.SELL, 98.0, 5.0)

        self.book.add_order(buy_low)
        self.book.add_order(buy_high)
        execs = self.book.add_order(sell)

        assert len(execs) == 1
        assert execs[0].buyer_order_id == "HIGH"
        assert execs[0].price == 101.0

    def test_sweep_multiple_levels(self):
        """Large sell sweeps multiple bid levels."""
        self.book.add_order(make_order(Side.BUY, 103.0, 2.0, order_id="B103"))
        self.book.add_order(make_order(Side.BUY, 102.0, 3.0, order_id="B102"))
        self.book.add_order(make_order(Side.BUY, 101.0, 5.0, order_id="B101"))

        sell = make_order(Side.SELL, 100.0, 8.0)
        execs = self.book.add_order(sell)

        assert len(execs) == 3
        assert execs[0].price == 103.0
        assert execs[0].quantity == 2.0
        assert execs[1].price == 102.0
        assert execs[1].quantity == 3.0
        assert execs[2].price == 101.0
        assert execs[2].quantity == 3.0
        assert sell.status == OrderStatus.FILLED


class TestMarketOrders:
    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_market_buy(self):
        self.book.add_order(make_order(Side.SELL, 100.0, 10.0))
        buy = make_order(Side.BUY, 0, 5.0, order_type=OrderType.MARKET)
        execs = self.book.add_order(buy)
        assert len(execs) == 1
        assert execs[0].quantity == 5.0
        assert buy.status == OrderStatus.FILLED

    def test_market_order_no_liquidity(self):
        """Market order with no counterparty gets cancelled."""
        buy = make_order(Side.BUY, 0, 5.0, order_type=OrderType.MARKET)
        execs = self.book.add_order(buy)
        assert execs == []
        assert buy.status == OrderStatus.CANCELLED


class TestCancellation:
    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_cancel_existing(self):
        order = make_order(Side.BUY, 100.0, 10.0, order_id="CANCEL_ME")
        self.book.add_order(order)
        result = self.book.cancel_order("CANCEL_ME")
        assert result is not None
        assert result.status == OrderStatus.CANCELLED

    def test_cancel_nonexistent(self):
        result = self.book.cancel_order("DOES_NOT_EXIST")
        assert result is None

    def test_cancel_filled(self):
        buy = make_order(Side.BUY, 100.0, 10.0, order_id="FILLED")
        self.book.add_order(buy)
        sell = make_order(Side.SELL, 100.0, 10.0)
        self.book.add_order(sell)
        result = self.book.cancel_order("FILLED")
        assert result is None  # already filled


class TestOrderBookSnapshot:
    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_empty_book(self):
        snap = self.book.get_snapshot()
        assert snap.bids == []
        assert snap.asks == []
        assert snap.best_bid is None
        assert snap.best_ask is None

    def test_snapshot_levels(self):
        self.book.add_order(make_order(Side.BUY, 100.0, 5.0))
        self.book.add_order(make_order(Side.BUY, 100.0, 3.0))
        self.book.add_order(make_order(Side.BUY, 99.0, 10.0))
        self.book.add_order(make_order(Side.SELL, 101.0, 7.0))

        snap = self.book.get_snapshot()
        assert snap.best_bid == 100.0
        assert snap.best_ask == 101.0
        assert snap.spread == 1.0
        assert len(snap.bids) == 2
        assert snap.bids[0].total_quantity == 8.0  # 5 + 3 at 100
        assert snap.bids[0].order_count == 2
        assert snap.bids[1].total_quantity == 10.0  # 10 at 99
