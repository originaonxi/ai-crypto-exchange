"""
Co-Location & Latency Arbitrage — Educational Implementation.

Demonstrates the core concepts of HFT co-location:
- Pre-allocated array-based order books (FPGA-style)
- Cross-venue arbitrage detection
- Latency measurement and analysis
- Network topology simulation

This module is referenced by CO_LOCATION.md and provides the working
code examples discussed in that lesson.

References:
- "Flash Boys" — Michael Lewis
- NYSE Co-location Technical Specifications
- "The High-Frequency Trading Arms Race" — American Economic Review
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from exchange.smart_order_router import ColoOrderBookLevel, LowLatencyOrderBook


class NetworkMedium(str, Enum):
    FIBER = "FIBER"
    MICROWAVE = "MICROWAVE"
    COPPER = "COPPER"
    COLOCATED = "COLOCATED"


@dataclass
class LatencyProfile:
    """Models physical latency between two points."""
    source: str
    destination: str
    medium: NetworkMedium
    distance_km: float
    one_way_latency_us: float  # microseconds
    round_trip_us: float
    jitter_us: float  # standard deviation of latency

    @staticmethod
    def calculate(
        source: str,
        destination: str,
        medium: NetworkMedium,
        distance_km: float,
    ) -> LatencyProfile:
        """Calculate latency from physics."""
        # Speed of light: 299,792 km/s
        c = 299_792.0  # km/s

        if medium == NetworkMedium.FIBER:
            # Light in fiber: ~2/3 of vacuum speed
            speed = c * 0.67  # ~200,000 km/s
        elif medium == NetworkMedium.MICROWAVE:
            # Microwave in air: ~93% of vacuum speed
            speed = c * 0.93  # ~279,000 km/s
        elif medium == NetworkMedium.COPPER:
            # Electrical signal in copper: ~77% of vacuum speed
            speed = c * 0.77  # ~231,000 km/s
        else:  # COLOCATED
            # Same data center: ~10 meters of cable
            speed = c * 0.67
            distance_km = 0.01  # 10 meters

        one_way_us = (distance_km / speed) * 1_000_000  # Convert to microseconds

        # Add processing overhead
        if medium == NetworkMedium.COLOCATED:
            processing_us = 0.5  # FPGA: ~500ns
        elif medium == NetworkMedium.FIBER:
            processing_us = 5.0  # Switch + NIC: ~5μs
        else:
            processing_us = 2.0  # Microwave: less processing

        one_way_total = one_way_us + processing_us
        jitter = one_way_us * 0.02  # 2% jitter

        return LatencyProfile(
            source=source,
            destination=destination,
            medium=medium,
            distance_km=distance_km,
            one_way_latency_us=round(one_way_total, 2),
            round_trip_us=round(one_way_total * 2, 2),
            jitter_us=round(jitter, 2),
        )


@dataclass
class ArbitrageOpportunity:
    """A detected cross-venue arbitrage opportunity."""
    opportunity_id: str
    buy_venue: str
    buy_price: float
    sell_venue: str
    sell_price: float
    quantity: int
    profit_per_share: float
    total_profit: float
    detection_latency_ns: int
    execution_window_us: float  # How long the opportunity is expected to last
    timestamp_ns: int


@dataclass
class ColoRackStats:
    """Statistics for a co-located trading rack."""
    venue: str
    rack_position: str  # e.g., "Row A, Rack 3"
    distance_to_engine_m: float
    cable_latency_ns: float  # 5ns per meter
    total_orders_sent: int = 0
    total_fills: int = 0
    total_arbitrage_detected: int = 0
    total_arbitrage_profit: float = 0.0
    avg_round_trip_ns: float = 0.0
    _rtt_sum: float = 0.0


class ColocationSimulator:
    """
    Simulates HFT co-location environment for educational purposes.

    Models:
    - Multiple exchange venues with independent order books
    - Physical distance and latency between venues
    - Cross-venue arbitrage detection and execution
    - Latency advantage quantification
    """

    def __init__(self):
        self.venues: dict[str, LowLatencyOrderBook] = {}
        self.latency_profiles: dict[tuple[str, str], LatencyProfile] = {}
        self.rack_stats: dict[str, ColoRackStats] = {}
        self._arbitrage_log: list[ArbitrageOpportunity] = []

    def add_venue(self, venue_name: str, max_price_cents: int = 100000) -> LowLatencyOrderBook:
        """Add a simulated exchange venue."""
        book = LowLatencyOrderBook(venue_name, max_price_cents)
        self.venues[venue_name] = book
        return book

    def add_rack(
        self,
        venue: str,
        rack_position: str,
        distance_to_engine_m: float,
    ) -> ColoRackStats:
        """Register a co-located rack at a venue."""
        cable_latency_ns = distance_to_engine_m * 5.0  # ~5ns per meter in Cat6A
        stats = ColoRackStats(
            venue=venue,
            rack_position=rack_position,
            distance_to_engine_m=distance_to_engine_m,
            cable_latency_ns=cable_latency_ns,
        )
        self.rack_stats[f"{venue}:{rack_position}"] = stats
        return stats

    def set_latency(
        self,
        source: str,
        destination: str,
        medium: NetworkMedium,
        distance_km: float,
    ) -> LatencyProfile:
        """Set the latency profile between two venues."""
        profile = LatencyProfile.calculate(source, destination, medium, distance_km)
        self.latency_profiles[(source, destination)] = profile
        self.latency_profiles[(destination, source)] = profile
        return profile

    def scan_arbitrage(self, fee_per_share: float = 0.001) -> list[dict]:
        """Scan all venue pairs for arbitrage opportunities."""
        opportunities = []
        venue_list = list(self.venues.keys())

        for i in range(len(venue_list)):
            for j in range(i + 1, len(venue_list)):
                v1 = venue_list[i]
                v2 = venue_list[j]
                book1 = self.venues[v1]
                book2 = self.venues[v2]

                arb = book1.check_arbitrage(book2, fee_per_share)
                if arb:
                    opportunities.append(arb)

        return opportunities

    def simulate_market_update(
        self,
        venue: str,
        side: str,
        price: float,
        size: int,
    ):
        """Simulate a market data update at a venue."""
        book = self.venues.get(venue)
        if book:
            book.update_level(side, price, size)

    def get_cross_venue_spread(self) -> dict:
        """Get the spread across all venues (NBBO-like)."""
        best_bid = 0.0
        best_bid_venue = ""
        best_ask = float('inf')
        best_ask_venue = ""

        for name, book in self.venues.items():
            bid = book.best_bid
            ask = book.best_ask

            if bid and bid > best_bid:
                best_bid = bid
                best_bid_venue = name
            if ask and ask < best_ask:
                best_ask = ask
                best_ask_venue = name

        return {
            "best_bid": best_bid if best_bid > 0 else None,
            "best_bid_venue": best_bid_venue,
            "best_ask": best_ask if best_ask < float('inf') else None,
            "best_ask_venue": best_ask_venue,
            "cross_venue_spread": round(best_ask - best_bid, 6) if best_bid > 0 and best_ask < float('inf') else None,
        }

    def get_latency_map(self) -> list[dict]:
        """Get all configured latency profiles."""
        seen = set()
        result = []
        for (src, dst), profile in self.latency_profiles.items():
            key = tuple(sorted([src, dst]))
            if key not in seen:
                seen.add(key)
                result.append({
                    "source": profile.source,
                    "destination": profile.destination,
                    "medium": profile.medium.value,
                    "distance_km": profile.distance_km,
                    "one_way_us": profile.one_way_latency_us,
                    "round_trip_us": profile.round_trip_us,
                    "jitter_us": profile.jitter_us,
                })
        return result

    def get_stats(self) -> dict:
        """Get simulator statistics."""
        return {
            "venues": {name: book.stats for name, book in self.venues.items()},
            "latency_profiles": len(self.latency_profiles) // 2,  # Each pair stored twice
            "racks": {
                name: {
                    "venue": s.venue,
                    "position": s.rack_position,
                    "distance_m": s.distance_to_engine_m,
                    "cable_latency_ns": s.cable_latency_ns,
                    "total_orders": s.total_orders_sent,
                    "total_arbitrage_profit": s.total_arbitrage_profit,
                }
                for name, s in self.rack_stats.items()
            },
            "total_arbitrage_opportunities": len(self._arbitrage_log),
        }
