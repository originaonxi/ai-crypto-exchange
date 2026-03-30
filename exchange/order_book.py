"""
Order Book with FIFO price-time priority matching.

Architecture inspired by NASDAQ and the 2010 Flash Crash learnings:
- SortedDict for price levels (Red-Black tree equivalent — O(log n) sorted access)
- FIFO deques within each price level for time priority
- O(1) order lookup via HashMap for fast cancellation
- L2 (aggregated depth) and L3 (individual order) market data
- Book imbalance detection for circuit breaker triggers
- Memory-efficient order pooling pattern
- Nanosecond-precision timestamps for regulatory compliance

References:
- "Limit Order Book Dynamics and Asset Pricing" — SSRN
- "The Design of a Financial Exchange" — ACM Queue
- "Findings Regarding the Market Events of May 6, 2010" — SEC
- LMAX Disruptor Architecture — Martin Fowler
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from uuid import uuid4

from sortedcontainers import SortedDict


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    IOC = "IOC"  # Immediate-or-Cancel
    FOK = "FOK"  # Fill-or-Kill


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    """
    64-byte aligned order structure.

    In production C++ implementations, this struct would be cache-line
    aligned (64 bytes) for maximum memory locality. Each field is chosen
    to minimize padding.
    """
    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float
    price: float  # 0 for market orders
    timestamp_ns: int
    client_id: str
    remaining_quantity: float = 0.0
    status: OrderStatus = OrderStatus.NEW
    fill_count: int = 0  # number of partial fills

    def __post_init__(self):
        if self.remaining_quantity == 0.0:
            self.remaining_quantity = self.quantity


@dataclass
class Execution:
    """Trade execution report (FIX ExecType=F)."""
    execution_id: str
    symbol: str
    price: float
    quantity: float
    buyer_order_id: str
    seller_order_id: str
    buyer_client_id: str
    seller_client_id: str
    timestamp_ns: int
    aggressor_side: Side


@dataclass
class OrderBookLevel:
    """Single price level — L2 market data."""
    price: float
    total_quantity: float
    order_count: int


@dataclass
class L3OrderInfo:
    """Individual order at a price level — L3 market data."""
    order_id: str
    quantity: float
    timestamp_ns: int
    client_id: str


@dataclass
class L3Level:
    """L3 market data: individual orders visible at each price."""
    price: float
    orders: list[L3OrderInfo]


@dataclass
class OrderBookSnapshot:
    """L2 order book snapshot."""
    symbol: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp_ns: int
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    mid_price: Optional[float] = None
    imbalance_ratio: Optional[float] = None  # >1 = buy pressure, <1 = sell pressure
    total_bid_quantity: float = 0.0
    total_ask_quantity: float = 0.0


@dataclass
class L3Snapshot:
    """L3 order book snapshot — individual orders visible."""
    symbol: str
    bids: list[L3Level]
    asks: list[L3Level]
    timestamp_ns: int


class OrderPool:
    """
    Pre-allocated order object pool to avoid GC pressure.

    Production exchanges use custom memory allocators that can
    allocate/deallocate order objects in constant time using
    free-list management. This eliminates GC pauses that could
    cause missing thousands of orders during peak trading.

    Even a 100μs GC pause at 1M orders/sec = 100 missed orders.
    """

    def __init__(self, initial_size: int = 10000):
        self._pool: deque[dict] = deque()
        self._allocated: int = 0
        self._reused: int = 0
        # Pre-warm the pool
        for _ in range(initial_size):
            self._pool.append({})

    def acquire(self) -> dict:
        if self._pool:
            self._reused += 1
            obj = self._pool.popleft()
            obj.clear()
            return obj
        self._allocated += 1
        return {}

    def release(self, obj: dict):
        self._pool.append(obj)

    @property
    def stats(self) -> dict:
        return {
            "pool_size": len(self._pool),
            "total_allocated": self._allocated,
            "total_reused": self._reused,
            "hit_rate": self._reused / max(1, self._reused + self._allocated),
        }


class OrderBook:
    """
    Single-symbol order book with price-time priority matching.

    Uses SortedDict (Red-Black tree equivalent) for O(log n) sorted
    price level access — critical for fast best-bid/ask lookup and
    price level iteration during matching sweeps.

    The 2010 Flash Crash taught us: order books must detect imbalance
    and support circuit breakers when one side becomes dangerously thin.

    Performance characteristics:
    - Add order: O(log P) where P = number of price levels
    - Match: O(log P) per price level sweep
    - Cancel: O(1) lookup + O(n) queue removal (n = orders at price level)
    - Best bid/ask: O(1) via SortedDict peekitem
    - L2 snapshot: O(depth)
    - L3 snapshot: O(depth * orders_per_level)
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        # SortedDict: Red-Black tree backed sorted dictionary
        # Bids: negative keys for descending sort (highest bid first)
        # Asks: positive keys for ascending sort (lowest ask first)
        self.bids: SortedDict = SortedDict()  # neg_price -> deque[Order]
        self.asks: SortedDict = SortedDict()  # price -> deque[Order]
        self.orders: dict[str, Order] = {}  # O(1) order lookup by ID
        self._order_to_price: dict[str, float] = {}  # order_id -> price for fast cancel

        # Statistics
        self.total_orders: int = 0
        self.total_cancels: int = 0
        self.total_fills: int = 0

    def add_order(self, order: Order) -> list[Execution]:
        """Add an order to the book. Returns list of executions (trades)."""
        if order.symbol != self.symbol:
            raise ValueError(f"Order symbol {order.symbol} != book symbol {self.symbol}")

        self.orders[order.order_id] = order
        self.total_orders += 1

        # FOK: check if full fill is possible before matching
        if order.order_type == OrderType.FOK:
            if not self._can_fill_completely(order):
                order.status = OrderStatus.CANCELLED
                return []

        executions = self._match(order)

        # IOC: cancel any remaining after matching
        if order.order_type == OrderType.IOC and order.remaining_quantity > 0:
            order.status = OrderStatus.CANCELLED
        elif order.remaining_quantity > 0 and order.order_type in (OrderType.LIMIT, OrderType.FOK):
            self._insert_into_book(order)
        elif order.remaining_quantity > 0 and order.order_type == OrderType.MARKET:
            order.status = OrderStatus.CANCELLED

        return executions

    def cancel_order(self, order_id: str) -> Optional[Order]:
        """Cancel an order. O(1) lookup + O(n) removal from price level queue."""
        order = self.orders.get(order_id)
        if not order or order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            return None

        price = self._order_to_price.get(order_id)
        if price is not None:
            if order.side == Side.BUY:
                neg_price = -price
                queue = self.bids.get(neg_price)
                if queue:
                    try:
                        queue.remove(order)
                        if not queue:
                            del self.bids[neg_price]
                    except ValueError:
                        pass
            else:
                queue = self.asks.get(price)
                if queue:
                    try:
                        queue.remove(order)
                        if not queue:
                            del self.asks[price]
                    except ValueError:
                        pass
            del self._order_to_price[order_id]

        order.status = OrderStatus.CANCELLED
        self.total_cancels += 1
        return order

    def get_snapshot(self, depth: int = 20) -> OrderBookSnapshot:
        """L2 order book snapshot — aggregated quantity per price level."""
        now = time.time_ns()
        bid_levels = []
        ask_levels = []
        total_bid_qty = 0.0
        total_ask_qty = 0.0

        # Bids: SortedDict with negative keys, iterate from smallest (highest price)
        for i, (neg_price, queue) in enumerate(self.bids.items()):
            if i >= depth:
                break
            price = -neg_price
            total_qty = sum(o.remaining_quantity for o in queue)
            bid_levels.append(OrderBookLevel(price, total_qty, len(queue)))
            total_bid_qty += total_qty

        # Asks: SortedDict with positive keys, iterate from smallest (lowest price)
        for i, (price, queue) in enumerate(self.asks.items()):
            if i >= depth:
                break
            total_qty = sum(o.remaining_quantity for o in queue)
            ask_levels.append(OrderBookLevel(price, total_qty, len(queue)))
            total_ask_qty += total_qty

        best_bid = bid_levels[0].price if bid_levels else None
        best_ask = ask_levels[0].price if ask_levels else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        mid_price = (best_ask + best_bid) / 2 if spread is not None else None

        # Book imbalance: ratio of bid quantity to ask quantity
        # >1.0 means more buy pressure, <1.0 means more sell pressure
        # The 2010 Flash Crash showed imbalance ratio dropping to near 0 on the ask side
        imbalance = (total_bid_qty / total_ask_qty) if total_ask_qty > 0 else float('inf') if total_bid_qty > 0 else None

        return OrderBookSnapshot(
            symbol=self.symbol,
            bids=bid_levels,
            asks=ask_levels,
            timestamp_ns=now,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            mid_price=mid_price,
            imbalance_ratio=imbalance,
            total_bid_quantity=total_bid_qty,
            total_ask_quantity=total_ask_qty,
        )

    def get_l3_snapshot(self, depth: int = 10) -> L3Snapshot:
        """L3 order book snapshot — individual orders visible at each level."""
        now = time.time_ns()
        bid_levels = []
        ask_levels = []

        for i, (neg_price, queue) in enumerate(self.bids.items()):
            if i >= depth:
                break
            orders = [
                L3OrderInfo(
                    order_id=o.order_id,
                    quantity=o.remaining_quantity,
                    timestamp_ns=o.timestamp_ns,
                    client_id=o.client_id,
                )
                for o in queue
            ]
            bid_levels.append(L3Level(price=-neg_price, orders=orders))

        for i, (price, queue) in enumerate(self.asks.items()):
            if i >= depth:
                break
            orders = [
                L3OrderInfo(
                    order_id=o.order_id,
                    quantity=o.remaining_quantity,
                    timestamp_ns=o.timestamp_ns,
                    client_id=o.client_id,
                )
                for o in queue
            ]
            ask_levels.append(L3Level(price=price, orders=orders))

        return L3Snapshot(symbol=self.symbol, bids=bid_levels, asks=ask_levels, timestamp_ns=now)

    def get_book_imbalance(self, levels: int = 5) -> float:
        """
        Calculate book imbalance across top N levels.

        Returns ratio: bid_qty / (bid_qty + ask_qty)
        - 0.5 = balanced
        - >0.7 = heavy buy pressure
        - <0.3 = heavy sell pressure (Flash Crash territory)

        During the 2010 Flash Crash, this ratio dropped below 0.1
        as market makers withdrew their ask quotes.
        """
        bid_qty = 0.0
        ask_qty = 0.0

        for i, (_, queue) in enumerate(self.bids.items()):
            if i >= levels:
                break
            bid_qty += sum(o.remaining_quantity for o in queue)

        for i, (_, queue) in enumerate(self.asks.items()):
            if i >= levels:
                break
            ask_qty += sum(o.remaining_quantity for o in queue)

        total = bid_qty + ask_qty
        if total == 0:
            return 0.5
        return bid_qty / total

    def get_depth_at_price(self, price: float, side: Side) -> float:
        """Get total quantity at a specific price level."""
        if side == Side.BUY:
            queue = self.bids.get(-price)
        else:
            queue = self.asks.get(price)
        if not queue:
            return 0.0
        return sum(o.remaining_quantity for o in queue)

    @property
    def best_bid(self) -> Optional[float]:
        if not self.bids:
            return None
        return -self.bids.peekitem(0)[0]

    @property
    def best_ask(self) -> Optional[float]:
        if not self.asks:
            return None
        return self.asks.peekitem(0)[0]

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is not None and ba is not None:
            return ba - bb
        return None

    @property
    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return None

    @property
    def bid_depth(self) -> int:
        """Number of price levels on bid side."""
        return len(self.bids)

    @property
    def ask_depth(self) -> int:
        """Number of price levels on ask side."""
        return len(self.asks)

    def _can_fill_completely(self, order: Order) -> bool:
        """Check if FOK order can be completely filled."""
        remaining = order.remaining_quantity
        if order.side == Side.BUY:
            for price, queue in self.asks.items():
                if order.order_type == OrderType.FOK and order.price > 0 and price > order.price:
                    break
                for o in queue:
                    remaining -= o.remaining_quantity
                    if remaining <= 0:
                        return True
        else:
            for neg_price, queue in self.bids.items():
                bid_price = -neg_price
                if order.order_type == OrderType.FOK and order.price > 0 and bid_price < order.price:
                    break
                for o in queue:
                    remaining -= o.remaining_quantity
                    if remaining <= 0:
                        return True
        return remaining <= 0

    def _match(self, order: Order) -> list[Execution]:
        if order.side == Side.BUY:
            return self._match_buy(order)
        return self._match_sell(order)

    def _match_buy(self, order: Order) -> list[Execution]:
        executions = []
        prices_to_remove = []

        for ask_price, ask_queue in self.asks.items():
            if order.remaining_quantity <= 0:
                break
            if order.order_type in (OrderType.LIMIT, OrderType.FOK) and ask_price > order.price:
                break

            while order.remaining_quantity > 0 and ask_queue:
                counterparty = ask_queue[0]
                fill_qty = min(order.remaining_quantity, counterparty.remaining_quantity)
                fill_price = ask_price  # price improvement: taker gets maker's price

                now = time.time_ns()
                execution = Execution(
                    execution_id=uuid4().hex[:16],
                    symbol=self.symbol,
                    price=fill_price,
                    quantity=fill_qty,
                    buyer_order_id=order.order_id,
                    seller_order_id=counterparty.order_id,
                    buyer_client_id=order.client_id,
                    seller_client_id=counterparty.client_id,
                    timestamp_ns=now,
                    aggressor_side=Side.BUY,
                )
                executions.append(execution)
                self.total_fills += 1

                order.remaining_quantity -= fill_qty
                order.fill_count += 1
                counterparty.remaining_quantity -= fill_qty
                counterparty.fill_count += 1

                if counterparty.remaining_quantity <= 0:
                    counterparty.status = OrderStatus.FILLED
                    ask_queue.popleft()
                    self._order_to_price.pop(counterparty.order_id, None)
                else:
                    counterparty.status = OrderStatus.PARTIALLY_FILLED

            if not ask_queue:
                prices_to_remove.append(ask_price)

        for p in prices_to_remove:
            del self.asks[p]

        if order.remaining_quantity <= 0:
            order.status = OrderStatus.FILLED
        elif executions:
            order.status = OrderStatus.PARTIALLY_FILLED

        return executions

    def _match_sell(self, order: Order) -> list[Execution]:
        executions = []
        prices_to_remove = []

        for neg_bid_price, bid_queue in self.bids.items():
            if order.remaining_quantity <= 0:
                break
            bid_price = -neg_bid_price
            if order.order_type in (OrderType.LIMIT, OrderType.FOK) and bid_price < order.price:
                break

            while order.remaining_quantity > 0 and bid_queue:
                counterparty = bid_queue[0]
                fill_qty = min(order.remaining_quantity, counterparty.remaining_quantity)
                fill_price = bid_price

                now = time.time_ns()
                execution = Execution(
                    execution_id=uuid4().hex[:16],
                    symbol=self.symbol,
                    price=fill_price,
                    quantity=fill_qty,
                    buyer_order_id=counterparty.order_id,
                    seller_order_id=order.order_id,
                    buyer_client_id=counterparty.client_id,
                    seller_client_id=order.client_id,
                    timestamp_ns=now,
                    aggressor_side=Side.SELL,
                )
                executions.append(execution)
                self.total_fills += 1

                order.remaining_quantity -= fill_qty
                order.fill_count += 1
                counterparty.remaining_quantity -= fill_qty
                counterparty.fill_count += 1

                if counterparty.remaining_quantity <= 0:
                    counterparty.status = OrderStatus.FILLED
                    bid_queue.popleft()
                    self._order_to_price.pop(counterparty.order_id, None)
                else:
                    counterparty.status = OrderStatus.PARTIALLY_FILLED

            if not bid_queue:
                prices_to_remove.append(neg_bid_price)

        for p in prices_to_remove:
            del self.bids[p]

        if order.remaining_quantity <= 0:
            order.status = OrderStatus.FILLED
        elif executions:
            order.status = OrderStatus.PARTIALLY_FILLED

        return executions

    def _insert_into_book(self, order: Order):
        if order.side == Side.BUY:
            neg_price = -order.price
            if neg_price not in self.bids:
                self.bids[neg_price] = deque()
            self.bids[neg_price].append(order)
        else:
            if order.price not in self.asks:
                self.asks[order.price] = deque()
            self.asks[order.price].append(order)
        self._order_to_price[order.order_id] = order.price
