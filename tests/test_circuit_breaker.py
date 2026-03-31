"""Tests for Risk Management & Circuit Breakers."""

from __future__ import annotations

import time

import pytest

from exchange.circuit_breaker import (
    CircuitBreakerController,
    CircuitBreakerLevel,
    KillSwitchManager,
    KillSwitchStatus,
    LULDMonitor,
    LULDState,
    PreTradeRiskEngine,
    PreTradeRiskLimits,
)


class TestLULDMonitor:
    def test_price_within_bands(self):
        monitor = LULDMonitor()
        monitor.initialize_band("BTC-USD", 50000.0)

        allowed, state = monitor.check_price("BTC-USD", 50500.0)
        assert allowed is True
        assert state == LULDState.NORMAL

    def test_price_outside_bands_triggers_limit_state(self):
        monitor = LULDMonitor()
        monitor.initialize_band("BTC-USD", 50000.0)  # 10% band = 45000-55000

        allowed, state = monitor.check_price("BTC-USD", 60000.0)  # 20% above
        assert allowed is False
        assert state == LULDState.LIMIT_STATE

    def test_tier1_narrower_bands(self):
        monitor = LULDMonitor(tier1_symbols={"AAPL"})
        monitor.initialize_band("AAPL", 175.0)  # 5% band = 166.25-183.75

        # Just inside
        allowed, _ = monitor.check_price("AAPL", 183.0)
        assert allowed is True

        # Just outside
        allowed, state = monitor.check_price("AAPL", 185.0)
        assert allowed is False
        assert state == LULDState.LIMIT_STATE

    def test_penny_stock_wider_bands(self):
        monitor = LULDMonitor()
        monitor.initialize_band("PENNY", 0.50)  # 75% band = 0.125-0.875

        allowed, _ = monitor.check_price("PENNY", 0.80)
        assert allowed is True

    def test_price_recovery_resets_limit_state(self):
        monitor = LULDMonitor()
        monitor.initialize_band("BTC-USD", 50000.0)

        # Trigger limit state
        monitor.check_price("BTC-USD", 60000.0)
        assert monitor.bands["BTC-USD"].state == LULDState.LIMIT_STATE

        # Price comes back
        monitor.check_price("BTC-USD", 50000.0)
        assert monitor.bands["BTC-USD"].state == LULDState.NORMAL

    def test_resume_trading(self):
        monitor = LULDMonitor()
        monitor.initialize_band("BTC-USD", 50000.0)

        # Force into pause
        monitor.bands["BTC-USD"].state = LULDState.TRADING_PAUSE
        allowed, _ = monitor.check_price("BTC-USD", 50000.0)
        assert allowed is False

        # Resume
        monitor.resume_trading("BTC-USD", 48000.0)
        allowed, _ = monitor.check_price("BTC-USD", 48500.0)
        assert allowed is True

    def test_get_band(self):
        monitor = LULDMonitor()
        monitor.initialize_band("BTC-USD", 50000.0)

        band = monitor.get_band("BTC-USD")
        assert band is not None
        assert band["symbol"] == "BTC-USD"
        assert band["upper_band"] > band["reference_price"]
        assert band["lower_band"] < band["reference_price"]

    def test_unknown_symbol_allowed(self):
        monitor = LULDMonitor()
        allowed, _ = monitor.check_price("UNKNOWN", 100.0)
        assert allowed is True


class TestCircuitBreakerController:
    def test_no_trigger_within_threshold(self):
        cb = CircuitBreakerController(reference_price=1000.0)

        level = cb.update_price(950.0)  # 5% decline
        assert level is None
        assert cb.state.halted is False

    def test_level_1_trigger(self):
        cb = CircuitBreakerController(reference_price=1000.0)

        level = cb.update_price(920.0)  # 8% decline
        assert level == CircuitBreakerLevel.LEVEL_1
        assert cb.state.halted is True
        assert cb.state.triggers_today == 1

    def test_level_2_trigger(self):
        cb = CircuitBreakerController(reference_price=1000.0)

        # Trigger L1 first
        cb.update_price(920.0)
        cb.force_resume()

        # Trigger L2
        level = cb.update_price(860.0)  # 14% decline
        assert level == CircuitBreakerLevel.LEVEL_2
        assert cb.state.triggers_today == 2

    def test_level_3_closes_market(self):
        cb = CircuitBreakerController(reference_price=1000.0)

        # L1
        cb.update_price(920.0)
        cb.force_resume()
        # L2
        cb.update_price(860.0)
        cb.force_resume()
        # L3
        level = cb.update_price(790.0)  # 21% decline
        assert level == CircuitBreakerLevel.LEVEL_3
        assert cb.state.halt_duration_seconds == 86400

    def test_halt_callback(self):
        cb = CircuitBreakerController(reference_price=1000.0)
        triggered = []
        cb.on_halt(lambda level, decline, duration: triggered.append(level))

        cb.update_price(920.0)
        assert len(triggered) == 1
        assert triggered[0] == CircuitBreakerLevel.LEVEL_1

    def test_no_trigger_during_halt(self):
        cb = CircuitBreakerController(reference_price=1000.0)
        cb.update_price(920.0)  # triggers L1, now halted

        # Further decline during halt should not re-trigger
        level = cb.update_price(800.0)
        assert level is None

    def test_reset_daily(self):
        cb = CircuitBreakerController(reference_price=1000.0)
        cb.update_price(920.0)

        cb.reset_daily(950.0)
        assert cb.state.halted is False
        assert cb.state.triggers_today == 0
        assert cb.state.reference_price == 950.0

    def test_get_state(self):
        cb = CircuitBreakerController(reference_price=1000.0)
        state = cb.get_state()
        assert state["current_level"] == "NONE"
        assert state["reference_price"] == 1000.0


class TestKillSwitchManager:
    def test_active_by_default(self):
        ks = KillSwitchManager()
        ks.register_participant("firm_a")
        assert ks.is_active("firm_a") is True

    def test_trigger_blocks_trading(self):
        ks = KillSwitchManager()
        ks.register_participant("firm_a")

        ks.trigger("firm_a", "Runaway algorithm detected")
        assert ks.is_active("firm_a") is False

    def test_trigger_callback(self):
        ks = KillSwitchManager()
        ks.register_participant("firm_a")
        triggered = []
        ks.on_trigger(lambda pid, reason: triggered.append(pid))

        ks.trigger("firm_a", "test")
        assert triggered == ["firm_a"]

    def test_reset(self):
        ks = KillSwitchManager()
        ks.register_participant("firm_a")
        ks.trigger("firm_a", "test")

        assert ks.is_active("firm_a") is False
        ks.reset("firm_a")
        assert ks.is_active("firm_a") is True

    def test_get_triggered(self):
        ks = KillSwitchManager()
        ks.register_participant("a")
        ks.register_participant("b")
        ks.trigger("a", "rogue algo")

        triggered = ks.get_triggered()
        assert len(triggered) == 1
        assert triggered[0]["participant_id"] == "a"


class TestPreTradeRiskEngine:
    def _setup_engine(self) -> PreTradeRiskEngine:
        engine = PreTradeRiskEngine()
        engine.set_limits("trader1", PreTradeRiskLimits(
            max_position=1000.0,
            max_order_size=100.0,
            buying_power=500_000.0,
            price_collar_pct=0.10,
            max_orders_per_second=50,
            max_notional_per_order=1_000_000.0,
        ))
        engine.update_price("BTC-USD", 50000.0)
        return engine

    def test_approved_order(self):
        engine = self._setup_engine()
        result = engine.check_order("trader1", "BTC-USD", 1.0, 50000.0)
        assert result.allowed is True
        assert result.reason == "APPROVED"
        assert result.latency_ns > 0

    def test_reject_order_size(self):
        engine = self._setup_engine()
        result = engine.check_order("trader1", "BTC-USD", 500.0, 50000.0)
        assert result.allowed is False
        assert "ORDER_SIZE" in result.reason

    def test_reject_price_collar(self):
        engine = self._setup_engine()
        result = engine.check_order("trader1", "BTC-USD", 1.0, 60000.0)  # 20% deviation
        assert result.allowed is False
        assert "PRICE_COLLAR" in result.reason

    def test_reject_position_limit(self):
        engine = self._setup_engine()
        engine.update_position("trader1", "BTC-USD", 999.0)  # almost at limit
        result = engine.check_order("trader1", "BTC-USD", 10.0, 50000.0)
        assert result.allowed is False
        assert "POSITION_LIMIT" in result.reason

    def test_reject_buying_power(self):
        engine = self._setup_engine()
        # 20 BTC × $50000 = $1M > $500K buying power
        result = engine.check_order("trader1", "BTC-USD", 20.0, 50000.0)
        assert result.allowed is False
        assert "BUYING_POWER" in result.reason

    def test_kill_switch_blocks_all(self):
        engine = self._setup_engine()
        engine.kill_switch.trigger("trader1", "test")
        result = engine.check_order("trader1", "BTC-USD", 1.0, 50000.0)
        assert result.allowed is False
        assert "KILL_SWITCH" in result.reason

    def test_no_limits_configured(self):
        engine = PreTradeRiskEngine()
        result = engine.check_order("unknown", "BTC-USD", 1.0, 50000.0)
        assert result.allowed is False

    def test_stats_tracking(self):
        engine = self._setup_engine()
        engine.check_order("trader1", "BTC-USD", 1.0, 50000.0)
        engine.check_order("trader1", "BTC-USD", 500.0, 50000.0)  # rejected

        stats = engine.get_stats()
        assert stats["total_orders_checked"] >= 2
        assert stats["total_risk_rejections"] >= 1
        assert stats["avg_check_latency_ns"] > 0
