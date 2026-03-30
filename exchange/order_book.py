"""
Order Book with FIFO price-time priority matching.

Inspired by NASDAQ's architecture:
- Separate FIFO queues per price level
- Bids sorted highest-to-lowest (max heap)
- Asks sorted lowest-to-highest (min heap)
- Single-threaded matching for deterministic latency
- Partial fill support with remaining quantity tracking
"""

from __future__ import annotations

import heapq
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from uuid import uuid4


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Order:
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

    def __post_init__(self):
        if self.remaining_quantity == 0.0:
            self.remaining_quantity = self.quantity


@dataclass
class Execution:
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
    price: float
    total_quantity: float
    order_count: int


@dataclass
class OrderBookSnapshot:
    symbol: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp_ns: int
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None


class OrderBook:
    """Single-symbol order book with price-time priority matching."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: dict[float, deque[Order]] = defaultdict(deque)
        self.asks: dict[float, deque[Order]] = defaultdict(deque)
        self.bid_prices: list[float] = []  # max heap (negated)
        self.ask_prices: list[float] = []  # min heap
        self.orders: dict[str, Order] = {}  # order_id -> Order for O(1) lookup
        self._bid_price_set: set[float] = set()
        self._ask_price_set: set[float] = set()

    def add_order(self, order: Order) -> list[Execution]:
        if order.symbol != self.symbol:
            raise ValueError(f"Order symbol {order.symbol} != book symbol {self.symbol}")

        self.orders[order.order_id] = order
        executions = self._match(order)

        if order.remaining_quantity > 0 and order.order_type == OrderType.LIMIT:
            self._insert_into_book(order)
        elif order.remaining_quantity > 0 and order.order_type == OrderType.MARKET:
            order.status = OrderStatus.CANCELLED  # unfilled market order portion cancelled

        return executions

    def cancel_order(self, order_id: str) -> Optional[Order]:
        order = self.orders.get(order_id)
        if not order or order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            return None

        if order.side == Side.BUY:
            queue = self.bids.get(order.price)
        else:
            queue = self.asks.get(order.price)

        if queue:
            try:
                queue.remove(order)
                if not queue:
                    self._remove_price_level(order.side, order.price)
            except ValueError:
                pass

        order.status = OrderStatus.CANCELLED
        return order

    def get_snapshot(self, depth: int = 20) -> OrderBookSnapshot:
        now = time.time_ns()
        bid_levels = []
        ask_levels = []

        for neg_price in sorted(self.bid_prices)[:depth]:
            price = -neg_price
            if price in self._bid_price_set and self.bids[price]:
                queue = self.bids[price]
                total_qty = sum(o.remaining_quantity for o in queue)
                bid_levels.append(OrderBookLevel(price, total_qty, len(queue)))

        for price in sorted(self.ask_prices)[:depth]:
            if price in self._ask_price_set and self.asks[price]:
                queue = self.asks[price]
                total_qty = sum(o.remaining_quantity for o in queue)
                ask_levels.append(OrderBookLevel(price, total_qty, len(queue)))

        best_bid = bid_levels[0].price if bid_levels else None
        best_ask = ask_levels[0].price if ask_levels else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

        return OrderBookSnapshot(
            symbol=self.symbol,
            bids=bid_levels,
            asks=ask_levels,
            timestamp_ns=now,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
        )

    def _match(self, order: Order) -> list[Execution]:
        executions = []

        if order.side == Side.BUY:
            executions = self._match_buy(order)
        else:
            executions = self._match_sell(order)

        return executions

    def _match_buy(self, order: Order) -> list[Execution]:
        executions = []

        while order.remaining_quantity > 0 and self.ask_prices:
            best_ask = self.ask_prices[0]

            if order.order_type == OrderType.LIMIT and best_ask > order.price:
                break

            ask_queue = self.asks[best_ask]
            if not ask_queue:
                heapq.heappop(self.ask_prices)
                self._ask_price_set.discard(best_ask)
                continue

            counterparty = ask_queue[0]
            fill_qty = min(order.remaining_quantity, counterparty.remaining_quantity)
            fill_price = best_ask  # price improvement: taker gets maker's price

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

            order.remaining_quantity -= fill_qty
            counterparty.remaining_quantity -= fill_qty

            if counterparty.remaining_quantity <= 0:
                counterparty.status = OrderStatus.FILLED
                ask_queue.popleft()
                if not ask_queue:
                    heapq.heappop(self.ask_prices)
                    self._ask_price_set.discard(best_ask)
                    del self.asks[best_ask]
            else:
                counterparty.status = OrderStatus.PARTIALLY_FILLED

        if order.remaining_quantity <= 0:
            order.status = OrderStatus.FILLED
        elif executions:
            order.status = OrderStatus.PARTIALLY_FILLED

        return executions

    def _match_sell(self, order: Order) -> list[Execution]:
        executions = []

        while order.remaining_quantity > 0 and self.bid_prices:
            best_bid_neg = self.bid_prices[0]
            best_bid = -best_bid_neg

            if order.order_type == OrderType.LIMIT and best_bid < order.price:
                break

            bid_queue = self.bids[best_bid]
            if not bid_queue:
                heapq.heappop(self.bid_prices)
                self._bid_price_set.discard(best_bid)
                continue

            counterparty = bid_queue[0]
            fill_qty = min(order.remaining_quantity, counterparty.remaining_quantity)
            fill_price = best_bid

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

            order.remaining_quantity -= fill_qty
            counterparty.remaining_quantity -= fill_qty

            if counterparty.remaining_quantity <= 0:
                counterparty.status = OrderStatus.FILLED
                bid_queue.popleft()
                if not bid_queue:
                    heapq.heappop(self.bid_prices)
                    self._bid_price_set.discard(best_bid)
                    del self.bids[best_bid]
            else:
                counterparty.status = OrderStatus.PARTIALLY_FILLED

        if order.remaining_quantity <= 0:
            order.status = OrderStatus.FILLED
        elif executions:
            order.status = OrderStatus.PARTIALLY_FILLED

        return executions

    def _insert_into_book(self, order: Order):
        if order.side == Side.BUY:
            self.bids[order.price].append(order)
            if order.price not in self._bid_price_set:
                heapq.heappush(self.bid_prices, -order.price)
                self._bid_price_set.add(order.price)
        else:
            self.asks[order.price].append(order)
            if order.price not in self._ask_price_set:
                heapq.heappush(self.ask_prices, order.price)
                self._ask_price_set.add(order.price)

    def _remove_price_level(self, side: Side, price: float):
        if side == Side.BUY:
            self._bid_price_set.discard(price)
            if price in self.bids:
                del self.bids[price]
        else:
            self._ask_price_set.discard(price)
            if price in self.asks:
                del self.asks[price]
