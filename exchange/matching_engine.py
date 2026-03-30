"""
Matching Engine — single-threaded event loop inspired by LMAX Disruptor.

Processes orders sequentially through a ring buffer to eliminate lock contention.
Coordinates order books, WAL, risk checks, and market data distribution.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import uuid4

from exchange.order_book import (
    Execution,
    Order,
    OrderBook,
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    Side,
)
from exchange.wal import WAL, WALEntry, WALEventType

logger = logging.getLogger(__name__)


@dataclass
class EngineStats:
    orders_processed: int = 0
    orders_matched: int = 0
    orders_rejected: int = 0
    orders_cancelled: int = 0
    total_executions: int = 0
    total_volume: float = 0.0
    avg_latency_us: float = 0.0
    _latency_sum: float = 0.0


class RingBuffer:
    """Lock-free ring buffer for order event sequencing (LMAX Disruptor pattern)."""

    def __init__(self, size: int = 65536):
        if size & (size - 1) != 0:
            raise ValueError("Ring buffer size must be power of 2")
        self.size = size
        self.mask = size - 1
        self.buffer: list[Optional[Order]] = [None] * size
        self.write_cursor: int = 0
        self.read_cursor: int = 0

    def publish(self, order: Order) -> int:
        slot = self.write_cursor & self.mask
        self.buffer[slot] = order
        seq = self.write_cursor
        self.write_cursor += 1
        return seq

    def consume(self) -> Optional[Order]:
        if self.read_cursor >= self.write_cursor:
            return None
        slot = self.read_cursor & self.mask
        order = self.buffer[slot]
        self.buffer[slot] = None
        self.read_cursor += 1
        return order

    @property
    def pending(self) -> int:
        return self.write_cursor - self.read_cursor


class MatchingEngine:
    """
    Core matching engine. All order processing is single-threaded.

    Flow: Gateway -> RingBuffer -> Match -> WAL -> Risk Check -> Broadcast
    """

    def __init__(
        self,
        symbols: list[str],
        wal: Optional[WAL] = None,
        risk_check: Optional[Callable[[Order, list[Execution]], bool]] = None,
    ):
        self.books: dict[str, OrderBook] = {s: OrderBook(s) for s in symbols}
        self.ring_buffer = RingBuffer()
        self.wal = wal
        self.risk_check = risk_check
        self.stats = EngineStats()
        self.halted = False
        self.halt_reason: Optional[str] = None

        # Event callbacks
        self._on_execution: list[Callable[[Execution], None]] = []
        self._on_book_update: list[Callable[[OrderBookSnapshot], None]] = []
        self._on_order_update: list[Callable[[Order], None]] = []

        self._running = False

    def on_execution(self, callback: Callable[[Execution], None]):
        self._on_execution.append(callback)

    def on_book_update(self, callback: Callable[[OrderBookSnapshot], None]):
        self._on_book_update.append(callback)

    def on_order_update(self, callback: Callable[[Order], None]):
        self._on_order_update.append(callback)

    def submit_order(self, order: Order) -> Order:
        """Submit an order to the engine. Returns the order with updated status."""
        if self.halted:
            order.status = OrderStatus.REJECTED
            logger.warning(f"Order {order.order_id} rejected: market halted ({self.halt_reason})")
            self.stats.orders_rejected += 1
            return order

        if order.symbol not in self.books:
            order.status = OrderStatus.REJECTED
            logger.warning(f"Order {order.order_id} rejected: unknown symbol {order.symbol}")
            self.stats.orders_rejected += 1
            return order

        if order.quantity <= 0:
            order.status = OrderStatus.REJECTED
            self.stats.orders_rejected += 1
            return order

        if order.order_type == OrderType.LIMIT and order.price <= 0:
            order.status = OrderStatus.REJECTED
            self.stats.orders_rejected += 1
            return order

        # WAL: log before processing
        if self.wal:
            self.wal.append(WALEntry(
                event_type=WALEventType.ORDER_NEW,
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side.value,
                order_type=order.order_type.value,
                price=order.price,
                quantity=order.quantity,
                client_id=order.client_id,
                timestamp_ns=order.timestamp_ns,
            ))

        start = time.perf_counter_ns()
        book = self.books[order.symbol]
        executions = book.add_order(order)
        elapsed_us = (time.perf_counter_ns() - start) / 1000

        # Stats
        self.stats.orders_processed += 1
        self.stats._latency_sum += elapsed_us
        self.stats.avg_latency_us = self.stats._latency_sum / self.stats.orders_processed

        if executions:
            self.stats.orders_matched += 1
            self.stats.total_executions += len(executions)
            for exe in executions:
                self.stats.total_volume += exe.price * exe.quantity
                if self.wal:
                    self.wal.append(WALEntry(
                        event_type=WALEventType.EXECUTION,
                        order_id=exe.execution_id,
                        symbol=exe.symbol,
                        side=exe.aggressor_side.value,
                        price=exe.price,
                        quantity=exe.quantity,
                        client_id=f"{exe.buyer_client_id}|{exe.seller_client_id}",
                        timestamp_ns=exe.timestamp_ns,
                    ))

        # Risk check
        if self.risk_check and not self.risk_check(order, executions):
            order.status = OrderStatus.REJECTED
            self.stats.orders_rejected += 1
            logger.warning(f"Order {order.order_id} rejected by risk check")

        # Notify listeners
        for cb in self._on_order_update:
            try:
                cb(order)
            except Exception as e:
                logger.error(f"Order update callback error: {e}")

        for exe in executions:
            for cb in self._on_execution:
                try:
                    cb(exe)
                except Exception as e:
                    logger.error(f"Execution callback error: {e}")

        # Broadcast book update
        snapshot = book.get_snapshot()
        for cb in self._on_book_update:
            try:
                cb(snapshot)
            except Exception as e:
                logger.error(f"Book update callback error: {e}")

        return order

    def cancel_order(self, order_id: str) -> Optional[Order]:
        for book in self.books.values():
            if order_id in book.orders:
                order = book.cancel_order(order_id)
                if order and self.wal:
                    self.wal.append(WALEntry(
                        event_type=WALEventType.ORDER_CANCEL,
                        order_id=order.order_id,
                        symbol=order.symbol,
                        side=order.side.value,
                        price=order.price,
                        quantity=order.remaining_quantity,
                        client_id=order.client_id,
                        timestamp_ns=time.time_ns(),
                    ))
                self.stats.orders_cancelled += 1
                return order
        return None

    def halt_trading(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        logger.critical(f"TRADING HALTED: {reason}")
        if self.wal:
            self.wal.append(WALEntry(
                event_type=WALEventType.HALT,
                order_id="SYSTEM",
                symbol="ALL",
                side="",
                price=0,
                quantity=0,
                client_id="SYSTEM",
                timestamp_ns=time.time_ns(),
            ))

    def resume_trading(self):
        self.halted = False
        self.halt_reason = None
        logger.info("Trading resumed")

    def get_order(self, order_id: str) -> Optional[Order]:
        for book in self.books.values():
            if order_id in book.orders:
                return book.orders[order_id]
        return None

    def get_book_snapshot(self, symbol: str, depth: int = 20) -> Optional[OrderBookSnapshot]:
        book = self.books.get(symbol)
        if book:
            return book.get_snapshot(depth)
        return None

    def create_order(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: float,
        price: float = 0.0,
        client_id: str = "anonymous",
    ) -> Order:
        """Helper to create and submit an order in one call."""
        order = Order(
            order_id=uuid4().hex[:16],
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            timestamp_ns=time.time_ns(),
            client_id=client_id,
        )
        return self.submit_order(order)
