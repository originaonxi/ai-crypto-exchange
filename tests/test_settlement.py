"""Tests for Settlement & Clearing Architecture."""

from __future__ import annotations

import time

import pytest

from exchange.settlement import (
    FailRecord,
    MarginCalculator,
    MemberAccount,
    MultilateralNettingEngine,
    NettingResult,
    SettlementEngine,
    SettlementInstruction,
    SettlementStatus,
    StockBorrowProgram,
    Trade,
)
from exchange.settlement_ai import FailPrediction, SettlementPredictor


# ---------------------------------------------------------------------------
# Multilateral Netting Engine
# ---------------------------------------------------------------------------

class TestMultilateralNettingEngine:
    def test_single_trade(self):
        engine = MultilateralNettingEngine()
        trade = Trade(
            trade_id="t1", symbol="BTC-USD", buyer_member_id="alice",
            seller_member_id="bob", quantity=10.0, price=50000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        )
        engine.add_trade(trade)
        result = engine.calculate_netting_result("2026-03-31")

        assert result.gross_trades == 1
        assert result.net_instructions == 1
        assert result.instructions[0].from_member == "bob"
        assert result.instructions[0].to_member == "alice"
        assert result.instructions[0].quantity == 10.0

    def test_netting_cancels_opposing_trades(self):
        """Alice buys 100 BTC from Bob, then sells 100 BTC to Bob — should net to zero."""
        engine = MultilateralNettingEngine()
        engine.add_trade(Trade(
            trade_id="t1", symbol="BTC-USD", buyer_member_id="alice",
            seller_member_id="bob", quantity=100.0, price=50000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))
        engine.add_trade(Trade(
            trade_id="t2", symbol="BTC-USD", buyer_member_id="bob",
            seller_member_id="alice", quantity=100.0, price=51000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))
        result = engine.calculate_netting_result("2026-03-31")

        assert result.gross_trades == 2
        assert result.net_instructions == 0
        assert result.netting_efficiency == 1.0

    def test_multilateral_netting_three_members(self):
        """A->B, B->C, C->A circular trades should net efficiently."""
        engine = MultilateralNettingEngine()

        # Alice buys 10 from Bob
        engine.add_trade(Trade(
            trade_id="t1", symbol="ETH-USD", buyer_member_id="alice",
            seller_member_id="bob", quantity=10.0, price=3000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))
        # Bob buys 10 from Charlie
        engine.add_trade(Trade(
            trade_id="t2", symbol="ETH-USD", buyer_member_id="bob",
            seller_member_id="charlie", quantity=10.0, price=3000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))
        # Charlie buys 10 from Alice
        engine.add_trade(Trade(
            trade_id="t3", symbol="ETH-USD", buyer_member_id="charlie",
            seller_member_id="alice", quantity=10.0, price=3000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))

        result = engine.calculate_netting_result("2026-03-31")

        # Perfect circular netting — all positions cancel out
        assert result.gross_trades == 3
        assert result.net_instructions == 0
        assert result.netting_efficiency == 1.0

    def test_partial_netting(self):
        """Alice buys 100, sells 60 — net is +40."""
        engine = MultilateralNettingEngine()
        engine.add_trade(Trade(
            trade_id="t1", symbol="SOL-USD", buyer_member_id="alice",
            seller_member_id="bob", quantity=100.0, price=100.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))
        engine.add_trade(Trade(
            trade_id="t2", symbol="SOL-USD", buyer_member_id="bob",
            seller_member_id="alice", quantity=60.0, price=100.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))

        result = engine.calculate_netting_result("2026-03-31")

        assert result.gross_trades == 2
        assert result.net_instructions == 1
        assert result.instructions[0].quantity == 40.0
        assert result.netting_efficiency == 0.5

    def test_multi_security_netting(self):
        """Netting across multiple securities independently."""
        engine = MultilateralNettingEngine()
        engine.add_trade(Trade(
            trade_id="t1", symbol="BTC-USD", buyer_member_id="alice",
            seller_member_id="bob", quantity=5.0, price=50000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))
        engine.add_trade(Trade(
            trade_id="t2", symbol="ETH-USD", buyer_member_id="bob",
            seller_member_id="alice", quantity=100.0, price=3000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))

        result = engine.calculate_netting_result("2026-03-31")

        assert result.gross_trades == 2
        assert result.net_instructions == 2  # one per security

    def test_cash_flows(self):
        """Net cash obligations calculated correctly."""
        engine = MultilateralNettingEngine()
        engine.add_trade(Trade(
            trade_id="t1", symbol="BTC-USD", buyer_member_id="alice",
            seller_member_id="bob", quantity=1.0, price=50000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        ))

        result = engine.calculate_netting_result("2026-03-31")

        assert result.member_cash_flows["alice"] == -50000.0  # buyer pays
        assert result.member_cash_flows["bob"] == 50000.0  # seller receives

    def test_high_volume_netting_efficiency(self):
        """Simulate 1000 trades among 10 members — expect >80% netting."""
        import random
        random.seed(42)

        engine = MultilateralNettingEngine()
        members = [f"member_{i}" for i in range(10)]

        for i in range(1000):
            buyer = random.choice(members)
            seller = random.choice([m for m in members if m != buyer])
            engine.add_trade(Trade(
                trade_id=f"t{i}", symbol="BTC-USD",
                buyer_member_id=buyer, seller_member_id=seller,
                quantity=random.uniform(1, 100), price=50000.0,
                timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
            ))

        result = engine.calculate_netting_result("2026-03-31")

        assert result.gross_trades == 1000
        assert result.netting_efficiency > 0.8
        assert result.capital_saved > 0


# ---------------------------------------------------------------------------
# Margin Calculator
# ---------------------------------------------------------------------------

class TestMarginCalculator:
    def test_empty_portfolio(self):
        calc = MarginCalculator(num_simulations=100)
        req = calc.calculate_margin("member1", {}, {})
        assert req.amount == 0.0

    def test_single_position_margin(self):
        calc = MarginCalculator(num_simulations=1000)
        calc.update_price("BTC-USD", 50000.0)
        calc.update_price("BTC-USD", 50500.0)
        calc.update_price("BTC-USD", 49800.0)
        calc.update_price("BTC-USD", 50200.0)

        req = calc.calculate_margin(
            "member1",
            {"BTC-USD": 10.0},
            {"BTC-USD": 50000.0},
        )

        assert req.amount > 0
        assert req.var_95 >= 0
        assert req.var_99 >= req.var_95
        assert req.stress_loss > 0
        assert "BTC-USD" in req.components

    def test_margin_increases_with_position_size(self):
        calc = MarginCalculator(num_simulations=1000)
        for p in [100, 101, 99, 102, 98]:
            calc.update_price("ETH-USD", p)

        small = calc.calculate_margin("m1", {"ETH-USD": 1.0}, {"ETH-USD": 100.0})
        large = calc.calculate_margin("m2", {"ETH-USD": 100.0}, {"ETH-USD": 100.0})

        assert large.amount > small.amount

    def test_minimum_margin_floor(self):
        calc = MarginCalculator(num_simulations=100, min_margin_pct=0.05)

        # With very low volatility, margin should still meet the floor
        for p in [100.0, 100.001, 100.002, 100.001]:
            calc.update_price("STABLE", p)

        req = calc.calculate_margin("m1", {"STABLE": 100.0}, {"STABLE": 100.0})
        min_expected = 100.0 * 100.0 * 0.05  # 5% of $10,000 position
        assert req.amount >= min_expected


# ---------------------------------------------------------------------------
# Stock Borrow Program
# ---------------------------------------------------------------------------

class TestStockBorrowProgram:
    def test_successful_borrow(self):
        program = StockBorrowProgram()
        program.register_lendable("lender1", "BTC-USD", 50.0)

        success, borrowed = program.attempt_borrow("BTC-USD", 30.0, "borrower1")
        assert success is True
        assert borrowed == 30.0

    def test_insufficient_borrow(self):
        program = StockBorrowProgram()
        program.register_lendable("lender1", "BTC-USD", 10.0)

        success, borrowed = program.attempt_borrow("BTC-USD", 50.0, "borrower1")
        assert success is False
        assert borrowed == 10.0

    def test_no_self_borrow(self):
        program = StockBorrowProgram()
        program.register_lendable("member1", "BTC-USD", 100.0)

        success, borrowed = program.attempt_borrow("BTC-USD", 50.0, "member1")
        assert success is False
        assert borrowed == 0.0

    def test_multi_lender_borrow(self):
        program = StockBorrowProgram()
        program.register_lendable("lender1", "ETH-USD", 30.0)
        program.register_lendable("lender2", "ETH-USD", 30.0)

        success, borrowed = program.attempt_borrow("ETH-USD", 50.0, "borrower1")
        assert success is True
        assert borrowed == 50.0


# ---------------------------------------------------------------------------
# Settlement Engine (Full Pipeline)
# ---------------------------------------------------------------------------

class TestSettlementEngine:
    def _create_engine_with_members(self) -> SettlementEngine:
        engine = SettlementEngine()
        engine.register_member("alice", "Alice Capital", initial_cash=1_000_000.0)
        engine.register_member("bob", "Bob Securities", initial_cash=1_000_000.0)
        engine.register_member("charlie", "Charlie Trading", initial_cash=1_000_000.0)

        # Deposit securities
        engine.deposit_securities("alice", "BTC-USD", 100.0)
        engine.deposit_securities("bob", "BTC-USD", 100.0)
        engine.deposit_securities("charlie", "BTC-USD", 100.0)
        engine.deposit_securities("alice", "ETH-USD", 1000.0)
        engine.deposit_securities("bob", "ETH-USD", 1000.0)

        return engine

    def test_register_member(self):
        engine = SettlementEngine()
        account = engine.register_member("test", "Test Firm", initial_cash=500_000.0)
        assert account.member_id == "test"
        assert account.cash_balance == 500_000.0
        assert account.is_active is True

    def test_deposit_securities(self):
        engine = SettlementEngine()
        engine.register_member("test", "Test")
        engine.deposit_securities("test", "BTC-USD", 50.0)
        assert engine.members["test"].securities["BTC-USD"] == 50.0

    def test_full_settlement_pipeline(self):
        """Test: ingest → net → settle."""
        engine = self._create_engine_with_members()

        # Alice buys 10 BTC from Bob
        trade = Trade(
            trade_id="t1", symbol="BTC-USD", buyer_member_id="alice",
            seller_member_id="bob", quantity=10.0, price=50000.0,
            timestamp_ns=time.time_ns(), settlement_date="2026-03-31",
        )
        engine.ingest_trade(trade)

        # Run netting
        netting = engine.run_netting_cycle("2026-03-31")
        assert netting.gross_trades == 1
        assert netting.net_instructions == 1

        # Execute settlement
        result = engine.execute_settlement("2026-03-31")
        assert result["settled"] == 1
        assert result["failed"] == 0

        # Verify balances
        assert engine.members["bob"].securities["BTC-USD"] == 90.0  # sold 10
        assert engine.members["alice"].securities["BTC-USD"] == 110.0  # bought 10

    def test_settlement_with_netting(self):
        """Multiple trades that net down."""
        engine = self._create_engine_with_members()

        trades = [
            Trade("t1", "BTC-USD", "alice", "bob", 10.0, 50000.0, time.time_ns(), "2026-03-31"),
            Trade("t2", "BTC-USD", "bob", "alice", 7.0, 50000.0, time.time_ns(), "2026-03-31"),
        ]
        for t in trades:
            engine.ingest_trade(t)

        netting = engine.run_netting_cycle("2026-03-31")
        assert netting.gross_trades == 2
        assert netting.net_instructions == 1
        assert netting.instructions[0].quantity == 3.0  # net transfer

    def test_settlement_fail_securities_short(self):
        """Fail when seller doesn't have enough securities."""
        engine = SettlementEngine()
        engine.register_member("alice", "Alice", initial_cash=1_000_000.0)
        engine.register_member("bob", "Bob", initial_cash=1_000_000.0)
        engine.deposit_securities("bob", "BTC-USD", 5.0)  # only 5 available

        # Bob sells 100 to Alice (doesn't have enough)
        trade = Trade("t1", "BTC-USD", "alice", "bob", 100.0, 50000.0, time.time_ns(), "2026-03-31")
        engine.ingest_trade(trade)
        engine.run_netting_cycle("2026-03-31")
        result = engine.execute_settlement("2026-03-31")

        assert result["failed"] >= 1
        assert len(engine.fail_records) >= 1
        assert len(engine.obligation_warehouse) >= 1

    def test_settlement_fail_resolved_by_borrow(self):
        """Fail resolved via stock borrow program."""
        engine = SettlementEngine()
        engine.register_member("alice", "Alice", initial_cash=1_000_000.0)
        engine.register_member("bob", "Bob", initial_cash=1_000_000.0)
        engine.register_member("lender", "Lender Corp", initial_cash=1_000_000.0)

        engine.deposit_securities("bob", "BTC-USD", 5.0)
        engine.deposit_securities("lender", "BTC-USD", 200.0)  # lender has excess

        trade = Trade("t1", "BTC-USD", "alice", "bob", 10.0, 50000.0, time.time_ns(), "2026-03-31")
        engine.ingest_trade(trade)
        engine.run_netting_cycle("2026-03-31")
        result = engine.execute_settlement("2026-03-31")

        # Should succeed via borrow
        assert result["settled"] == 1
        assert engine.stats.total_borrowed == 1

    def test_obligation_warehouse_retry(self):
        """Failed instruction retried after member deposits securities."""
        engine = SettlementEngine()
        engine.register_member("alice", "Alice", initial_cash=1_000_000.0)
        engine.register_member("bob", "Bob", initial_cash=1_000_000.0)
        engine.deposit_securities("bob", "BTC-USD", 1.0)

        trade = Trade("t1", "BTC-USD", "alice", "bob", 50.0, 50000.0, time.time_ns(), "2026-03-31")
        engine.ingest_trade(trade)
        engine.run_netting_cycle("2026-03-31")
        engine.execute_settlement("2026-03-31")

        assert len(engine.obligation_warehouse) >= 1

        # Bob deposits more securities
        engine.deposit_securities("bob", "BTC-USD", 100.0)

        resolved = engine.process_obligation_warehouse()
        assert len(resolved) >= 1
        assert engine.obligation_warehouse == []

    def test_margin_calculation(self):
        engine = self._create_engine_with_members()

        trade = Trade("t1", "BTC-USD", "alice", "bob", 10.0, 50000.0, time.time_ns(), "2026-03-31")
        engine.ingest_trade(trade)

        margins = engine.calculate_all_margins({"BTC-USD": 50000.0, "ETH-USD": 3000.0})
        assert "alice" in margins
        assert "bob" in margins

    def test_stats(self):
        engine = self._create_engine_with_members()

        trade = Trade("t1", "BTC-USD", "alice", "bob", 10.0, 50000.0, time.time_ns(), "2026-03-31")
        engine.ingest_trade(trade)
        engine.run_netting_cycle("2026-03-31")
        engine.execute_settlement("2026-03-31")

        stats = engine.get_settlement_stats()
        assert stats["total_trades_processed"] == 1
        assert stats["total_settled"] == 1
        assert stats["active_members"] == 3


# ---------------------------------------------------------------------------
# AI Settlement Predictor
# ---------------------------------------------------------------------------

class TestSettlementPredictor:
    def test_heuristic_low_risk(self):
        predictor = SettlementPredictor()
        prediction = predictor.predict_fail_heuristic(
            member_id="good_member", symbol="BTC-USD",
            position_size=10.0, available_securities=20.0,
            market_volatility=0.3,
        )
        assert prediction.probability < 0.5
        assert prediction.risk_level in ("LOW", "MEDIUM")

    def test_heuristic_high_risk_no_coverage(self):
        predictor = SettlementPredictor()
        prediction = predictor.predict_fail_heuristic(
            member_id="risky_member", symbol="BTC-USD",
            position_size=100.0, available_securities=10.0,
            market_volatility=1.5,
        )
        assert prediction.probability > 0.3
        assert prediction.risk_level in ("MEDIUM", "HIGH", "CRITICAL")

    def test_pattern_tracking_increases_risk(self):
        predictor = SettlementPredictor()

        # Record multiple fails
        for _ in range(5):
            predictor.record_settlement("bad_member", "BTC-USD", success=False)

        prediction = predictor.predict_fail_heuristic(
            member_id="bad_member", symbol="BTC-USD",
            position_size=50.0, available_securities=50.0,
            market_volatility=0.5,
        )

        # Should be higher risk due to historical fails
        assert prediction.historical_fail_rate == 1.0
        assert prediction.probability > 0.2

    def test_consecutive_fails_momentum(self):
        predictor = SettlementPredictor()

        predictor.record_settlement("member1", "ETH-USD", success=False)
        predictor.record_settlement("member1", "ETH-USD", success=False)
        predictor.record_settlement("member1", "ETH-USD", success=False)

        p1 = predictor.predict_fail_heuristic(
            "member1", "ETH-USD", 50.0, 50.0, 0.5,
        )

        # Reset consecutive fails
        predictor.record_settlement("member1", "ETH-USD", success=True)

        p2 = predictor.predict_fail_heuristic(
            "member1", "ETH-USD", 50.0, 50.0, 0.5,
        )

        assert p1.probability > p2.probability

    def test_predictions_stored(self):
        predictor = SettlementPredictor()
        predictor.predict_fail_heuristic("m1", "BTC-USD", 10.0, 20.0, 0.3)
        predictor.predict_fail_heuristic("m2", "ETH-USD", 10.0, 5.0, 0.8)

        recent = predictor.get_recent_predictions()
        assert len(recent) == 2

    def test_high_risk_members(self):
        predictor = SettlementPredictor(high_risk_threshold=0.3)

        predictor.predict_fail_heuristic("safe", "BTC-USD", 10.0, 100.0, 0.1)
        predictor.predict_fail_heuristic("risky", "BTC-USD", 100.0, 5.0, 1.5)

        high_risk = predictor.get_high_risk_members()
        risky_ids = [m["member_id"] for m in high_risk]
        assert "risky" in risky_ids

    def test_member_pattern(self):
        predictor = SettlementPredictor()
        predictor.record_settlement("m1", "BTC-USD", True, delay_hours=2.0)
        predictor.record_settlement("m1", "BTC-USD", False, delay_hours=24.0)

        pattern = predictor.get_member_pattern("m1")
        assert pattern is not None
        assert pattern["total_settlements"] == 2
        assert pattern["total_fails"] == 1
        assert pattern["fail_rate"] == 0.5
