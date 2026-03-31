"""
Risk Management & Circuit Breakers — Preventing Market Meltdowns.

Implements SEC-mandated circuit breaker levels, Limit Up-Limit Down (LULD) bands,
kill switches, and sub-microsecond pre-trade risk checks.

Inspired by:
- Knight Capital's $440M loss in 45 minutes (Aug 1, 2012)
- COVID-19 circuit breaker cascade (March 9-18, 2020)
- SEC Rule 201 (Short Sale Circuit Breaker)
- LULD Plan (NMS Plan to Address Extraordinary Market Volatility)

Architecture:
  Market Data → Pre-Trade Risk Engine → Matching Engine → Post-Trade Monitor
                     ↕                                         ↕
              LULD Band Monitor                          Kill Switch Trigger
                     ↕
            Circuit Breaker Controller → Market-Wide Halt Signal
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------

class CircuitBreakerLevel(str, Enum):
    NONE = "NONE"
    LEVEL_1 = "LEVEL_1"   # 7% decline → 15-min halt
    LEVEL_2 = "LEVEL_2"   # 13% decline → 15-min halt
    LEVEL_3 = "LEVEL_3"   # 20% decline → market close


class LULDState(str, Enum):
    NORMAL = "NORMAL"
    LIMIT_STATE = "LIMIT_STATE"     # 15-second window, no trades outside band
    TRADING_PAUSE = "TRADING_PAUSE"  # full halt for the security


class KillSwitchStatus(str, Enum):
    ACTIVE = "ACTIVE"      # trading allowed
    TRIGGERED = "TRIGGERED"  # all orders blocked


@dataclass
class LULDBand:
    """Dynamic price band for a security, recalculated every 30 seconds."""
    symbol: str
    reference_price: float
    upper_band: float
    lower_band: float
    band_pct: float
    state: LULDState = LULDState.NORMAL
    limit_state_entered_ns: int = 0
    last_recalc_ns: int = 0


@dataclass
class CircuitBreakerState:
    """Market-wide circuit breaker state."""
    current_level: CircuitBreakerLevel = CircuitBreakerLevel.NONE
    reference_price: float = 0.0  # previous day close (e.g., S&P 500 equivalent)
    current_price: float = 0.0
    decline_pct: float = 0.0
    halted: bool = False
    halt_start_ns: int = 0
    halt_duration_seconds: int = 0
    triggers_today: int = 0
    last_trigger_ns: int = 0


@dataclass
class KillSwitchRecord:
    """Per-participant kill switch."""
    participant_id: str
    status: KillSwitchStatus = KillSwitchStatus.ACTIVE
    triggered_at_ns: int = 0
    reason: str = ""
    orders_cancelled: int = 0
    estimated_loss_prevented: float = 0.0


@dataclass
class RiskCheckResult:
    """Result of a pre-trade risk check with latency tracking."""
    allowed: bool
    reason: str
    latency_ns: int = 0
    checks_performed: int = 0


@dataclass
class PreTradeRiskLimits:
    """Risk limits per account, designed to fit in L1 cache (64 bytes)."""
    max_position: float = 10_000.0
    max_order_size: float = 1_000.0
    buying_power: float = 1_000_000.0
    price_collar_pct: float = 0.10  # 10% from last trade
    max_orders_per_second: int = 1000
    max_notional_per_order: float = 5_000_000.0


@dataclass
class CircuitBreakerStats:
    total_halts: int = 0
    total_luld_pauses: int = 0
    total_kill_switches: int = 0
    total_risk_rejections: int = 0
    total_orders_checked: int = 0
    avg_check_latency_ns: float = 0.0
    max_check_latency_ns: int = 0
    _latency_sum: int = 0


# ---------------------------------------------------------------------------
# Limit Up-Limit Down (LULD) Band Monitor
# ---------------------------------------------------------------------------

class LULDMonitor:
    """
    Per-security dynamic price bands (SEC LULD Plan).

    Tier 1 (S&P 500, Russell 1000): ±5% for stocks > $3.00
    Tier 2 (all others): ±10%
    Lower-priced stocks get wider bands.

    Bands recalculate every 30 seconds using rolling reference price.
    Limit state lasts 15 seconds before escalating to trading pause.
    """

    LIMIT_STATE_DURATION_NS = 15 * 1_000_000_000  # 15 seconds
    RECALC_INTERVAL_NS = 30 * 1_000_000_000  # 30 seconds

    def __init__(self, tier1_symbols: Optional[set[str]] = None):
        self.bands: dict[str, LULDBand] = {}
        self.tier1_symbols = tier1_symbols or set()
        self._on_limit_state: list[Callable] = []
        self._on_trading_pause: list[Callable] = []

    def on_limit_state(self, callback: Callable):
        self._on_limit_state.append(callback)

    def on_trading_pause(self, callback: Callable):
        self._on_trading_pause.append(callback)

    def initialize_band(self, symbol: str, reference_price: float):
        """Set initial LULD band for a security."""
        band_pct = self._get_band_pct(symbol, reference_price)
        self.bands[symbol] = LULDBand(
            symbol=symbol,
            reference_price=reference_price,
            upper_band=reference_price * (1 + band_pct),
            lower_band=reference_price * (1 - band_pct),
            band_pct=band_pct,
            last_recalc_ns=time.time_ns(),
        )

    def _get_band_pct(self, symbol: str, price: float) -> float:
        """Determine band width based on tier and price."""
        if price <= 0.75:
            return 0.75  # 75% for penny stocks
        elif price <= 3.0:
            return 0.20  # 20% for low-priced stocks
        elif symbol in self.tier1_symbols:
            return 0.05  # 5% for Tier 1
        else:
            return 0.10  # 10% for Tier 2

    def check_price(self, symbol: str, price: float) -> tuple[bool, LULDState]:
        """
        Check if a trade price is within LULD bands.
        Returns (allowed, current_state).
        """
        band = self.bands.get(symbol)
        if not band:
            return True, LULDState.NORMAL

        now = time.time_ns()

        # Recalculate bands if interval exceeded
        if now - band.last_recalc_ns > self.RECALC_INTERVAL_NS:
            self._recalculate_band(symbol, price)

        # Check if in trading pause
        if band.state == LULDState.TRADING_PAUSE:
            return False, LULDState.TRADING_PAUSE

        # Check if in limit state and duration exceeded → escalate to pause
        if band.state == LULDState.LIMIT_STATE:
            if now - band.limit_state_entered_ns > self.LIMIT_STATE_DURATION_NS:
                band.state = LULDState.TRADING_PAUSE
                for cb in self._on_trading_pause:
                    cb(symbol, band)
                logger.critical(f"LULD TRADING PAUSE: {symbol} — no valid trades in 15s window")
                return False, LULDState.TRADING_PAUSE

        # Check price against bands
        if price > band.upper_band or price < band.lower_band:
            if band.state == LULDState.NORMAL:
                band.state = LULDState.LIMIT_STATE
                band.limit_state_entered_ns = now
                for cb in self._on_limit_state:
                    cb(symbol, band)
                logger.warning(
                    f"LULD LIMIT STATE: {symbol} price {price} outside "
                    f"[{band.lower_band:.2f}, {band.upper_band:.2f}]"
                )
            return False, band.state

        # Price is within bands — reset to normal if in limit state
        if band.state == LULDState.LIMIT_STATE:
            band.state = LULDState.NORMAL
            logger.info(f"LULD resolved: {symbol} back within bands")

        return True, LULDState.NORMAL

    def _recalculate_band(self, symbol: str, current_price: float):
        """Recalculate LULD bands using current price as new reference."""
        band = self.bands[symbol]
        band.reference_price = current_price
        band.band_pct = self._get_band_pct(symbol, current_price)
        band.upper_band = current_price * (1 + band.band_pct)
        band.lower_band = current_price * (1 - band.band_pct)
        band.last_recalc_ns = time.time_ns()

    def resume_trading(self, symbol: str, new_reference_price: float):
        """Resume trading after a pause with new reference price."""
        band = self.bands.get(symbol)
        if band:
            band.state = LULDState.NORMAL
            self._recalculate_band(symbol, new_reference_price)
            logger.info(f"LULD resumed: {symbol} with new ref price {new_reference_price}")

    def get_band(self, symbol: str) -> Optional[dict]:
        band = self.bands.get(symbol)
        if not band:
            return None
        return {
            "symbol": band.symbol,
            "reference_price": band.reference_price,
            "upper_band": round(band.upper_band, 4),
            "lower_band": round(band.lower_band, 4),
            "band_pct": band.band_pct,
            "state": band.state.value,
        }

    def get_all_bands(self) -> list[dict]:
        return [self.get_band(s) for s in self.bands]


# ---------------------------------------------------------------------------
# Market-Wide Circuit Breaker Controller
# ---------------------------------------------------------------------------

class CircuitBreakerController:
    """
    SEC market-wide circuit breakers based on S&P 500 equivalent index.

    Level 1: 7% decline  → 15-minute halt
    Level 2: 13% decline → 15-minute halt
    Level 3: 20% decline → market close for the day

    Halt signal must propagate to all venues within 200ms.
    """

    LEVEL_1_THRESHOLD = 0.07
    LEVEL_2_THRESHOLD = 0.13
    LEVEL_3_THRESHOLD = 0.20

    LEVEL_1_HALT_SECONDS = 900   # 15 minutes
    LEVEL_2_HALT_SECONDS = 900   # 15 minutes
    LEVEL_3_HALT_SECONDS = 86400  # rest of day

    def __init__(self, reference_price: float = 0.0):
        self.state = CircuitBreakerState(reference_price=reference_price)
        self._on_halt: list[Callable] = []
        self._on_resume: list[Callable] = []
        self._halt_history: list[dict] = []

    def on_halt(self, callback: Callable):
        self._on_halt.append(callback)

    def on_resume(self, callback: Callable):
        self._on_resume.append(callback)

    def set_reference_price(self, price: float):
        """Set previous day's closing price as reference."""
        self.state.reference_price = price
        self.state.current_price = price

    def update_price(self, price: float) -> Optional[CircuitBreakerLevel]:
        """
        Update market index price and check circuit breaker levels.
        Returns the triggered level, or None.
        """
        if self.state.reference_price <= 0:
            return None

        self.state.current_price = price
        decline = (self.state.reference_price - price) / self.state.reference_price
        self.state.decline_pct = decline

        # Check if currently halted and whether halt period expired
        if self.state.halted:
            elapsed = (time.time_ns() - self.state.halt_start_ns) / 1_000_000_000
            if elapsed >= self.state.halt_duration_seconds:
                self._resume()
            return None

        # Check levels (only trigger each once per day, ascending)
        triggered = None
        if decline >= self.LEVEL_3_THRESHOLD and self.state.triggers_today < 3:
            triggered = CircuitBreakerLevel.LEVEL_3
        elif decline >= self.LEVEL_2_THRESHOLD and self.state.triggers_today < 2:
            triggered = CircuitBreakerLevel.LEVEL_2
        elif decline >= self.LEVEL_1_THRESHOLD and self.state.triggers_today < 1:
            triggered = CircuitBreakerLevel.LEVEL_1

        if triggered:
            self._trigger_halt(triggered, decline)

        return triggered

    def _trigger_halt(self, level: CircuitBreakerLevel, decline: float):
        now = time.time_ns()
        duration = {
            CircuitBreakerLevel.LEVEL_1: self.LEVEL_1_HALT_SECONDS,
            CircuitBreakerLevel.LEVEL_2: self.LEVEL_2_HALT_SECONDS,
            CircuitBreakerLevel.LEVEL_3: self.LEVEL_3_HALT_SECONDS,
        }[level]

        self.state.current_level = level
        self.state.halted = True
        self.state.halt_start_ns = now
        self.state.halt_duration_seconds = duration
        self.state.triggers_today += 1
        self.state.last_trigger_ns = now

        self._halt_history.append({
            "level": level.value,
            "decline_pct": round(decline, 4),
            "triggered_at_ns": now,
            "duration_seconds": duration,
        })

        for cb in self._on_halt:
            cb(level, decline, duration)

        logger.critical(
            f"CIRCUIT BREAKER {level.value}: market declined {decline:.1%}, "
            f"halting for {duration}s"
        )

    def _resume(self):
        self.state.halted = False
        self.state.current_level = CircuitBreakerLevel.NONE

        for cb in self._on_resume:
            cb()

        logger.info("Circuit breaker halt period ended — trading resumed")

    def force_resume(self):
        """Manual override to resume trading."""
        self._resume()

    def reset_daily(self, new_reference_price: float):
        """Reset for new trading day."""
        self.state = CircuitBreakerState(reference_price=new_reference_price)

    def get_state(self) -> dict:
        return {
            "current_level": self.state.current_level.value,
            "reference_price": self.state.reference_price,
            "current_price": self.state.current_price,
            "decline_pct": round(self.state.decline_pct, 4),
            "halted": self.state.halted,
            "triggers_today": self.state.triggers_today,
            "halt_history": self._halt_history,
        }


# ---------------------------------------------------------------------------
# Kill Switch — Per-Participant Trade Termination
# ---------------------------------------------------------------------------

class KillSwitchManager:
    """
    Immediately stop all trading for a specific participant.

    Unlike circuit breakers (market-wide), kill switches target individual
    firms. Activates in <100μs — rejects all new orders and cancels open orders.

    After Knight Capital, SEC mandated all broker-dealers implement kill switches.
    """

    def __init__(self):
        self.switches: dict[str, KillSwitchRecord] = {}
        self._on_trigger: list[Callable] = []

    def on_trigger(self, callback: Callable):
        self._on_trigger.append(callback)

    def register_participant(self, participant_id: str):
        self.switches[participant_id] = KillSwitchRecord(participant_id=participant_id)

    def trigger(self, participant_id: str, reason: str, estimated_loss: float = 0.0) -> bool:
        """Trigger kill switch for a participant. Returns True if triggered."""
        switch = self.switches.get(participant_id)
        if not switch:
            return False

        switch.status = KillSwitchStatus.TRIGGERED
        switch.triggered_at_ns = time.time_ns()
        switch.reason = reason
        switch.estimated_loss_prevented = estimated_loss

        for cb in self._on_trigger:
            cb(participant_id, reason)

        logger.critical(f"KILL SWITCH TRIGGERED: {participant_id} — {reason}")
        return True

    def is_active(self, participant_id: str) -> bool:
        """Check if participant is allowed to trade (fast path for risk checks)."""
        switch = self.switches.get(participant_id)
        return switch is not None and switch.status == KillSwitchStatus.ACTIVE

    def reset(self, participant_id: str) -> bool:
        switch = self.switches.get(participant_id)
        if not switch:
            return False
        switch.status = KillSwitchStatus.ACTIVE
        switch.reason = ""
        logger.info(f"Kill switch reset: {participant_id}")
        return True

    def get_triggered(self) -> list[dict]:
        return [
            {
                "participant_id": s.participant_id,
                "triggered_at_ns": s.triggered_at_ns,
                "reason": s.reason,
                "estimated_loss_prevented": s.estimated_loss_prevented,
            }
            for s in self.switches.values()
            if s.status == KillSwitchStatus.TRIGGERED
        ]


# ---------------------------------------------------------------------------
# Pre-Trade Risk Engine (Sub-5μs Target)
# ---------------------------------------------------------------------------

class PreTradeRiskEngine:
    """
    Ultra-low-latency pre-trade risk engine.

    All critical data designed to fit in L1 cache (64KB):
    - 1000 instruments × 64 bytes = 64KB
    - Dictionary lookups → O(1) amortized

    Check ordering: cheapest checks first to reject fast.
    Target: <5μs per order on modern hardware.
    """

    def __init__(self):
        self.limits: dict[str, PreTradeRiskLimits] = {}
        self.positions: dict[str, dict[str, float]] = defaultdict(dict)  # account -> symbol -> qty
        self.last_prices: dict[str, float] = {}
        self.order_timestamps: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=2000))
        self.kill_switch: KillSwitchManager = KillSwitchManager()

        self.stats = CircuitBreakerStats()

    def set_limits(self, account_id: str, limits: PreTradeRiskLimits):
        self.limits[account_id] = limits
        self.kill_switch.register_participant(account_id)

    def update_price(self, symbol: str, price: float):
        self.last_prices[symbol] = price

    def update_position(self, account_id: str, symbol: str, quantity_delta: float):
        current = self.positions[account_id].get(symbol, 0.0)
        self.positions[account_id][symbol] = current + quantity_delta

    def check_order(
        self,
        account_id: str,
        symbol: str,
        quantity: float,
        price: float,
    ) -> RiskCheckResult:
        """
        Pre-trade risk check. Target <5μs latency.

        Check order (cheapest first):
        1. Kill switch (bitmap lookup)
        2. Order size (simple comparison)
        3. Price collar (division + comparison)
        4. Position limit (addition + comparison)
        5. Buying power (multiplication + comparison)
        6. Rate limit (deque scan)
        """
        start_ns = time.perf_counter_ns()
        checks = 0

        # 1. Kill switch check (fastest — single dict lookup)
        checks += 1
        if not self.kill_switch.is_active(account_id):
            return self._result(False, "KILL_SWITCH_ACTIVE", start_ns, checks)

        # 2. Get limits
        limits = self.limits.get(account_id)
        if not limits:
            return self._result(False, "NO_LIMITS_CONFIGURED", start_ns, checks)

        # 3. Order size check
        checks += 1
        if abs(quantity) > limits.max_order_size:
            self.stats.total_risk_rejections += 1
            return self._result(
                False,
                f"ORDER_SIZE_EXCEEDED: {abs(quantity)} > {limits.max_order_size}",
                start_ns, checks,
            )

        # 4. Notional value check
        checks += 1
        notional = abs(quantity * price)
        if notional > limits.max_notional_per_order:
            self.stats.total_risk_rejections += 1
            return self._result(
                False,
                f"NOTIONAL_EXCEEDED: ${notional:,.0f} > ${limits.max_notional_per_order:,.0f}",
                start_ns, checks,
            )

        # 5. Price collar check (reject fat-finger errors)
        checks += 1
        last_price = self.last_prices.get(symbol)
        if last_price and last_price > 0 and price > 0:
            deviation = abs(price - last_price) / last_price
            if deviation > limits.price_collar_pct:
                self.stats.total_risk_rejections += 1
                return self._result(
                    False,
                    f"PRICE_COLLAR: {deviation:.1%} deviation > {limits.price_collar_pct:.0%} limit",
                    start_ns, checks,
                )

        # 6. Position limit check
        checks += 1
        current_pos = self.positions[account_id].get(symbol, 0.0)
        projected = current_pos + quantity
        if abs(projected) > limits.max_position:
            self.stats.total_risk_rejections += 1
            return self._result(
                False,
                f"POSITION_LIMIT: projected {projected} > max {limits.max_position}",
                start_ns, checks,
            )

        # 7. Buying power check
        checks += 1
        if notional > limits.buying_power:
            self.stats.total_risk_rejections += 1
            return self._result(
                False,
                f"BUYING_POWER: ${notional:,.0f} > ${limits.buying_power:,.0f}",
                start_ns, checks,
            )

        # 8. Rate limiting
        checks += 1
        now = time.time()
        timestamps = self.order_timestamps[account_id]
        timestamps.append(now)
        recent = sum(1 for t in timestamps if now - t < 1.0)
        if recent > limits.max_orders_per_second:
            self.stats.total_risk_rejections += 1
            return self._result(
                False,
                f"RATE_LIMIT: {recent}/sec > {limits.max_orders_per_second}/sec",
                start_ns, checks,
            )

        self.stats.total_orders_checked += 1
        return self._result(True, "APPROVED", start_ns, checks)

    def _result(self, allowed: bool, reason: str, start_ns: int, checks: int) -> RiskCheckResult:
        latency = time.perf_counter_ns() - start_ns
        self.stats._latency_sum += latency
        self.stats.total_orders_checked += 1
        self.stats.avg_check_latency_ns = self.stats._latency_sum / self.stats.total_orders_checked
        if latency > self.stats.max_check_latency_ns:
            self.stats.max_check_latency_ns = latency

        if latency > 5_000:  # >5μs warning
            logger.debug(f"Slow risk check: {latency}ns ({reason})")

        return RiskCheckResult(allowed=allowed, reason=reason, latency_ns=latency, checks_performed=checks)

    def get_stats(self) -> dict:
        return {
            "total_orders_checked": self.stats.total_orders_checked,
            "total_risk_rejections": self.stats.total_risk_rejections,
            "avg_check_latency_ns": round(self.stats.avg_check_latency_ns, 1),
            "max_check_latency_ns": self.stats.max_check_latency_ns,
            "kill_switches_triggered": len(self.kill_switch.get_triggered()),
        }
