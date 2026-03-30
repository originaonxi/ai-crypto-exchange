"""
Comprehensive tests for the Order Book — the heart of the exchange.

Tests cover:
- Price-time priority (FIFO within price levels)
- Limit order matching (full, partial, no match)
- Market order matching (sweep multiple levels)
- IOC (Immediate-or-Cancel) and FOK (Fill-or-Kill) orders
- Order cancellation
- Order book snapshots (L2 and L3)
- Book imbalance detection (Flash Crash indicator)
- SortedDict (Red-Black tree) price level management
"""

import time
import pytest
from exchange.order_book import (
    Order, OrderBook, OrderPool, OrderType, OrderStatus, Side, Execution,
    OrderBookSnapshot, L3Snapshot,
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
        assert snap.mid_price == 100.5
        assert snap.imbalance_ratio is not None
        assert snap.total_bid_quantity == 18.0
        assert snap.total_ask_quantity == 7.0
        assert len(snap.bids) == 2
        assert snap.bids[0].total_quantity == 8.0  # 5 + 3 at 100
        assert snap.bids[0].order_count == 2
        assert snap.bids[1].total_quantity == 10.0  # 10 at 99


class TestIOCOrders:
    """Immediate-or-Cancel: fill what you can, cancel the rest."""

    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_ioc_full_fill(self):
        self.book.add_order(make_order(Side.SELL, 100.0, 10.0))
        ioc = make_order(Side.BUY, 100.0, 5.0, order_type=OrderType.IOC)
        execs = self.book.add_order(ioc)
        assert len(execs) == 1
        assert ioc.status == OrderStatus.FILLED

    def test_ioc_partial_cancelled(self):
        self.book.add_order(make_order(Side.SELL, 100.0, 3.0))
        ioc = make_order(Side.BUY, 100.0, 10.0, order_type=OrderType.IOC)
        execs = self.book.add_order(ioc)
        assert len(execs) == 1
        assert execs[0].quantity == 3.0
        assert ioc.status == OrderStatus.CANCELLED  # unfilled portion cancelled

    def test_ioc_no_match_cancelled(self):
        ioc = make_order(Side.BUY, 100.0, 5.0, order_type=OrderType.IOC)
        execs = self.book.add_order(ioc)
        assert execs == []
        assert ioc.status == OrderStatus.CANCELLED


class TestFOKOrders:
    """Fill-or-Kill: fill completely or reject entirely."""

    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_fok_full_fill(self):
        self.book.add_order(make_order(Side.SELL, 100.0, 10.0))
        fok = make_order(Side.BUY, 100.0, 5.0, order_type=OrderType.FOK)
        execs = self.book.add_order(fok)
        assert len(execs) == 1
        assert fok.status == OrderStatus.FILLED

    def test_fok_insufficient_liquidity_rejected(self):
        self.book.add_order(make_order(Side.SELL, 100.0, 3.0))
        fok = make_order(Side.BUY, 100.0, 10.0, order_type=OrderType.FOK)
        execs = self.book.add_order(fok)
        assert execs == []
        assert fok.status == OrderStatus.CANCELLED

    def test_fok_no_match_rejected(self):
        fok = make_order(Side.BUY, 100.0, 5.0, order_type=OrderType.FOK)
        execs = self.book.add_order(fok)
        assert execs == []
        assert fok.status == OrderStatus.CANCELLED


class TestL3MarketData:
    """L3 market data: individual orders visible at each price level."""

    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_l3_shows_individual_orders(self):
        self.book.add_order(make_order(Side.BUY, 100.0, 5.0, order_id="B1", client_id="alice"))
        self.book.add_order(make_order(Side.BUY, 100.0, 3.0, order_id="B2", client_id="bob"))
        self.book.add_order(make_order(Side.SELL, 101.0, 7.0, order_id="S1", client_id="carol"))

        l3 = self.book.get_l3_snapshot()
        assert len(l3.bids) == 1  # one price level
        assert len(l3.bids[0].orders) == 2  # two orders at that level
        assert l3.bids[0].orders[0].order_id == "B1"
        assert l3.bids[0].orders[1].order_id == "B2"
        assert len(l3.asks) == 1
        assert l3.asks[0].orders[0].order_id == "S1"


class TestBookImbalance:
    """Book imbalance detection — key Flash Crash indicator."""

    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_balanced_book(self):
        self.book.add_order(make_order(Side.BUY, 100.0, 10.0))
        self.book.add_order(make_order(Side.SELL, 101.0, 10.0))
        imbalance = self.book.get_book_imbalance()
        assert 0.45 <= imbalance <= 0.55  # approximately balanced

    def test_heavy_buy_pressure(self):
        self.book.add_order(make_order(Side.BUY, 100.0, 100.0))
        self.book.add_order(make_order(Side.SELL, 101.0, 1.0))
        imbalance = self.book.get_book_imbalance()
        assert imbalance > 0.9  # heavily buy-side

    def test_heavy_sell_pressure(self):
        """Flash Crash scenario: ask side dominates."""
        self.book.add_order(make_order(Side.BUY, 100.0, 1.0))
        self.book.add_order(make_order(Side.SELL, 101.0, 100.0))
        imbalance = self.book.get_book_imbalance()
        assert imbalance < 0.1  # dangerously thin bid side

    def test_empty_book_balanced(self):
        imbalance = self.book.get_book_imbalance()
        assert imbalance == 0.5


class TestSortedDictProperties:
    """Verify Red-Black tree (SortedDict) properties."""

    def setup_method(self):
        self.book = OrderBook("BTC-USD")

    def test_best_bid_ask_properties(self):
        self.book.add_order(make_order(Side.BUY, 99.0, 5.0))
        self.book.add_order(make_order(Side.BUY, 100.0, 5.0))
        self.book.add_order(make_order(Side.SELL, 101.0, 5.0))
        self.book.add_order(make_order(Side.SELL, 102.0, 5.0))

        assert self.book.best_bid == 100.0
        assert self.book.best_ask == 101.0
        assert self.book.spread == 1.0
        assert self.book.mid_price == 100.5
        assert self.book.bid_depth == 2
        assert self.book.ask_depth == 2

    def test_depth_at_price(self):
        self.book.add_order(make_order(Side.BUY, 100.0, 5.0))
        self.book.add_order(make_order(Side.BUY, 100.0, 3.0))
        assert self.book.get_depth_at_price(100.0, Side.BUY) == 8.0
        assert self.book.get_depth_at_price(99.0, Side.BUY) == 0.0


class TestOrderPool:
    def test_pool_reuse(self):
        pool = OrderPool(initial_size=5)
        obj1 = pool.acquire()
        pool.release(obj1)
        obj2 = pool.acquire()
        assert pool.stats["total_reused"] >= 1

    def test_pool_expansion(self):
        pool = OrderPool(initial_size=0)
        obj = pool.acquire()
        assert pool.stats["total_allocated"] == 1
