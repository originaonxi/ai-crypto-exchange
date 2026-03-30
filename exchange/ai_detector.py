"""
AI-Powered Anomaly Detection using Claude API.

Detects:
- Flash crashes (sudden price drops)
- Pump-and-dump schemes (coordinated volume + price manipulation)
- Unusual order patterns (spoofing, layering)
- Volume anomalies that precede market impact

Uses Anthropic's tool_use to structure detection decisions,
enabling automatic halt when threats are identified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Try to import anthropic; gracefully degrade if not available
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    logger.warning("anthropic package not installed — AI detection will use rule-based fallback")


@dataclass
class AnomalyAlert:
    alert_id: str
    alert_type: str  # flash_crash, pump_dump, spoofing, volume_spike
    symbol: str
    severity: str  # LOW, MEDIUM, HIGH, CRITICAL
    description: str
    recommendation: str  # MONITOR, WARN, HALT
    confidence: float  # 0.0 - 1.0
    timestamp: float
    metadata: dict = field(default_factory=dict)


@dataclass
class MarketSnapshot:
    symbol: str
    current_price: float
    price_5s_ago: float
    price_30s_ago: float
    price_60s_ago: float
    volume_5s: float
    volume_30s: float
    volume_60s: float
    bid_ask_spread: float
    order_count_5s: int
    cancel_count_5s: int
    unique_clients_5s: int


# Claude tool definitions for structured anomaly detection
ANOMALY_TOOLS = [
    {
        "name": "report_anomaly",
        "description": "Report a detected market anomaly with severity and recommended action",
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_type": {
                    "type": "string",
                    "enum": ["flash_crash", "pump_dump", "spoofing", "layering", "volume_spike", "wash_trading"],
                    "description": "Type of market anomaly detected",
                },
                "severity": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    "description": "Severity level. CRITICAL = immediate halt recommended",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence in detection from 0.0 to 1.0",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of the anomaly",
                },
                "recommendation": {
                    "type": "string",
                    "enum": ["MONITOR", "WARN", "HALT"],
                    "description": "Recommended action",
                },
            },
            "required": ["alert_type", "severity", "confidence", "description", "recommendation"],
        },
    },
    {
        "name": "no_anomaly",
        "description": "Report that no anomaly was detected in the current market data",
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Brief explanation of why the market data appears normal",
                },
            },
            "required": ["reasoning"],
        },
    },
]


class AIAnomalyDetector:
    """
    Hybrid anomaly detector: rule-based fast path + Claude API deep analysis.

    Rule-based checks run on every trade (microseconds).
    Claude API analysis runs periodically or when rules flag suspicious activity.
    """

    def __init__(
        self,
        halt_callback: Optional[Callable[[str], None]] = None,
        api_key: Optional[str] = None,
        analysis_interval: float = 30.0,
        model: str = "claude-sonnet-4-20250514",
    ):
        self.halt_callback = halt_callback
        self.model = model
        self.analysis_interval = analysis_interval
        self.alerts: list[AnomalyAlert] = []

        # Market data buffers
        self._prices: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=10000)
        )
        self._volumes: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=10000)
        )
        self._orders: dict[str, deque[tuple[float, str, str]]] = defaultdict(
            lambda: deque(maxlen=10000)
        )  # (time, client_id, action)
        self._cancels: dict[str, deque[tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=10000)
        )

        # Claude client
        self._client = None
        if HAS_ANTHROPIC:
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if key:
                self._client = anthropic.Anthropic(api_key=key)
                logger.info("Claude API anomaly detection enabled")
            else:
                logger.warning("No ANTHROPIC_API_KEY — using rule-based detection only")

        self._last_analysis: dict[str, float] = {}

    def record_trade(self, symbol: str, price: float, volume: float, buyer: str, seller: str):
        now = time.time()
        self._prices[symbol].append((now, price))
        self._volumes[symbol].append((now, volume * price))
        self._orders[symbol].append((now, buyer, "BUY"))
        self._orders[symbol].append((now, seller, "SELL"))

    def record_cancel(self, symbol: str, client_id: str):
        now = time.time()
        self._cancels[symbol].append((now, client_id))

    def check_rules(self, symbol: str) -> Optional[AnomalyAlert]:
        """Fast rule-based checks (runs on every trade)."""
        now = time.time()
        prices = self._prices[symbol]

        if len(prices) < 5:
            return None

        current_price = prices[-1][1]

        # Flash crash: >5% drop in 5 seconds
        prices_5s = [p for t, p in prices if now - t < 5]
        if len(prices_5s) >= 2:
            max_price = max(prices_5s)
            if max_price > 0:
                drop = (max_price - current_price) / max_price
                if drop > 0.05:
                    alert = AnomalyAlert(
                        alert_id=f"FC-{int(now)}",
                        alert_type="flash_crash",
                        symbol=symbol,
                        severity="CRITICAL",
                        description=f"{symbol} dropped {drop:.1%} in <5s (${max_price:.2f} -> ${current_price:.2f})",
                        recommendation="HALT",
                        confidence=0.95,
                        timestamp=now,
                        metadata={"drop_pct": drop, "from": max_price, "to": current_price},
                    )
                    self.alerts.append(alert)
                    if self.halt_callback:
                        self.halt_callback(alert.description)
                    return alert

        # Spoofing: >80% cancel rate from a single client in 10s
        cancels_10s = [(t, c) for t, c in self._cancels[symbol] if now - t < 10]
        orders_10s = [(t, c, a) for t, c, a in self._orders[symbol] if now - t < 10]

        client_orders: dict[str, int] = defaultdict(int)
        client_cancels: dict[str, int] = defaultdict(int)
        for _, client, _ in orders_10s:
            client_orders[client] += 1
        for _, client in cancels_10s:
            client_cancels[client] += 1

        for client, cancel_count in client_cancels.items():
            order_count = client_orders.get(client, 0)
            if order_count >= 5 and cancel_count / order_count > 0.7:
                alert = AnomalyAlert(
                    alert_id=f"SP-{int(now)}",
                    alert_type="spoofing",
                    symbol=symbol,
                    severity="HIGH",
                    description=f"Client {client} cancelled {cancel_count}/{order_count} orders on {symbol} in 10s",
                    recommendation="WARN",
                    confidence=0.85,
                    timestamp=now,
                    metadata={"client": client, "cancel_rate": cancel_count / order_count},
                )
                self.alerts.append(alert)
                return alert

        # Pump pattern: >50% price increase with concentrated buying
        prices_30s = [p for t, p in prices if now - t < 30]
        if len(prices_30s) >= 5:
            start_price = prices_30s[0]
            if start_price > 0 and (current_price - start_price) / start_price > 0.50:
                buyers_30s = [c for t, c, a in self._orders[symbol] if now - t < 30 and a == "BUY"]
                unique_buyers = len(set(buyers_30s))
                if unique_buyers <= 3 and len(buyers_30s) >= 10:
                    alert = AnomalyAlert(
                        alert_id=f"PD-{int(now)}",
                        alert_type="pump_dump",
                        symbol=symbol,
                        severity="CRITICAL",
                        description=(
                            f"{symbol} up {((current_price - start_price) / start_price):.0%} in 30s "
                            f"with only {unique_buyers} buyer(s) across {len(buyers_30s)} trades"
                        ),
                        recommendation="HALT",
                        confidence=0.9,
                        timestamp=now,
                    )
                    self.alerts.append(alert)
                    if self.halt_callback:
                        self.halt_callback(alert.description)
                    return alert

        return None

    async def deep_analysis(self, symbol: str) -> Optional[AnomalyAlert]:
        """Claude API-powered deep analysis of market patterns."""
        if not self._client:
            return None

        now = time.time()
        last = self._last_analysis.get(symbol, 0)
        if now - last < self.analysis_interval:
            return None
        self._last_analysis[symbol] = now

        snapshot = self._build_snapshot(symbol)
        if not snapshot:
            return None

        prompt = self._build_analysis_prompt(snapshot)

        try:
            response = await asyncio.to_thread(
                self._client.messages.create,
                model=self.model,
                max_tokens=500,
                tools=ANOMALY_TOOLS,
                messages=[{"role": "user", "content": prompt}],
            )

            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "report_anomaly":
                        inp = block.input
                        alert = AnomalyAlert(
                            alert_id=f"AI-{int(now)}",
                            alert_type=inp["alert_type"],
                            symbol=symbol,
                            severity=inp["severity"],
                            description=inp["description"],
                            recommendation=inp["recommendation"],
                            confidence=inp["confidence"],
                            timestamp=now,
                            metadata={"ai_model": self.model},
                        )
                        self.alerts.append(alert)
                        if alert.recommendation == "HALT" and self.halt_callback:
                            self.halt_callback(f"AI detected: {alert.description}")
                        return alert
                    elif block.name == "no_anomaly":
                        logger.debug(f"AI: no anomaly on {symbol} — {block.input.get('reasoning', '')}")

        except Exception as e:
            logger.error(f"Claude API analysis failed: {e}")

        return None

    def _build_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        now = time.time()
        prices = self._prices[symbol]
        if len(prices) < 2:
            return None

        current_price = prices[-1][1]

        def price_at(seconds_ago: float) -> float:
            target = now - seconds_ago
            closest = current_price
            for t, p in reversed(prices):
                if t <= target:
                    closest = p
                    break
            return closest

        def volume_in(seconds: float) -> float:
            return sum(v for t, v in self._volumes[symbol] if now - t < seconds)

        orders_5s = [(t, c, a) for t, c, a in self._orders[symbol] if now - t < 5]
        cancels_5s = [(t, c) for t, c in self._cancels[symbol] if now - t < 5]

        return MarketSnapshot(
            symbol=symbol,
            current_price=current_price,
            price_5s_ago=price_at(5),
            price_30s_ago=price_at(30),
            price_60s_ago=price_at(60),
            volume_5s=volume_in(5),
            volume_30s=volume_in(30),
            volume_60s=volume_in(60),
            bid_ask_spread=0.0,
            order_count_5s=len(orders_5s),
            cancel_count_5s=len(cancels_5s),
            unique_clients_5s=len(set(c for _, c, _ in orders_5s)),
        )

    def _build_analysis_prompt(self, snap: MarketSnapshot) -> str:
        price_change_5s = (
            (snap.current_price - snap.price_5s_ago) / snap.price_5s_ago * 100
            if snap.price_5s_ago > 0 else 0
        )
        price_change_30s = (
            (snap.current_price - snap.price_30s_ago) / snap.price_30s_ago * 100
            if snap.price_30s_ago > 0 else 0
        )
        price_change_60s = (
            (snap.current_price - snap.price_60s_ago) / snap.price_60s_ago * 100
            if snap.price_60s_ago > 0 else 0
        )

        return f"""Analyze this crypto market data for anomalies (flash crashes, pump-and-dump, spoofing, wash trading).

Symbol: {snap.symbol}
Current Price: ${snap.current_price:.4f}
Price 5s ago: ${snap.price_5s_ago:.4f} ({price_change_5s:+.2f}%)
Price 30s ago: ${snap.price_30s_ago:.4f} ({price_change_30s:+.2f}%)
Price 60s ago: ${snap.price_60s_ago:.4f} ({price_change_60s:+.2f}%)

Volume (last 5s): ${snap.volume_5s:,.2f}
Volume (last 30s): ${snap.volume_30s:,.2f}
Volume (last 60s): ${snap.volume_60s:,.2f}

Orders in last 5s: {snap.order_count_5s}
Cancels in last 5s: {snap.cancel_count_5s}
Unique clients (5s): {snap.unique_clients_5s}

Use the report_anomaly tool if you detect manipulation or danger, or no_anomaly if the market looks healthy."""

    def get_recent_alerts(self, limit: int = 50) -> list[AnomalyAlert]:
        return self.alerts[-limit:]
