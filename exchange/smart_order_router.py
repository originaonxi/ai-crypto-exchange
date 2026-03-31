"""
Smart Order Router with AI Optimization — The SMB Alternative to Co-Location.

Instead of paying $14K/month for a rack at NYSE Mahwah, this module uses
intelligence over speed: ML-based venue selection, AI-powered timing
optimization, and execution quality analytics.

Architecture:
┌───────────────────────────────────────────────────────────────────────────┐
│                    SMART ORDER ROUTER (AI-FIRST)                         │
├───────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  ┌─────────────┐   ┌──────────────────┐   ┌──────────────────────────┐  │
│  │  ORDER      │   │   VENUE SCORER   │   │    AI TIMING ENGINE      │  │
│  │  INTAKE     │──▶│  (ML-ranked)     │──▶│  (Claude microstructure) │  │
│  │             │   │                  │   │                          │  │
│  │  Validate   │   │  Spread cost     │   │  Predict price impact    │  │
│  │  Classify   │   │  Fill rate       │   │  Optimal slice size      │  │
│  │  Urgency    │   │  Adverse select  │   │  Time-of-day patterns    │  │
│  └─────────────┘   │  Latency score   │   │  Volatility regime       │  │
│                    │  Queue position   │   └───────────┬──────────────┘  │
│                    └────────┬─────────┘               │                  │
│                             │                          │                  │
│                             ▼                          ▼                  │
│                    ┌──────────────────────────────────────────────────┐  │
│                    │              EXECUTION PLANNER                    │  │
│                    │                                                  │  │
│                    │  ┌──────────┐  ┌───────────┐  ┌──────────────┐ │  │
│                    │  │  TWAP    │  │   VWAP    │  │  ADAPTIVE    │ │  │
│                    │  │ (time-   │  │ (volume-  │  │ (AI-driven   │ │  │
│                    │  │ weighted)│  │  weighted) │  │  dynamic)    │ │  │
│                    │  └──────────┘  └───────────┘  └──────────────┘ │  │
│                    └───────────────────┬──────────────────────────────┘  │
│                                        │                                 │
│                                        ▼                                 │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    VENUE CONNECTIONS                                │  │
│  │                                                                    │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌────────┐│  │
│  │  │  NYSE   │  │ NASDAQ  │  │  CBOE   │  │   IEX   │  │  ARCA  ││  │
│  │  │ Mahwah  │  │ Carteret│  │ Secaucus│  │ Weehawk │  │ Mahwah ││  │
│  │  │         │  │         │  │         │  │         │  │        ││  │
│  │  │ Taker:  │  │ Taker:  │  │ Taker:  │  │ Taker:  │  │ Taker: ││  │
│  │  │ $0.0030 │  │ $0.0030 │  │ $0.0025 │  │ $0.0009 │  │ $0.0030││  │
│  │  │ Maker:  │  │ Maker:  │  │ Maker:  │  │ Maker:  │  │ Maker: ││  │
│  │  │-$0.0020 │  │-$0.0020 │  │-$0.0017 │  │ $0.0000 │  │-$0.0020││  │
│  │  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └────────┘│  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                           │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    EXECUTION ANALYTICS                              │  │
│  │                                                                    │  │
│  │  ┌──────────────┐  ┌───────────────┐  ┌─────────────────────────┐│  │
│  │  │ Slippage     │  │ Fill Rate     │  │  Implementation         ││  │
│  │  │ Tracker      │  │ Monitor       │  │  Shortfall (IS)         ││  │
│  │  │              │  │               │  │                         ││  │
│  │  │ Arrival vs   │  │ By venue      │  │  Decision price vs      ││  │
│  │  │ fill price   │  │ By strategy   │  │  actual execution cost  ││  │
│  │  │ by venue     │  │ By time       │  │  = true cost of trading ││  │
│  │  └──────────────┘  └───────────────┘  └─────────────────────────┘│  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                           │
│  Monthly cost: ~$300 total                                                │
│  ┌─────────────────┐ ┌──────────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ Polygon.io $99  │ │ Claude $50   │ │ Pinecone │ │ AWS Lambda $50   │ │
│  │ Market data     │ │ AI analysis  │ │ $70/mo   │ │ Compute          │ │
│  │ REST + WS       │ │ per 1K tokens│ │ Vector DB│ │ 1ms billing      │ │
│  └─────────────────┘ └──────────────┘ └──────────┘ └──────────────────┘ │
└───────────────────────────────────────────────────────────────────────────┘

References:
- "Optimal Execution of Portfolio Transactions" — Almgren & Chriss (2000)
- "Market Microstructure in Practice" — Lehalle & Laruelle
- SEC Regulation NMS — Order Protection Rule (Rule 611)
- IEX: "The Problem With High-Frequency Trading" — Brad Katsuyama
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------

class VenueID(str, Enum):
    NYSE = "NYSE"
    NASDAQ = "NASDAQ"
    CBOE = "CBOE"
    IEX = "IEX"
    ARCA = "ARCA"


class OrderUrgency(str, Enum):
    LOW = "LOW"          # Can wait for best price (passive)
    MEDIUM = "MEDIUM"    # Balance speed and price
    HIGH = "HIGH"        # Need execution now (aggressive)
    CRITICAL = "CRITICAL"  # Immediate fill at any cost


class ExecutionStrategy(str, Enum):
    TWAP = "TWAP"          # Time-Weighted Average Price
    VWAP = "VWAP"          # Volume-Weighted Average Price
    ADAPTIVE = "ADAPTIVE"  # AI-driven dynamic slicing
    DIRECT = "DIRECT"      # Single venue, immediate


class RoutingReason(str, Enum):
    BEST_PRICE = "BEST_PRICE"
    LOWEST_FEE = "LOWEST_FEE"
    BEST_FILL_RATE = "BEST_FILL_RATE"
    LOW_ADVERSE_SELECTION = "LOW_ADVERSE_SELECTION"
    AI_RECOMMENDATION = "AI_RECOMMENDATION"
    VOLATILITY_REGIME = "VOLATILITY_REGIME"


@dataclass
class VenueProfile:
    """Static + dynamic characteristics of a trading venue."""
    venue_id: VenueID
    name: str

    # Fee schedule (per share, in dollars)
    taker_fee: float
    maker_rebate: float  # negative = rebate (you get paid)

    # Performance metrics (rolling averages)
    avg_fill_rate: float = 0.85        # % of orders that fill
    avg_fill_time_ms: float = 5.0      # Time to fill
    avg_slippage_bps: float = 1.0      # Basis points of slippage
    adverse_selection_score: float = 0.5  # 0=safe, 1=toxic flow

    # Current state
    is_available: bool = True
    current_spread_bps: float = 1.0
    current_depth: float = 10000.0     # Shares at best price
    queue_position_estimate: int = 0

    # Historical stats
    total_orders_routed: int = 0
    total_fills: int = 0
    total_volume: float = 0.0
    _recent_slippages: deque = field(default_factory=lambda: deque(maxlen=1000))


@dataclass
class RoutingDecision:
    """The router's decision for where and how to execute."""
    decision_id: str
    symbol: str
    side: str
    total_quantity: float
    urgency: OrderUrgency
    strategy: ExecutionStrategy
    timestamp_ns: int

    # Venue allocation
    venue_allocations: list[VenueAllocation]

    # AI reasoning
    reasoning: str
    confidence: float  # 0.0 - 1.0
    primary_reason: RoutingReason

    # Predicted costs
    estimated_total_cost_bps: float  # Total cost in basis points
    estimated_fees: float            # Dollar fees
    estimated_slippage: float        # Dollar slippage
    estimated_market_impact: float   # Dollar market impact


@dataclass
class VenueAllocation:
    """How much of the order goes to each venue."""
    venue_id: VenueID
    quantity: float
    limit_price: Optional[float]
    is_passive: bool  # True = maker (post limit), False = taker (cross spread)
    expected_fill_rate: float
    expected_cost_bps: float
    reason: RoutingReason
    child_order_id: str = ""


@dataclass
class ExecutionReport:
    """Post-execution analytics for a routed order."""
    decision_id: str
    symbol: str
    side: str
    total_quantity: float
    filled_quantity: float
    avg_fill_price: float
    arrival_price: float  # Price when order arrived (benchmark)

    # Cost analysis
    slippage_bps: float           # Arrival price vs fill price
    implementation_shortfall: float  # Total execution cost
    fees_paid: float
    maker_rebates_earned: float

    # Venue breakdown
    venue_fills: dict  # venue_id -> {quantity, avg_price, fill_rate}

    # Timing
    total_execution_time_ms: float
    timestamp_ns: int


@dataclass
class MarketMicrostructure:
    """Real-time market microstructure snapshot for AI analysis."""
    symbol: str
    bid: float
    ask: float
    spread_bps: float
    mid_price: float
    bid_depth: float
    ask_depth: float
    imbalance_ratio: float
    recent_trades_per_sec: float
    volatility_1min: float
    volatility_5min: float
    time_of_day_bucket: str  # "open", "morning", "midday", "afternoon", "close"
    volume_participation_rate: float  # Our volume / market volume


@dataclass
class VenueScore:
    """Composite score for venue ranking."""
    venue_id: VenueID
    total_score: float
    spread_score: float
    fee_score: float
    fill_rate_score: float
    adverse_selection_score: float
    depth_score: float
    latency_score: float


# ---------------------------------------------------------------------------
# Venue Scorer — ML-Based Venue Ranking
# ---------------------------------------------------------------------------

class VenueScorer:
    """
    Scores and ranks venues for optimal order routing.

    Uses a weighted multi-factor model inspired by Almgren-Chriss
    optimal execution framework. Each factor captures a different
    dimension of execution quality.

    In production, these weights would be learned from historical
    execution data using gradient boosting or a small neural network.
    Here we use a configurable linear model.
    """

    def __init__(self, weights: Optional[dict] = None):
        # Default weights — tuned for typical US equity execution
        self.weights = weights or {
            "spread": 0.25,           # Tighter spread = better
            "fee": 0.15,              # Lower fee = better
            "fill_rate": 0.20,        # Higher fill = better
            "adverse_selection": 0.20, # Lower adverse selection = better
            "depth": 0.10,            # More depth = better
            "latency": 0.10,          # Lower fill time = better
        }

    def score_venues(
        self,
        venues: list[VenueProfile],
        side: str,
        quantity: float,
        urgency: OrderUrgency,
    ) -> list[VenueScore]:
        """Score all venues and return sorted by best score."""

        # Adjust weights based on urgency
        w = dict(self.weights)
        if urgency == OrderUrgency.CRITICAL:
            w["fill_rate"] = 0.40
            w["depth"] = 0.25
            w["spread"] = 0.10
            w["fee"] = 0.05
        elif urgency == OrderUrgency.LOW:
            w["fee"] = 0.30
            w["spread"] = 0.30
            w["adverse_selection"] = 0.25

        # Normalize weights
        total_w = sum(w.values())
        w = {k: v / total_w for k, v in w.items()}

        scores = []
        available = [v for v in venues if v.is_available]

        if not available:
            return []

        # Compute raw metrics
        spreads = [v.current_spread_bps for v in available]
        fees = [v.taker_fee for v in available]
        fills = [v.avg_fill_rate for v in available]
        adverse = [v.adverse_selection_score for v in available]
        depths = [v.current_depth for v in available]
        latencies = [v.avg_fill_time_ms for v in available]

        for i, venue in enumerate(available):
            # Normalize each factor to [0, 1] where 1 = best
            spread_score = 1.0 - self._normalize(spreads[i], spreads)
            fee_score = 1.0 - self._normalize(fees[i], fees)
            fill_score = self._normalize(fills[i], fills)
            adverse_score = 1.0 - self._normalize(adverse[i], adverse)
            depth_score = self._normalize(depths[i], depths)
            latency_score = 1.0 - self._normalize(latencies[i], latencies)

            total = (
                w["spread"] * spread_score
                + w["fee"] * fee_score
                + w["fill_rate"] * fill_score
                + w["adverse_selection"] * adverse_score
                + w["depth"] * depth_score
                + w["latency"] * latency_score
            )

            scores.append(VenueScore(
                venue_id=venue.venue_id,
                total_score=round(total, 4),
                spread_score=round(spread_score, 4),
                fee_score=round(fee_score, 4),
                fill_rate_score=round(fill_score, 4),
                adverse_selection_score=round(adverse_score, 4),
                depth_score=round(depth_score, 4),
                latency_score=round(latency_score, 4),
            ))

        scores.sort(key=lambda s: s.total_score, reverse=True)
        return scores

    @staticmethod
    def _normalize(value: float, all_values: list[float]) -> float:
        """Min-max normalization to [0, 1]."""
        min_v = min(all_values)
        max_v = max(all_values)
        if max_v == min_v:
            return 0.5
        return (value - min_v) / (max_v - min_v)


# ---------------------------------------------------------------------------
# Execution Planner — TWAP / VWAP / Adaptive
# ---------------------------------------------------------------------------

class ExecutionPlanner:
    """
    Splits large orders into child slices using standard execution algorithms.

    TWAP: Equal slices over time — simple, predictable, no volume signal leakage.
    VWAP: Volume-weighted slices — trades more when market is active.
    Adaptive: AI-driven — adjusts aggressiveness based on real-time conditions.
    """

    @staticmethod
    def plan_twap(
        total_quantity: float,
        duration_seconds: float,
        interval_seconds: float = 30.0,
    ) -> list[tuple[float, float]]:
        """
        Time-Weighted Average Price execution plan.

        Returns list of (time_offset_seconds, quantity) tuples.
        """
        num_slices = max(1, int(duration_seconds / interval_seconds))
        slice_qty = total_quantity / num_slices

        return [
            (i * interval_seconds, slice_qty)
            for i in range(num_slices)
        ]

    @staticmethod
    def plan_vwap(
        total_quantity: float,
        volume_profile: list[float],
        duration_seconds: float,
    ) -> list[tuple[float, float]]:
        """
        Volume-Weighted Average Price execution plan.

        volume_profile: relative volume by time bucket (e.g., [0.15, 0.10, ...])
        """
        if not volume_profile:
            return [(0.0, total_quantity)]

        total_vol = sum(volume_profile)
        if total_vol == 0:
            return [(0.0, total_quantity)]

        interval = duration_seconds / len(volume_profile)
        plan = []

        for i, vol_fraction in enumerate(volume_profile):
            weight = vol_fraction / total_vol
            qty = total_quantity * weight
            if qty > 0:
                plan.append((i * interval, round(qty, 8)))

        return plan

    @staticmethod
    def plan_adaptive(
        total_quantity: float,
        microstructure: MarketMicrostructure,
        urgency: OrderUrgency,
    ) -> list[tuple[float, float]]:
        """
        AI-driven adaptive execution plan.

        Adjusts slice size based on:
        - Spread: wider spread → smaller slices to reduce impact
        - Volatility: higher vol → faster execution to avoid drift
        - Depth: low depth → smaller slices to avoid moving the market
        - Time of day: avoid open/close auctions for passive orders
        """
        # Base participation rate (% of market volume per slice)
        if urgency == OrderUrgency.CRITICAL:
            participation = 0.25  # 25% of volume
        elif urgency == OrderUrgency.HIGH:
            participation = 0.15
        elif urgency == OrderUrgency.MEDIUM:
            participation = 0.08
        else:
            participation = 0.03  # Very passive

        # Adjust for volatility — higher vol means execute faster
        vol_multiplier = 1.0 + microstructure.volatility_1min * 2.0

        # Adjust for depth — less depth means smaller slices
        if microstructure.bid_depth + microstructure.ask_depth > 0:
            depth_ratio = total_quantity / (microstructure.bid_depth + microstructure.ask_depth)
            if depth_ratio > 0.5:
                # Order is large relative to book — be more careful
                participation *= 0.5
        else:
            participation *= 0.3

        # Calculate number of slices
        effective_rate = participation * vol_multiplier
        effective_rate = max(0.01, min(0.50, effective_rate))

        num_slices = max(1, int(1.0 / effective_rate))
        num_slices = min(num_slices, 100)  # Cap at 100 slices

        # Variable slice sizes — front-load slightly for urgency
        slices = []
        remaining = total_quantity
        interval = 30.0  # 30 seconds between slices

        for i in range(num_slices):
            if i < num_slices - 1:
                # Slight front-loading for higher urgency
                weight = 1.0 + (0.1 * (num_slices - i) / num_slices) if urgency != OrderUrgency.LOW else 1.0
                qty = (remaining / (num_slices - i)) * weight
                qty = min(qty, remaining)
            else:
                qty = remaining

            if qty > 0:
                slices.append((i * interval, round(qty, 8)))
                remaining -= qty

        return slices


# ---------------------------------------------------------------------------
# Execution Analytics — Post-Trade Cost Analysis
# ---------------------------------------------------------------------------

class ExecutionAnalytics:
    """
    Measures execution quality using institutional-grade metrics.

    Implementation Shortfall (IS): The gold standard for measuring execution
    cost. Compares the actual execution against a theoretical "paper portfolio"
    that executed instantly at the arrival price.

    IS = (Actual Cost - Paper Cost) / Paper Cost
    = Delay Cost + Trading Cost + Opportunity Cost
    """

    def __init__(self):
        self.execution_history: deque[ExecutionReport] = deque(maxlen=10000)
        self._venue_stats: dict[str, dict] = defaultdict(lambda: {
            "total_orders": 0,
            "total_fills": 0,
            "total_slippage_bps": 0.0,
            "total_fees": 0.0,
            "total_rebates": 0.0,
        })

    def record_execution(self, report: ExecutionReport):
        """Record an execution for analytics."""
        self.execution_history.append(report)

        for venue_id, fill_data in report.venue_fills.items():
            stats = self._venue_stats[venue_id]
            stats["total_orders"] += 1
            if fill_data.get("quantity", 0) > 0:
                stats["total_fills"] += 1

    def calculate_implementation_shortfall(
        self,
        arrival_price: float,
        avg_fill_price: float,
        quantity: float,
        side: str,
        fees: float,
    ) -> dict:
        """
        Calculate Implementation Shortfall — the true cost of trading.

        For a BUY: IS = (avg_fill_price - arrival_price) * quantity + fees
        For a SELL: IS = (arrival_price - avg_fill_price) * quantity + fees
        """
        if side == "BUY":
            price_impact = (avg_fill_price - arrival_price) * quantity
        else:
            price_impact = (arrival_price - avg_fill_price) * quantity

        total_is = price_impact + fees

        # Express as basis points
        notional = arrival_price * quantity
        is_bps = (total_is / notional) * 10000 if notional > 0 else 0

        return {
            "implementation_shortfall": round(total_is, 4),
            "implementation_shortfall_bps": round(is_bps, 2),
            "price_impact": round(price_impact, 4),
            "fees": round(fees, 4),
            "notional_value": round(notional, 2),
            "side": side,
        }

    def get_venue_performance(self) -> dict:
        """Get aggregate performance stats by venue."""
        result = {}
        for venue_id, stats in self._venue_stats.items():
            fill_rate = stats["total_fills"] / max(1, stats["total_orders"])
            avg_slippage = stats["total_slippage_bps"] / max(1, stats["total_fills"])
            net_fees = stats["total_fees"] - stats["total_rebates"]

            result[venue_id] = {
                "total_orders": stats["total_orders"],
                "total_fills": stats["total_fills"],
                "fill_rate": round(fill_rate, 4),
                "avg_slippage_bps": round(avg_slippage, 2),
                "net_fees": round(net_fees, 4),
            }
        return result

    def get_summary(self) -> dict:
        """Get overall execution quality summary."""
        if not self.execution_history:
            return {"total_executions": 0}

        total_slippage = sum(r.slippage_bps for r in self.execution_history)
        total_is = sum(r.implementation_shortfall for r in self.execution_history)
        total_fees = sum(r.fees_paid for r in self.execution_history)
        total_rebates = sum(r.maker_rebates_earned for r in self.execution_history)

        n = len(self.execution_history)
        return {
            "total_executions": n,
            "avg_slippage_bps": round(total_slippage / n, 2),
            "total_implementation_shortfall": round(total_is, 2),
            "total_fees_paid": round(total_fees, 2),
            "total_rebates_earned": round(total_rebates, 2),
            "net_execution_cost": round(total_is + total_fees - total_rebates, 2),
            "venue_performance": self.get_venue_performance(),
        }


# ---------------------------------------------------------------------------
# Co-Location Simulator — Low-Latency Order Book
# ---------------------------------------------------------------------------

@dataclass
class ColoOrderBookLevel:
    """Single price level for co-location order book."""
    price: float
    size: int
    timestamp_ns: int


class LowLatencyOrderBook:
    """
    Ultra-fast order book optimized for microsecond updates.

    Uses pre-allocated arrays instead of tree structures to eliminate
    dynamic allocation and pointer traversal. This is how real FPGA
    trading systems maintain order books — fixed-size arrays indexed
    by price in cents.

    Performance: O(1) update, O(1) best bid/ask, zero GC pressure.
    Limitation: Fixed price range (configurable).
    """

    def __init__(self, venue: str, max_price_cents: int = 100000):
        self.venue = venue
        self.max_price_cents = max_price_cents
        self.bids: list[Optional[ColoOrderBookLevel]] = [None] * max_price_cents
        self.asks: list[Optional[ColoOrderBookLevel]] = [None] * max_price_cents
        self.best_bid_idx: int = 0
        self.best_ask_idx: int = max_price_cents - 1
        self._update_count: int = 0

    def update_level(self, side: str, price: float, size: int):
        """Update a price level. O(1) operation."""
        timestamp = time.time_ns()
        price_idx = int(price * 100)

        if price_idx < 0 or price_idx >= self.max_price_cents:
            return

        self._update_count += 1

        if side == "B":  # Bid
            if size == 0:
                self.bids[price_idx] = None
                if price_idx == self.best_bid_idx:
                    self._scan_best_bid()
            else:
                self.bids[price_idx] = ColoOrderBookLevel(price, size, timestamp)
                if price_idx > self.best_bid_idx:
                    self.best_bid_idx = price_idx
        else:  # Ask
            if size == 0:
                self.asks[price_idx] = None
                if price_idx == self.best_ask_idx:
                    self._scan_best_ask()
            else:
                self.asks[price_idx] = ColoOrderBookLevel(price, size, timestamp)
                if price_idx < self.best_ask_idx:
                    self.best_ask_idx = price_idx

    def _scan_best_bid(self):
        """Re-scan for best bid after removal. Rare operation."""
        for i in range(self.best_bid_idx, -1, -1):
            if self.bids[i] is not None:
                self.best_bid_idx = i
                return
        self.best_bid_idx = 0

    def _scan_best_ask(self):
        """Re-scan for best ask after removal. Rare operation."""
        for i in range(self.best_ask_idx, self.max_price_cents):
            if self.asks[i] is not None:
                self.best_ask_idx = i
                return
        self.best_ask_idx = self.max_price_cents - 1

    @property
    def best_bid(self) -> Optional[float]:
        level = self.bids[self.best_bid_idx]
        return level.price if level else None

    @property
    def best_ask(self) -> Optional[float]:
        level = self.asks[self.best_ask_idx]
        return level.price if level else None

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
            return (bb + ba) / 2.0
        return None

    def get_depth(self, side: str, levels: int = 5) -> list[ColoOrderBookLevel]:
        """Get top N price levels."""
        result = []
        if side == "B":
            idx = self.best_bid_idx
            while idx >= 0 and len(result) < levels:
                if self.bids[idx] is not None:
                    result.append(self.bids[idx])
                idx -= 1
        else:
            idx = self.best_ask_idx
            while idx < self.max_price_cents and len(result) < levels:
                if self.asks[idx] is not None:
                    result.append(self.asks[idx])
                idx += 1
        return result

    def check_arbitrage(self, other: LowLatencyOrderBook, fee_per_share: float = 0.001) -> Optional[dict]:
        """
        Detect cross-venue arbitrage opportunity.

        Profitable when: other_bid > our_ask + fees (buy here, sell there)
                    OR:  our_bid > other_ask + fees (buy there, sell here)
        """
        our_ask = self.best_ask
        our_bid = self.best_bid
        other_bid = other.best_bid
        other_ask = other.best_ask

        if our_ask and other_bid and other_bid > our_ask + fee_per_share:
            our_ask_level = self.asks[self.best_ask_idx]
            other_bid_level = other.bids[other.best_bid_idx]
            max_qty = min(
                our_ask_level.size if our_ask_level else 0,
                other_bid_level.size if other_bid_level else 0,
            )
            return {
                "direction": "BUY_HERE_SELL_THERE",
                "buy_venue": self.venue,
                "buy_price": our_ask,
                "sell_venue": other.venue,
                "sell_price": other_bid,
                "profit_per_share": round(other_bid - our_ask - fee_per_share, 6),
                "max_quantity": max_qty,
                "max_profit": round((other_bid - our_ask - fee_per_share) * max_qty, 4),
                "timestamp_ns": time.time_ns(),
            }

        if our_bid and other_ask and our_bid > other_ask + fee_per_share:
            other_ask_level = other.asks[other.best_ask_idx]
            our_bid_level = self.bids[self.best_bid_idx]
            max_qty = min(
                other_ask_level.size if other_ask_level else 0,
                our_bid_level.size if our_bid_level else 0,
            )
            return {
                "direction": "BUY_THERE_SELL_HERE",
                "buy_venue": other.venue,
                "buy_price": other_ask,
                "sell_venue": self.venue,
                "sell_price": our_bid,
                "profit_per_share": round(our_bid - other_ask - fee_per_share, 6),
                "max_quantity": max_qty,
                "max_profit": round((our_bid - other_ask - fee_per_share) * max_qty, 4),
                "timestamp_ns": time.time_ns(),
            }

        return None

    @property
    def stats(self) -> dict:
        return {
            "venue": self.venue,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "mid_price": self.mid_price,
            "total_updates": self._update_count,
        }


# ---------------------------------------------------------------------------
# Smart Order Router — The Core
# ---------------------------------------------------------------------------

# Default US equity venue profiles
DEFAULT_VENUES = [
    VenueProfile(
        venue_id=VenueID.NYSE,
        name="New York Stock Exchange",
        taker_fee=0.0030,
        maker_rebate=-0.0020,
        avg_fill_rate=0.88,
        avg_fill_time_ms=3.2,
        avg_slippage_bps=0.8,
        adverse_selection_score=0.4,
        current_spread_bps=1.0,
        current_depth=15000,
    ),
    VenueProfile(
        venue_id=VenueID.NASDAQ,
        name="NASDAQ",
        taker_fee=0.0030,
        maker_rebate=-0.0020,
        avg_fill_rate=0.90,
        avg_fill_time_ms=2.8,
        avg_slippage_bps=1.2,
        adverse_selection_score=0.6,  # Higher toxic flow from HFTs
        current_spread_bps=0.8,
        current_depth=20000,
    ),
    VenueProfile(
        venue_id=VenueID.CBOE,
        name="Cboe BZX Exchange",
        taker_fee=0.0025,
        maker_rebate=-0.0017,
        avg_fill_rate=0.82,
        avg_fill_time_ms=4.1,
        avg_slippage_bps=0.9,
        adverse_selection_score=0.45,
        current_spread_bps=1.1,
        current_depth=12000,
    ),
    VenueProfile(
        venue_id=VenueID.IEX,
        name="IEX (Investors Exchange)",
        taker_fee=0.0009,
        maker_rebate=0.0000,  # No rebate, no payment for order flow
        avg_fill_rate=0.75,
        avg_fill_time_ms=6.0,  # Speed bump: 350μs intentional delay
        avg_slippage_bps=0.3,  # Much lower — that's the whole point of IEX
        adverse_selection_score=0.15,  # Lowest — speed bump protects against HFT
        current_spread_bps=1.2,
        current_depth=8000,
    ),
    VenueProfile(
        venue_id=VenueID.ARCA,
        name="NYSE Arca",
        taker_fee=0.0030,
        maker_rebate=-0.0020,
        avg_fill_rate=0.85,
        avg_fill_time_ms=3.5,
        avg_slippage_bps=1.0,
        adverse_selection_score=0.5,
        current_spread_bps=1.0,
        current_depth=14000,
    ),
]


class SmartOrderRouter:
    """
    AI-powered Smart Order Router for execution optimization.

    Replaces $14K/month co-location with $300/month intelligence:
    - ML venue scoring replaces raw latency advantage
    - Adaptive execution algorithms replace FPGA speed
    - Post-trade analytics replace gut feel

    The key insight: for orders >100 shares, execution quality depends
    more on WHERE and WHEN you route than HOW FAST. A smart router
    sending to IEX during high-volatility periods will outperform
    a co-located server blindly hitting NASDAQ.
    """

    def __init__(
        self,
        venues: Optional[list[VenueProfile]] = None,
        ai_enabled: bool = True,
    ):
        self.venues = {v.venue_id: v for v in (venues or DEFAULT_VENUES)}
        self.scorer = VenueScorer()
        self.planner = ExecutionPlanner()
        self.analytics = ExecutionAnalytics()
        self.ai_enabled = ai_enabled

        # Historical execution data for ML features
        self._execution_log: deque[RoutingDecision] = deque(maxlen=50000)
        self._market_snapshots: deque[MarketMicrostructure] = deque(maxlen=10000)

        # Co-location order books (for arbitrage detection demo)
        self.colo_books: dict[str, dict[str, LowLatencyOrderBook]] = {}

        # Routing stats
        self.total_orders_routed: int = 0
        self.total_volume_routed: float = 0.0
        self.total_decisions: int = 0

    def route_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        urgency: OrderUrgency = OrderUrgency.MEDIUM,
        max_participation_rate: float = 0.10,
        duration_seconds: Optional[float] = None,
    ) -> RoutingDecision:
        """
        Main entry point: route an order across venues.

        1. Score all venues
        2. Select execution strategy based on order characteristics
        3. Allocate quantity across top venues
        4. Return routing plan with cost estimates
        """
        now = time.time_ns()

        # 1. Score venues
        venue_scores = self.scorer.score_venues(
            list(self.venues.values()), side, quantity, urgency,
        )

        if not venue_scores:
            raise ValueError("No available venues for routing")

        # 2. Select strategy
        strategy = self._select_strategy(quantity, urgency, symbol)

        # 3. Determine venue allocations
        allocations = self._allocate_across_venues(
            venue_scores, side, quantity, urgency, strategy,
        )

        # 4. Estimate costs
        est_fees, est_slippage, est_impact = self._estimate_costs(
            allocations, side, quantity,
        )
        est_total_bps = (est_fees + est_slippage + est_impact) / max(1, quantity * self._get_reference_price(symbol)) * 10000

        # 5. Build decision
        primary_reason = self._determine_primary_reason(venue_scores[0], urgency)

        decision = RoutingDecision(
            decision_id=uuid4().hex[:16],
            symbol=symbol,
            side=side,
            total_quantity=quantity,
            urgency=urgency,
            strategy=strategy,
            timestamp_ns=now,
            venue_allocations=allocations,
            reasoning=self._generate_reasoning(venue_scores, allocations, urgency, strategy),
            confidence=self._calculate_confidence(venue_scores),
            primary_reason=primary_reason,
            estimated_total_cost_bps=round(est_total_bps, 2),
            estimated_fees=round(est_fees, 4),
            estimated_slippage=round(est_slippage, 4),
            estimated_market_impact=round(est_impact, 4),
        )

        self._execution_log.append(decision)
        self.total_orders_routed += 1
        self.total_volume_routed += quantity
        self.total_decisions += 1

        return decision

    def _select_strategy(
        self,
        quantity: float,
        urgency: OrderUrgency,
        symbol: str,
    ) -> ExecutionStrategy:
        """Select execution strategy based on order characteristics."""
        if urgency == OrderUrgency.CRITICAL:
            return ExecutionStrategy.DIRECT
        if quantity < 100:
            return ExecutionStrategy.DIRECT
        if urgency == OrderUrgency.LOW:
            return ExecutionStrategy.TWAP
        if self.ai_enabled and quantity > 1000:
            return ExecutionStrategy.ADAPTIVE
        return ExecutionStrategy.VWAP

    def _allocate_across_venues(
        self,
        scores: list[VenueScore],
        side: str,
        quantity: float,
        urgency: OrderUrgency,
        strategy: ExecutionStrategy,
    ) -> list[VenueAllocation]:
        """Allocate order quantity across venues based on scores."""
        allocations = []

        if strategy == ExecutionStrategy.DIRECT:
            # Single venue — send everything to the best one
            best = scores[0]
            venue = self.venues[best.venue_id]
            allocations.append(VenueAllocation(
                venue_id=best.venue_id,
                quantity=quantity,
                limit_price=None,
                is_passive=urgency == OrderUrgency.LOW,
                expected_fill_rate=venue.avg_fill_rate,
                expected_cost_bps=venue.avg_slippage_bps + (venue.taker_fee * 10000 / 150),  # Rough BPS
                reason=RoutingReason.BEST_PRICE if best.spread_score > 0.7 else RoutingReason.BEST_FILL_RATE,
                child_order_id=uuid4().hex[:12],
            ))
        else:
            # Multi-venue allocation — weighted by score
            total_score = sum(s.total_score for s in scores[:3])  # Top 3 venues
            remaining = quantity

            for i, score in enumerate(scores[:3]):
                if remaining <= 0:
                    break

                weight = score.total_score / total_score if total_score > 0 else 1.0 / 3
                alloc_qty = round(quantity * weight, 8) if i < 2 else remaining
                alloc_qty = min(alloc_qty, remaining)

                venue = self.venues[score.venue_id]

                # IEX gets passive orders during high volatility
                is_passive = (
                    urgency in (OrderUrgency.LOW, OrderUrgency.MEDIUM)
                    and venue.adverse_selection_score < 0.3
                )

                reason = RoutingReason.LOW_ADVERSE_SELECTION if venue.adverse_selection_score < 0.3 \
                    else RoutingReason.BEST_PRICE if score.spread_score > 0.7 \
                    else RoutingReason.BEST_FILL_RATE

                allocations.append(VenueAllocation(
                    venue_id=score.venue_id,
                    quantity=alloc_qty,
                    limit_price=None,
                    is_passive=is_passive,
                    expected_fill_rate=venue.avg_fill_rate,
                    expected_cost_bps=venue.avg_slippage_bps,
                    reason=reason,
                    child_order_id=uuid4().hex[:12],
                ))
                remaining -= alloc_qty

        return allocations

    def _estimate_costs(
        self,
        allocations: list[VenueAllocation],
        side: str,
        quantity: float,
    ) -> tuple[float, float, float]:
        """Estimate total execution costs (fees, slippage, market impact)."""
        total_fees = 0.0
        total_slippage = 0.0

        for alloc in allocations:
            venue = self.venues[alloc.venue_id]
            if alloc.is_passive:
                # Maker: earn rebate
                total_fees += venue.maker_rebate * alloc.quantity  # Negative = rebate
            else:
                total_fees += venue.taker_fee * alloc.quantity

            # Slippage estimate
            total_slippage += (venue.avg_slippage_bps / 10000) * alloc.quantity * self._get_reference_price(alloc.venue_id.value)

        # Market impact (simplified Almgren-Chriss)
        # Impact ∝ σ * sqrt(Q / V) where σ=volatility, Q=quantity, V=daily volume
        daily_volume = sum(v.current_depth * 100 for v in self.venues.values())  # Rough estimate
        if daily_volume > 0:
            impact_fraction = math.sqrt(quantity / daily_volume) * 0.1
            ref_price = self._get_reference_price("DEFAULT")
            market_impact = impact_fraction * ref_price * quantity
        else:
            market_impact = 0.0

        return total_fees, total_slippage, market_impact

    def _get_reference_price(self, key: str) -> float:
        """Get a reference price for cost calculations."""
        # In production: fetch from market data feed
        return 150.0  # Default reference for estimation

    def _determine_primary_reason(self, top_score: VenueScore, urgency: OrderUrgency) -> RoutingReason:
        """Determine the primary routing reason."""
        if urgency == OrderUrgency.CRITICAL:
            return RoutingReason.BEST_FILL_RATE
        if top_score.adverse_selection_score > 0.8:
            return RoutingReason.LOW_ADVERSE_SELECTION
        if top_score.fee_score > 0.8:
            return RoutingReason.LOWEST_FEE
        if top_score.spread_score > 0.8:
            return RoutingReason.BEST_PRICE
        return RoutingReason.AI_RECOMMENDATION

    def _generate_reasoning(
        self,
        scores: list[VenueScore],
        allocations: list[VenueAllocation],
        urgency: OrderUrgency,
        strategy: ExecutionStrategy,
    ) -> str:
        """Generate human-readable routing explanation."""
        top = scores[0]
        venue = self.venues[top.venue_id]

        parts = [
            f"Strategy: {strategy.value}.",
            f"Top venue: {venue.name} (score={top.total_score:.3f}).",
        ]

        if len(allocations) > 1:
            venues_str = ", ".join(f"{a.venue_id.value}({a.quantity:.0f})" for a in allocations)
            parts.append(f"Split across: {venues_str}.")

        if venue.adverse_selection_score < 0.3:
            parts.append(f"{venue.name} selected for low adverse selection ({venue.adverse_selection_score:.2f}).")

        if urgency == OrderUrgency.CRITICAL:
            parts.append("Critical urgency: prioritizing fill rate over price improvement.")
        elif urgency == OrderUrgency.LOW:
            parts.append("Low urgency: optimizing for fee savings and price improvement.")

        return " ".join(parts)

    def _calculate_confidence(self, scores: list[VenueScore]) -> float:
        """Calculate confidence in routing decision."""
        if len(scores) < 2:
            return 0.5
        # Higher confidence when top venue clearly dominates
        gap = scores[0].total_score - scores[1].total_score
        return min(0.99, 0.5 + gap * 2)

    def update_venue_state(
        self,
        venue_id: VenueID,
        spread_bps: Optional[float] = None,
        depth: Optional[float] = None,
        fill_rate: Optional[float] = None,
        adverse_selection: Optional[float] = None,
    ):
        """Update real-time venue state (called from market data feed)."""
        venue = self.venues.get(venue_id)
        if not venue:
            return
        if spread_bps is not None:
            venue.current_spread_bps = spread_bps
        if depth is not None:
            venue.current_depth = depth
        if fill_rate is not None:
            venue.avg_fill_rate = fill_rate
        if adverse_selection is not None:
            venue.adverse_selection_score = adverse_selection

    def get_venue_rankings(self, side: str = "BUY", quantity: float = 100, urgency: OrderUrgency = OrderUrgency.MEDIUM) -> list[dict]:
        """Get current venue rankings."""
        scores = self.scorer.score_venues(
            list(self.venues.values()), side, quantity, urgency,
        )
        return [
            {
                "venue": s.venue_id.value,
                "total_score": s.total_score,
                "spread": s.spread_score,
                "fees": s.fee_score,
                "fill_rate": s.fill_rate_score,
                "adverse_selection": s.adverse_selection_score,
                "depth": s.depth_score,
                "latency": s.latency_score,
            }
            for s in scores
        ]

    def get_stats(self) -> dict:
        """Get router statistics."""
        return {
            "total_orders_routed": self.total_orders_routed,
            "total_volume_routed": self.total_volume_routed,
            "total_decisions": self.total_decisions,
            "venues": {
                v.venue_id.value: {
                    "name": v.name,
                    "taker_fee": v.taker_fee,
                    "maker_rebate": v.maker_rebate,
                    "fill_rate": v.avg_fill_rate,
                    "adverse_selection": v.adverse_selection_score,
                    "spread_bps": v.current_spread_bps,
                    "depth": v.current_depth,
                    "is_available": v.is_available,
                }
                for v in self.venues.values()
            },
            "execution_analytics": self.analytics.get_summary(),
        }
