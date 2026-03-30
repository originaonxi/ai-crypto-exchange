#!/usr/bin/env python3
"""
Load Test Harness for the Exchange.

Generates realistic order flow and measures throughput/latency.
Can be run standalone or against the REST API.

Usage:
    python scripts/load_test.py --mode direct --orders 100000
    python scripts/load_test.py --mode api --url http://localhost:8000 --orders 10000
"""

import argparse
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from exchange.matching_engine import MatchingEngine
from exchange.order_book import OrderType, Side


def run_direct_benchmark(num_orders: int, num_symbols: int = 5):
    """Benchmark the engine directly (no network overhead)."""
    symbols = [f"SYM{i}-USD" for i in range(num_symbols)]
    engine = MatchingEngine(symbols=symbols)

    latencies = []
    base_prices = {s: random.uniform(10, 1000) for s in symbols}

    print(f"\n{'='*60}")
    print(f"  Direct Engine Benchmark")
    print(f"  Orders: {num_orders:,}")
    print(f"  Symbols: {num_symbols}")
    print(f"{'='*60}\n")

    start = time.perf_counter()

    for i in range(num_orders):
        symbol = random.choice(symbols)
        side = random.choice([Side.BUY, Side.SELL])
        base = base_prices[symbol]
        price = round(base + random.gauss(0, base * 0.02), 2)
        qty = round(random.uniform(0.1, 100.0), 4)
        order_type = random.choices(
            [OrderType.LIMIT, OrderType.MARKET],
            weights=[0.85, 0.15],
        )[0]

        t0 = time.perf_counter_ns()
        engine.create_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=qty,
            price=price if order_type == OrderType.LIMIT else 0,
            client_id=f"client-{random.randint(1, 100)}",
        )
        t1 = time.perf_counter_ns()
        latencies.append((t1 - t0) / 1000)  # microseconds

        if (i + 1) % 10000 == 0:
            elapsed = time.perf_counter() - start
            rate = (i + 1) / elapsed
            print(f"  Progress: {i+1:>8,} orders | {rate:>10,.0f} orders/sec")

    total_time = time.perf_counter() - start
    throughput = num_orders / total_time

    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  Total time:        {total_time:.3f}s")
    print(f"  Throughput:        {throughput:,.0f} orders/sec")
    print(f"  Orders matched:    {engine.stats.orders_matched:,}")
    print(f"  Total executions:  {engine.stats.total_executions:,}")
    print(f"  Total volume:      ${engine.stats.total_volume:,.2f}")
    print(f"  Avg latency:       {statistics.mean(latencies):.1f} μs")
    print(f"  P50 latency:       {statistics.median(latencies):.1f} μs")
    print(f"  P95 latency:       {sorted(latencies)[int(len(latencies)*0.95)]:.1f} μs")
    print(f"  P99 latency:       {sorted(latencies)[int(len(latencies)*0.99)]:.1f} μs")
    print(f"  Max latency:       {max(latencies):.1f} μs")
    print(f"{'='*60}\n")

    return throughput, latencies


def run_api_benchmark(url: str, num_orders: int, concurrency: int = 10):
    """Benchmark against the REST API."""
    import requests

    symbols = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "ADA-USD"]
    latencies = []

    print(f"\n{'='*60}")
    print(f"  API Benchmark")
    print(f"  URL: {url}")
    print(f"  Orders: {num_orders:,}")
    print(f"  Concurrency: {concurrency}")
    print(f"{'='*60}\n")

    def send_order(_):
        symbol = random.choice(symbols)
        side = random.choice(["BUY", "SELL"])
        price = round(random.uniform(90, 110), 2)
        qty = round(random.uniform(0.1, 10.0), 4)

        t0 = time.perf_counter()
        resp = requests.post(f"{url}/orders", json={
            "symbol": symbol,
            "side": side,
            "order_type": "LIMIT",
            "quantity": qty,
            "price": price,
            "client_id": f"loadtest-{random.randint(1, 50)}",
        })
        t1 = time.perf_counter()
        return (t1 - t0) * 1000  # milliseconds

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        latencies = list(pool.map(send_order, range(num_orders)))
    total_time = time.perf_counter() - start

    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  Total time:        {total_time:.3f}s")
    print(f"  Throughput:        {num_orders / total_time:,.0f} orders/sec")
    print(f"  Avg latency:       {statistics.mean(latencies):.1f} ms")
    print(f"  P50 latency:       {statistics.median(latencies):.1f} ms")
    print(f"  P95 latency:       {sorted(latencies)[int(len(latencies)*0.95)]:.1f} ms")
    print(f"  P99 latency:       {sorted(latencies)[int(len(latencies)*0.99)]:.1f} ms")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exchange Load Tester")
    parser.add_argument("--mode", choices=["direct", "api"], default="direct")
    parser.add_argument("--orders", type=int, default=100000)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--symbols", type=int, default=5)
    args = parser.parse_args()

    if args.mode == "direct":
        run_direct_benchmark(args.orders, args.symbols)
    else:
        run_api_benchmark(args.url, args.orders, args.concurrency)
