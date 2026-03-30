#!/usr/bin/env python3
"""
Interactive demo showing the exchange in action.

Simulates realistic market activity with multiple traders,
triggers anomaly detection, and demonstrates the kill switch.
"""

import random
import time
from exchange.matching_engine import MatchingEngine
from exchange.order_book import OrderType, Side
from exchange.risk_manager import RiskManager, RiskLimits
from exchange.ai_detector import AIAnomalyDetector
from exchange.wal import WAL


def main():
    print("\n" + "="*70)
    print("  AI-ENHANCED CRYPTO EXCHANGE — LIVE DEMO")
    print("  LMAX Disruptor-inspired | Claude AI Anomaly Detection")
    print("="*70)

    # Setup
    wal = WAL("data/demo.wal")
    wal.open()

    halts = []
    risk_mgr = RiskManager(
        limits=RiskLimits(
            max_order_quantity=500.0,
            max_order_value=500_000.0,
            price_band_pct=0.15,
        ),
        halt_callback=lambda r: halts.append(r),
    )
    ai_detector = AIAnomalyDetector(
        halt_callback=lambda r: halts.append(r),
    )

    def risk_check(order, execs):
        ok, _ = risk_mgr.pre_trade_check(order)
        return ok

    engine = MatchingEngine(
        symbols=["BTC-USD", "ETH-USD", "SOL-USD"],
        wal=wal,
        risk_check=risk_check,
    )

    # Wire up AI detector
    def on_exec(exe):
        risk_mgr.post_trade_update(exe)
        ai_detector.record_trade(exe.symbol, exe.price, exe.quantity, exe.buyer_client_id, exe.seller_client_id)

    engine.on_execution(on_exec)

    # Phase 1: Normal trading
    print("\n[Phase 1] Normal Market Activity — 500 orders")
    print("-" * 50)
    for i in range(500):
        symbol = random.choice(["BTC-USD", "ETH-USD", "SOL-USD"])
        prices = {"BTC-USD": 65000, "ETH-USD": 3500, "SOL-USD": 150}
        base = prices[symbol]
        side = random.choice([Side.BUY, Side.SELL])
        price = round(base + random.gauss(0, base * 0.005), 2)
        qty = round(random.uniform(0.01, 5.0), 4)

        engine.create_order(symbol, side, OrderType.LIMIT, qty, price, f"trader-{random.randint(1,20)}")

    s = engine.stats
    print(f"  Orders:     {s.orders_processed}")
    print(f"  Matched:    {s.orders_matched}")
    print(f"  Executions: {s.total_executions}")
    print(f"  Volume:     ${s.total_volume:,.2f}")
    print(f"  Avg Lat:    {s.avg_latency_us:.1f} μs")

    for sym in ["BTC-USD", "ETH-USD", "SOL-USD"]:
        snap = engine.get_book_snapshot(sym)
        if snap.best_bid and snap.best_ask:
            print(f"  {sym}: bid=${snap.best_bid:,.2f} ask=${snap.best_ask:,.2f} spread=${snap.spread:,.2f}")

    # Phase 2: Flash crash simulation
    print(f"\n[Phase 2] Simulating Flash Crash on BTC-USD")
    print("-" * 50)
    for i in range(10):
        crash_price = 65000 - (i * 4000)
        engine.create_order("BTC-USD", Side.SELL, OrderType.LIMIT, 100.0, crash_price, "panic-seller")
        ai_detector.check_rules("BTC-USD")

    alerts = ai_detector.get_recent_alerts()
    for alert in alerts:
        print(f"  ALERT [{alert.severity}] {alert.alert_type}: {alert.description}")

    # Phase 3: Pump and dump simulation
    print(f"\n[Phase 3] Simulating Pump-and-Dump on SOL-USD")
    print("-" * 50)
    base_time = time.time()
    for i in range(15):
        pump_price = 150 + (i * 15)
        ai_detector._prices["SOL-USD"].append((base_time + i, pump_price))
        ai_detector._orders["SOL-USD"].append((base_time + i, "whale", "BUY"))
        ai_detector._volumes["SOL-USD"].append((base_time + i, pump_price * 100))

    alert = ai_detector.check_rules("SOL-USD")
    if alert:
        print(f"  ALERT [{alert.severity}] {alert.alert_type}: {alert.description}")

    # Summary
    print(f"\n{'='*70}")
    print(f"  DEMO SUMMARY")
    print(f"{'='*70}")
    s = engine.stats
    print(f"  Total Orders:      {s.orders_processed}")
    print(f"  Total Executions:  {s.total_executions}")
    print(f"  Total Volume:      ${s.total_volume:,.2f}")
    print(f"  Avg Latency:       {s.avg_latency_us:.1f} μs")
    print(f"  Risk Rejections:   {risk_mgr.rejections}")
    print(f"  AI Alerts:         {len(ai_detector.alerts)}")
    print(f"  Halts Triggered:   {len(halts)}")
    print(f"  WAL Size:          {wal.size_bytes:,} bytes")
    print(f"{'='*70}\n")

    wal.close()


if __name__ == "__main__":
    main()
