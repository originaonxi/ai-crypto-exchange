"""
Tests for Smart Order Router, Execution Planner, and Co-Location Simulator.

Covers:
- Venue scoring and ranking
- Order routing across venues
- TWAP/VWAP/Adaptive execution planning
- Execution analytics (Implementation Shortfall)
- Low-latency order book operations
- Cross-venue arbitrage detection
- Co-location latency simulation
"""

import time
import pytest

from exchange.smart_order_router import (
    ColoOrderBookLevel,
    ExecutionAnalytics,
    ExecutionPlanner,
    ExecutionStrategy,
    LowLatencyOrderBook,
    OrderUrgency,
    RoutingReason,
    SmartOrderRouter,
    VenueID,
    VenueProfile,
    VenueScorer,
    MarketMicrostructure,
)
from exchange.colocation import (
    ColocationSimulator,
    LatencyProfile,
    NetworkMedium,
)


# ===================================================================
# Venue Scorer Tests
# ===================================================================

class TestVenueScorer:
    def test_score_venues_returns_sorted(self):
        scorer = VenueScorer()
        venues = [
            VenueProfile(VenueID.NYSE, "NYSE", 0.003, -0.002, avg_fill_rate=0.9),
            VenueProfile(VenueID.IEX, "IEX", 0.0009, 0.0, avg_fill_rate=0.75, adverse_selection_score=0.1),
            VenueProfile(VenueID.NASDAQ, "NASDAQ", 0.003, -0.002, avg_fill_rate=0.95),
        ]
        scores = scorer.score_venues(venues, "BUY", 100, OrderUrgency.MEDIUM)
        assert len(scores) == 3
        # Should be sorted descending by total_score
        assert scores[0].total_score >= scores[1].total_score >= scores[2].total_score

    def test_critical_urgency_favors_fill_rate(self):
        scorer = VenueScorer()
        # One venue with high fill rate, another with low fees
        high_fill = VenueProfile(VenueID.NYSE, "NYSE", 0.005, -0.001, avg_fill_rate=0.99, current_depth=50000)
        low_fee = VenueProfile(VenueID.IEX, "IEX", 0.0001, 0.0, avg_fill_rate=0.60, current_depth=5000)
        scores = scorer.score_venues([high_fill, low_fee], "BUY", 100, OrderUrgency.CRITICAL)
        assert scores[0].venue_id == VenueID.NYSE

    def test_low_urgency_favors_fees(self):
        scorer = VenueScorer()
        expensive = VenueProfile(VenueID.NYSE, "NYSE", 0.005, -0.001, adverse_selection_score=0.9, current_spread_bps=3.0)
        cheap = VenueProfile(VenueID.IEX, "IEX", 0.0001, 0.0, adverse_selection_score=0.1, current_spread_bps=0.5)
        scores = scorer.score_venues([expensive, cheap], "BUY", 100, OrderUrgency.LOW)
        assert scores[0].venue_id == VenueID.IEX

    def test_empty_venues_returns_empty(self):
        scorer = VenueScorer()
        assert scorer.score_venues([], "BUY", 100, OrderUrgency.MEDIUM) == []

    def test_unavailable_venues_excluded(self):
        scorer = VenueScorer()
        available = VenueProfile(VenueID.NYSE, "NYSE", 0.003, -0.002)
        unavailable = VenueProfile(VenueID.NASDAQ, "NASDAQ", 0.003, -0.002, is_available=False)
        scores = scorer.score_venues([available, unavailable], "BUY", 100, OrderUrgency.MEDIUM)
        assert len(scores) == 1
        assert scores[0].venue_id == VenueID.NYSE

    def test_scores_have_all_components(self):
        scorer = VenueScorer()
        venues = [VenueProfile(VenueID.NYSE, "NYSE", 0.003, -0.002)]
        scores = scorer.score_venues(venues, "BUY", 100, OrderUrgency.MEDIUM)
        s = scores[0]
        assert 0 <= s.total_score <= 1
        assert 0 <= s.spread_score <= 1
        assert 0 <= s.fee_score <= 1


# ===================================================================
# Smart Order Router Tests
# ===================================================================

class TestSmartOrderRouter:
    def test_route_small_order_uses_direct(self):
        router = SmartOrderRouter()
        decision = router.route_order("AAPL", "BUY", 50, OrderUrgency.MEDIUM)
        assert decision.strategy == ExecutionStrategy.DIRECT
        assert len(decision.venue_allocations) == 1
        assert decision.total_quantity == 50

    def test_route_critical_uses_direct(self):
        router = SmartOrderRouter()
        decision = router.route_order("AAPL", "BUY", 5000, OrderUrgency.CRITICAL)
        assert decision.strategy == ExecutionStrategy.DIRECT

    def test_route_large_order_splits_venues(self):
        router = SmartOrderRouter()
        decision = router.route_order("AAPL", "BUY", 500, OrderUrgency.MEDIUM)
        # Should use VWAP for medium-sized orders
        assert decision.strategy in (ExecutionStrategy.VWAP, ExecutionStrategy.ADAPTIVE)

    def test_route_low_urgency_uses_twap(self):
        router = SmartOrderRouter()
        decision = router.route_order("AAPL", "BUY", 500, OrderUrgency.LOW)
        assert decision.strategy == ExecutionStrategy.TWAP

    def test_routing_decision_has_reasoning(self):
        router = SmartOrderRouter()
        decision = router.route_order("AAPL", "BUY", 100, OrderUrgency.MEDIUM)
        assert len(decision.reasoning) > 0
        assert decision.confidence > 0

    def test_routing_decision_has_cost_estimates(self):
        router = SmartOrderRouter()
        decision = router.route_order("AAPL", "BUY", 100, OrderUrgency.MEDIUM)
        assert decision.estimated_fees is not None
        assert isinstance(decision.estimated_total_cost_bps, float)

    def test_allocations_sum_to_total(self):
        router = SmartOrderRouter()
        decision = router.route_order("AAPL", "BUY", 1000, OrderUrgency.MEDIUM)
        total_allocated = sum(a.quantity for a in decision.venue_allocations)
        assert abs(total_allocated - 1000) < 0.01

    def test_update_venue_state(self):
        router = SmartOrderRouter()
        router.update_venue_state(VenueID.IEX, spread_bps=5.0, depth=1000)
        venue = router.venues[VenueID.IEX]
        assert venue.current_spread_bps == 5.0
        assert venue.current_depth == 1000

    def test_venue_rankings(self):
        router = SmartOrderRouter()
        rankings = router.get_venue_rankings()
        assert len(rankings) == 5
        assert all("total_score" in r for r in rankings)

    def test_stats(self):
        router = SmartOrderRouter()
        router.route_order("AAPL", "BUY", 100, OrderUrgency.MEDIUM)
        stats = router.get_stats()
        assert stats["total_orders_routed"] == 1
        assert stats["total_volume_routed"] == 100
        assert len(stats["venues"]) == 5

    def test_no_available_venues_raises(self):
        venues = [VenueProfile(VenueID.NYSE, "NYSE", 0.003, -0.002, is_available=False)]
        router = SmartOrderRouter(venues=venues)
        with pytest.raises(ValueError, match="No available venues"):
            router.route_order("AAPL", "BUY", 100)


# ===================================================================
# Execution Planner Tests
# ===================================================================

class TestExecutionPlanner:
    def test_twap_equal_slices(self):
        plan = ExecutionPlanner.plan_twap(1000, 300, 30)
        assert len(plan) == 10
        assert all(qty == 100 for _, qty in plan)
        assert plan[0][0] == 0
        assert plan[-1][0] == 270

    def test_twap_single_slice_short_duration(self):
        plan = ExecutionPlanner.plan_twap(100, 10, 30)
        assert len(plan) == 1
        assert plan[0][1] == 100

    def test_vwap_follows_volume_profile(self):
        profile = [0.3, 0.1, 0.1, 0.2, 0.3]  # U-shaped (open/close heavy)
        plan = ExecutionPlanner.plan_vwap(1000, profile, 300)
        assert len(plan) == 5
        # First and last should be largest
        assert plan[0][1] > plan[1][1]
        assert plan[-1][1] > plan[2][1]
        # Total should sum to ~1000
        total = sum(qty for _, qty in plan)
        assert abs(total - 1000) < 1

    def test_vwap_empty_profile(self):
        plan = ExecutionPlanner.plan_vwap(1000, [], 300)
        assert len(plan) == 1
        assert plan[0][1] == 1000

    def test_adaptive_critical_fewer_slices(self):
        micro = MarketMicrostructure(
            symbol="AAPL", bid=150.0, ask=150.01, spread_bps=0.67,
            mid_price=150.005, bid_depth=5000, ask_depth=5000,
            imbalance_ratio=0.5, recent_trades_per_sec=100,
            volatility_1min=0.001, volatility_5min=0.002,
            time_of_day_bucket="midday", volume_participation_rate=0.05,
        )
        critical = ExecutionPlanner.plan_adaptive(1000, micro, OrderUrgency.CRITICAL)
        low = ExecutionPlanner.plan_adaptive(1000, micro, OrderUrgency.LOW)
        assert len(critical) <= len(low)

    def test_adaptive_total_quantity_matches(self):
        micro = MarketMicrostructure(
            symbol="AAPL", bid=150.0, ask=150.01, spread_bps=0.67,
            mid_price=150.005, bid_depth=5000, ask_depth=5000,
            imbalance_ratio=0.5, recent_trades_per_sec=100,
            volatility_1min=0.001, volatility_5min=0.002,
            time_of_day_bucket="midday", volume_participation_rate=0.05,
        )
        plan = ExecutionPlanner.plan_adaptive(1000, micro, OrderUrgency.MEDIUM)
        total = sum(qty for _, qty in plan)
        assert abs(total - 1000) < 1


# ===================================================================
# Execution Analytics Tests
# ===================================================================

class TestExecutionAnalytics:
    def test_implementation_shortfall_buy(self):
        analytics = ExecutionAnalytics()
        result = analytics.calculate_implementation_shortfall(
            arrival_price=150.00,
            avg_fill_price=150.05,
            quantity=1000,
            side="BUY",
            fees=3.00,
        )
        # Bought higher than arrival: positive IS (cost)
        assert result["implementation_shortfall"] > 0
        assert result["price_impact"] == 50.0  # (150.05 - 150.00) * 1000
        assert result["fees"] == 3.0

    def test_implementation_shortfall_sell(self):
        analytics = ExecutionAnalytics()
        result = analytics.calculate_implementation_shortfall(
            arrival_price=150.00,
            avg_fill_price=149.95,
            quantity=1000,
            side="SELL",
            fees=3.00,
        )
        # Sold lower than arrival: positive IS (cost)
        assert result["implementation_shortfall"] > 0
        assert result["price_impact"] == 50.0

    def test_zero_slippage(self):
        analytics = ExecutionAnalytics()
        result = analytics.calculate_implementation_shortfall(
            arrival_price=150.00,
            avg_fill_price=150.00,
            quantity=1000,
            side="BUY",
            fees=0.0,
        )
        assert result["implementation_shortfall"] == 0

    def test_summary_empty(self):
        analytics = ExecutionAnalytics()
        summary = analytics.get_summary()
        assert summary["total_executions"] == 0


# ===================================================================
# Low-Latency Order Book Tests
# ===================================================================

class TestLowLatencyOrderBook:
    def test_basic_bid_ask(self):
        book = LowLatencyOrderBook("NYSE")
        book.update_level("B", 150.00, 1000)
        book.update_level("A", 150.01, 500)
        assert book.best_bid == 150.00
        assert book.best_ask == 150.01
        assert book.spread == pytest.approx(0.01, abs=0.001)

    def test_mid_price(self):
        book = LowLatencyOrderBook("NYSE")
        book.update_level("B", 150.00, 1000)
        book.update_level("A", 150.02, 500)
        assert book.mid_price == pytest.approx(150.01, abs=0.001)

    def test_update_best_bid(self):
        book = LowLatencyOrderBook("NYSE")
        book.update_level("B", 150.00, 1000)
        book.update_level("B", 150.05, 500)
        assert book.best_bid == 150.05

    def test_remove_level(self):
        book = LowLatencyOrderBook("NYSE")
        book.update_level("B", 150.00, 1000)
        book.update_level("B", 150.05, 500)
        book.update_level("B", 150.05, 0)  # Remove
        assert book.best_bid == 150.00

    def test_get_depth(self):
        book = LowLatencyOrderBook("NYSE")
        book.update_level("A", 150.01, 100)
        book.update_level("A", 150.02, 200)
        book.update_level("A", 150.03, 300)
        depth = book.get_depth("A", levels=2)
        assert len(depth) == 2
        assert depth[0].price == 150.01  # Best ask first

    def test_arbitrage_detected(self):
        book_nyse = LowLatencyOrderBook("NYSE")
        book_nasdaq = LowLatencyOrderBook("NASDAQ")
        # NYSE ask is lower than NASDAQ bid -> arbitrage!
        book_nyse.update_level("B", 149.90, 100)
        book_nyse.update_level("A", 150.00, 100)
        book_nasdaq.update_level("B", 150.05, 100)
        book_nasdaq.update_level("A", 150.10, 100)
        arb = book_nyse.check_arbitrage(book_nasdaq, fee_per_share=0.001)
        assert arb is not None
        assert arb["profit_per_share"] > 0
        assert arb["buy_venue"] == "NYSE"
        assert arb["sell_venue"] == "NASDAQ"

    def test_no_arbitrage(self):
        book_nyse = LowLatencyOrderBook("NYSE")
        book_nasdaq = LowLatencyOrderBook("NASDAQ")
        book_nyse.update_level("B", 149.99, 100)
        book_nyse.update_level("A", 150.01, 100)
        book_nasdaq.update_level("B", 149.99, 100)
        book_nasdaq.update_level("A", 150.01, 100)
        assert book_nyse.check_arbitrage(book_nasdaq) is None

    def test_empty_book(self):
        book = LowLatencyOrderBook("NYSE")
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.spread is None

    def test_stats(self):
        book = LowLatencyOrderBook("NYSE")
        book.update_level("B", 150.00, 1000)
        book.update_level("A", 150.01, 500)
        stats = book.stats
        assert stats["venue"] == "NYSE"
        assert stats["total_updates"] == 2


# ===================================================================
# Co-Location Simulator Tests
# ===================================================================

class TestColocationSimulator:
    def test_add_venue(self):
        sim = ColocationSimulator()
        book = sim.add_venue("NYSE")
        assert "NYSE" in sim.venues
        assert book.venue == "NYSE"

    def test_add_rack(self):
        sim = ColocationSimulator()
        sim.add_venue("NYSE")
        rack = sim.add_rack("NYSE", "Row A, Rack 3", 5.0)
        assert rack.cable_latency_ns == 25.0  # 5m * 5ns/m

    def test_latency_fiber(self):
        profile = LatencyProfile.calculate("NYC", "Chicago", NetworkMedium.FIBER, 1200)
        # Fiber: 1200km / 200,000 km/s = 6.0ms = 6000μs + processing
        assert profile.one_way_latency_us > 5000
        assert profile.one_way_latency_us < 7000

    def test_latency_microwave(self):
        profile = LatencyProfile.calculate("NYC", "Chicago", NetworkMedium.MICROWAVE, 1200)
        # Microwave: 1200km / 279,000 km/s ≈ 4.3ms + processing
        assert profile.one_way_latency_us < 5000

    def test_microwave_faster_than_fiber(self):
        fiber = LatencyProfile.calculate("NYC", "Chicago", NetworkMedium.FIBER, 1200)
        microwave = LatencyProfile.calculate("NYC", "Chicago", NetworkMedium.MICROWAVE, 1200)
        assert microwave.one_way_latency_us < fiber.one_way_latency_us

    def test_colocated_very_fast(self):
        profile = LatencyProfile.calculate("Rack1", "Engine", NetworkMedium.COLOCATED, 0)
        assert profile.one_way_latency_us < 2  # Sub-2μs

    def test_scan_arbitrage(self):
        sim = ColocationSimulator()
        sim.add_venue("NYSE")
        sim.add_venue("NASDAQ")
        # Create arbitrage condition
        sim.simulate_market_update("NYSE", "B", 149.90, 100)
        sim.simulate_market_update("NYSE", "A", 150.00, 100)
        sim.simulate_market_update("NASDAQ", "B", 150.05, 100)
        sim.simulate_market_update("NASDAQ", "A", 150.10, 100)
        arbs = sim.scan_arbitrage(fee_per_share=0.001)
        assert len(arbs) > 0
        assert arbs[0]["profit_per_share"] > 0

    def test_cross_venue_spread(self):
        sim = ColocationSimulator()
        sim.add_venue("NYSE")
        sim.add_venue("NASDAQ")
        sim.simulate_market_update("NYSE", "B", 150.00, 100)
        sim.simulate_market_update("NASDAQ", "A", 150.02, 100)
        result = sim.get_cross_venue_spread()
        assert result["best_bid"] == 150.00
        assert result["best_bid_venue"] == "NYSE"
        assert result["best_ask"] == 150.02
        assert result["best_ask_venue"] == "NASDAQ"

    def test_latency_map(self):
        sim = ColocationSimulator()
        sim.set_latency("NYC", "Chicago", NetworkMedium.FIBER, 1200)
        sim.set_latency("NYC", "London", NetworkMedium.MICROWAVE, 5500)
        lat_map = sim.get_latency_map()
        assert len(lat_map) == 2

    def test_stats(self):
        sim = ColocationSimulator()
        sim.add_venue("NYSE")
        sim.add_venue("NASDAQ")
        sim.add_rack("NYSE", "A3", 5.0)
        stats = sim.get_stats()
        assert len(stats["venues"]) == 2
        assert len(stats["racks"]) == 1
