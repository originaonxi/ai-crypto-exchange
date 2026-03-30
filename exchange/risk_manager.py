"""
Risk Management System.

Real-time position tracking, price band enforcement, volume spike detection,
and circuit breaker (kill switch) functionality.

Inspired by Knight Capital's $440M loss — every order passes through risk checks
before execution, and the system can auto-halt on anomalies.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from exchange.order_book import Execution, Order, OrderType, Side

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    max_order_quantity: float = 1000.0
    max_order_value: float = 1_000_000.0
    max_position_per_symbol: float = 10_000.0
    max_total_position_value: float = 10_000_000.0
    price_band_pct: float = 0.10  # reject orders >10% from last trade
    max_orders_per_second: int = 1000
    volume_spike_multiplier: float = 5.0  # halt if volume > 5x rolling average
    volume_window_seconds: int = 60


@dataclass
class ClientPosition:
    symbol: str
    quantity: float = 0.0  # positive = long, negative = short
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


class RiskManager:
    def __init__(
        self,
        limits: Optional[RiskLimits] = None,
        halt_callback: Optional[Callable[[str], None]] = None,
    ):
        self.limits = limits or RiskLimits()
        self.halt_callback = halt_callback

        # Position tracking
        self.positions: dict[str, dict[str, ClientPosition]] = defaultdict(dict)  # client -> symbol -> pos

        # Rate limiting
        self._order_timestamps: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=5000))

        # Volume tracking for spike detection
        self._trade_volumes: dict[str, deque[tuple[float, float]]] = defaultdict(deque)  # symbol -> (time, volume)
        self._last_prices: dict[str, float] = {}

        # Rejection stats
        self.rejections: int = 0
        self.halts_triggered: int = 0

    def pre_trade_check(self, order: Order) -> tuple[bool, str]:
        """Run all risk checks before matching. Returns (allowed, reason)."""
        # 1. Order size limit
        if order.quantity > self.limits.max_order_quantity:
            self.rejections += 1
            return False, f"Quantity {order.quantity} exceeds max {self.limits.max_order_quantity}"

        # 2. Order value limit
        if order.order_type == OrderType.LIMIT:
            value = order.quantity * order.price
            if value > self.limits.max_order_value:
                self.rejections += 1
                return False, f"Order value ${value:,.2f} exceeds max ${self.limits.max_order_value:,.2f}"

        # 3. Price band check (limit orders only)
        if order.order_type == OrderType.LIMIT and order.symbol in self._last_prices:
            last_price = self._last_prices[order.symbol]
            deviation = abs(order.price - last_price) / last_price
            if deviation > self.limits.price_band_pct:
                self.rejections += 1
                return False, (
                    f"Price {order.price} deviates {deviation:.1%} from last "
                    f"trade {last_price} (max {self.limits.price_band_pct:.0%})"
                )

        # 4. Rate limiting
        now = time.time()
        timestamps = self._order_timestamps[order.client_id]
        timestamps.append(now)
        recent = sum(1 for t in timestamps if now - t < 1.0)
        if recent > self.limits.max_orders_per_second:
            self.rejections += 1
            return False, f"Rate limit exceeded: {recent} orders/sec (max {self.limits.max_orders_per_second})"

        # 5. Position limit check
        pos = self.positions[order.client_id].get(order.symbol)
        if pos:
            projected = pos.quantity + (order.quantity if order.side == Side.BUY else -order.quantity)
            if abs(projected) > self.limits.max_position_per_symbol:
                self.rejections += 1
                return False, f"Position limit: projected {projected} exceeds max {self.limits.max_position_per_symbol}"

        return True, "OK"

    def post_trade_update(self, execution: Execution):
        """Update positions and check for volume anomalies after a trade."""
        # Update last price
        self._last_prices[execution.symbol] = execution.price

        # Update buyer position
        self._update_position(
            execution.buyer_client_id,
            execution.symbol,
            execution.quantity,
            execution.price,
        )

        # Update seller position
        self._update_position(
            execution.seller_client_id,
            execution.symbol,
            -execution.quantity,
            execution.price,
        )

        # Track volume for spike detection
        now = time.time()
        volume = execution.quantity * execution.price
        self._trade_volumes[execution.symbol].append((now, volume))

        # Prune old entries
        cutoff = now - self.limits.volume_window_seconds
        while self._trade_volumes[execution.symbol] and self._trade_volumes[execution.symbol][0][0] < cutoff:
            self._trade_volumes[execution.symbol].popleft()

        # Check for volume spike
        self._check_volume_spike(execution.symbol)

    def _update_position(self, client_id: str, symbol: str, qty_delta: float, price: float):
        if symbol not in self.positions[client_id]:
            self.positions[client_id][symbol] = ClientPosition(symbol=symbol)

        pos = self.positions[client_id][symbol]
        old_qty = pos.quantity
        new_qty = old_qty + qty_delta

        if old_qty == 0:
            pos.avg_price = price
        elif (old_qty > 0 and qty_delta > 0) or (old_qty < 0 and qty_delta < 0):
            # Adding to position
            total_cost = (abs(old_qty) * pos.avg_price) + (abs(qty_delta) * price)
            pos.avg_price = total_cost / (abs(old_qty) + abs(qty_delta))
        else:
            # Reducing/flipping position
            closed_qty = min(abs(old_qty), abs(qty_delta))
            if old_qty > 0:
                pos.realized_pnl += closed_qty * (price - pos.avg_price)
            else:
                pos.realized_pnl += closed_qty * (pos.avg_price - price)

            if abs(qty_delta) > abs(old_qty):
                pos.avg_price = price

        pos.quantity = new_qty

    def _check_volume_spike(self, symbol: str):
        volumes = self._trade_volumes[symbol]
        if len(volumes) < 10:
            return

        now = time.time()
        recent_window = 5  # last 5 seconds
        historical_window = self.limits.volume_window_seconds

        recent_vol = sum(v for t, v in volumes if now - t < recent_window)
        total_vol = sum(v for t, v in volumes)
        avg_rate = total_vol / historical_window
        recent_rate = recent_vol / recent_window

        if avg_rate > 0 and recent_rate > avg_rate * self.limits.volume_spike_multiplier:
            reason = (
                f"Volume spike on {symbol}: "
                f"${recent_rate:,.0f}/sec vs avg ${avg_rate:,.0f}/sec "
                f"({recent_rate/avg_rate:.1f}x multiplier)"
            )
            logger.critical(reason)
            self.halts_triggered += 1
            if self.halt_callback:
                self.halt_callback(reason)

    def get_client_positions(self, client_id: str) -> dict[str, ClientPosition]:
        return dict(self.positions.get(client_id, {}))

    def get_risk_summary(self) -> dict:
        return {
            "total_clients": len(self.positions),
            "total_rejections": self.rejections,
            "halts_triggered": self.halts_triggered,
            "tracked_symbols": list(self._last_prices.keys()),
            "last_prices": dict(self._last_prices),
        }
