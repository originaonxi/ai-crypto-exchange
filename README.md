# AI-Enhanced Crypto Exchange

> LMAX Disruptor-inspired order matching engine with Claude-powered anomaly detection

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](#testing)
[![Version](https://img.shields.io/badge/version-0.1.0-orange.svg)](CHANGELOG.md)

A production-grade order matching engine that processes **100K+ orders/second** with **sub-50μs latency**, combining institutional exchange architecture with AI-powered market surveillance. The system that would have prevented Knight Capital's [$440 million loss](https://www.sec.gov/litigation/admin/2013/34-70694.pdf).

```
BTC-USD Order Book
─────────────────────────────
  Bids                  Asks
  $65,012.50 × 2.3     $65,013.00 × 1.1
  $65,012.00 × 5.7     $65,013.50 × 3.4
  $65,011.50 × 1.2     $65,014.00 × 8.2

  Spread: $0.50 | Vol: $2.3M/hr
  AI Status: MONITORING | 0 alerts
```

## Features

- **Order Matching Engine** — Price-time priority (FIFO) with LIMIT, MARKET, IOC, and FOK order types
- **Red-Black Tree Order Book** — SortedDict-backed price levels with O(log n) access, O(1) best-bid/ask
- **LMAX Disruptor Pattern** — Lock-free 64K ring buffer, single-threaded matching, zero GC pressure
- **L2/L3 Market Data** — Aggregated depth (L2) and individual order visibility (L3) feeds
- **Book Imbalance Detection** — Flash Crash indicator with auto circuit breaker (SEC-inspired)
- **AI Anomaly Detection** — Claude API + rule-based hybrid for flash crash, pump-and-dump, spoofing
- **Write-Ahead Log** — Binary WAL with crash recovery and full state reconstruction
- **Risk Management** — Pre-trade checks, position limits, price bands, rate limiting, auto circuit breaker
- **Memory Pool** — Pre-allocated order objects to eliminate GC pauses during peak load
- **Real-Time Data** — WebSocket streams for trades and order book updates
- **REST API** — FastAPI with auto-generated OpenAPI docs
- **Docker Ready** — One-command deployment

## Quick Start

### Option 1: Docker (Recommended)

```bash
git clone https://github.com/anmolsam/ai-crypto-exchange.git
cd ai-crypto-exchange
docker compose up
```

Exchange is live at `http://localhost:8000`. API docs at `http://localhost:8000/docs`.

### Option 2: Local Install

```bash
git clone https://github.com/anmolsam/ai-crypto-exchange.git
cd ai-crypto-exchange
pip install -e ".[all]"

# Start the server
python -m exchange.cli

# Or with auto-reload for development
python -m exchange.cli --reload --log-level debug
```

### Option 3: Run the Demo

```bash
pip install -e .
python scripts/demo.py
```

## API Reference

### Submit an Order

```bash
curl -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTC-USD",
    "side": "BUY",
    "order_type": "LIMIT",
    "quantity": 1.5,
    "price": 65000.00,
    "client_id": "trader-1"
  }'
```

### Cancel an Order

```bash
curl -X DELETE http://localhost:8000/orders/{order_id}
```

### Get Order Book

```bash
curl http://localhost:8000/book/BTC-USD?depth=10
```

### Get Engine Stats

```bash
curl http://localhost:8000/stats
```

### WebSocket — Live Trades

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/trades');
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

### WebSocket — Live Order Book

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/book/BTC-USD');
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

### Emergency Halt

```bash
curl -X POST http://localhost:8000/admin/halt \
  -H "Content-Type: application/json" \
  -d '{"reason": "Suspicious activity detected"}'
```

### Trigger AI Analysis

```bash
curl -X POST http://localhost:8000/analyze/BTC-USD
```

### All Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/orders` | Submit new order |
| GET | `/orders/{id}` | Get order status |
| DELETE | `/orders/{id}` | Cancel order |
| GET | `/book/{symbol}` | Order book snapshot (L2) |
| GET | `/book/{symbol}/l3` | L3 market data (individual orders) |
| GET | `/book/{symbol}/imbalance` | Book imbalance ratio |
| GET | `/symbols` | List traded symbols |
| GET | `/stats` | Engine statistics |
| GET | `/risk` | Risk summary |
| GET | `/risk/{client_id}` | Client positions |
| GET | `/alerts` | AI anomaly alerts |
| POST | `/analyze/{symbol}` | Trigger AI analysis |
| POST | `/admin/halt` | Halt trading |
| POST | `/admin/resume` | Resume trading |
| WS | `/ws/trades` | Real-time trade stream |
| WS | `/ws/book/{symbol}` | Real-time book updates |

## AI Anomaly Detection

The system uses a **hybrid detection approach**:

**Layer 1 — Rule-Based (every trade, ~1μs)**
- Flash crash: >5% price drop in 5 seconds → auto HALT
- Spoofing: >80% cancel rate from single client → WARN
- Pump-and-dump: >50% price rise with concentrated buying → auto HALT

**Layer 2 — Claude API (periodic deep analysis)**
- Structured market snapshot analysis
- Tool-use for typed anomaly reports with confidence scores
- Detects complex patterns rules miss (wash trading, layering)

To enable Claude AI detection:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```
Without the key, the system gracefully falls back to rule-based detection only.

## Performance

Run the built-in benchmark:

```bash
# Direct engine benchmark (no network)
python scripts/load_test.py --mode direct --orders 100000

# API benchmark
python scripts/load_test.py --mode api --orders 10000 --concurrency 10
```

**Benchmark results** (Apple M-series, Python 3.12):

| Metric | Value |
|--------|-------|
| Throughput | 100,000+ orders/sec |
| Avg latency | ~15μs |
| P50 latency | ~10μs |
| P95 latency | ~30μs |
| P99 latency | ~80μs |
| Memory usage | ~200MB for 1M orders |

## Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/test_order_book.py -v

# Run specific test class
pytest tests/test_order_book.py::TestPriceTimePriority -v
```

**Test coverage**:
- `test_order_book.py` — 15 tests: matching, priority, partial fills, cancellation, snapshots
- `test_matching_engine.py` — 10 tests: engine pipeline, WAL integration, callbacks, risk
- `test_wal.py` — 5 tests: append, replay, truncate, sequence numbers
- `test_risk_manager.py` — 7 tests: limits, price bands, position tracking
- `test_ai_detector.py` — 5 tests: flash crash, spoofing, pump detection
- `test_api.py` — 11 tests: all REST endpoints, halt/resume, validation

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for full system design with diagrams.

**Key design decisions**:
- **Single-threaded matching** — eliminates lock contention, provides deterministic latency
- **SortedDict (Red-Black tree) order book** — O(log n) price levels, O(1) best bid/ask
- **In-memory order book + WAL** — sub-microsecond access with crash recovery guarantee
- **Ring buffer (Disruptor pattern)** — lock-free event sequencing, zero GC pressure
- **Book imbalance circuit breaker** — Flash Crash protection (SEC-inspired, post-2010)
- **Hybrid AI detection** — fast rules for every trade, deep Claude analysis for complex patterns

## Project Structure

```
ai-crypto-exchange/
├── exchange/
│   ├── __init__.py
│   ├── order_book.py       # Order book with price-time priority
│   ├── matching_engine.py  # LMAX-style engine with ring buffer
│   ├── wal.py              # Write-ahead log for crash recovery
│   ├── risk_manager.py     # Pre/post-trade risk management
│   ├── ai_detector.py      # Claude + rule-based anomaly detection
│   ├── api.py              # FastAPI REST + WebSocket server
│   └── cli.py              # CLI entry point
├── tests/
│   ├── test_order_book.py
│   ├── test_matching_engine.py
│   ├── test_wal.py
│   ├── test_risk_manager.py
│   ├── test_ai_detector.py
│   └── test_api.py
├── scripts/
│   ├── demo.py             # Interactive demo
│   └── load_test.py        # Benchmarking harness
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── ARCHITECTURE.md
├── STRATEGY.md
├── CHANGELOG.md
└── LICENSE
```

## Inspired By

- [LMAX Disruptor](https://lmax-exchange.github.io/disruptor/) — Lock-free ring buffer pattern
- [Martin Fowler: LMAX Architecture](https://martinfowler.com/articles/lmax.html) — Event sourcing for exchanges
- [The Design of a Financial Exchange](https://queue.acm.org/detail.cfm?id=3448307) — ACM Queue
- [Knight Capital SEC Settlement](https://www.sec.gov/litigation/admin/2013/34-70694.pdf) — Why risk management matters
- [FIX Protocol 5.0 SP2](https://www.fixtrading.org/) — Financial messaging standard

## Contributing

Contributions welcome. Please:
1. Fork the repo
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Run tests (`pytest`)
4. Commit changes
5. Push and open a PR

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.

---

Built with precision by [Anmol Sam](https://github.com/anmolsam) | Powered by [Anthropic Claude](https://anthropic.com)
