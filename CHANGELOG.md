# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-03-30

### Added — Settlement & Clearing Architecture (T+1)
- **Multilateral netting engine** (CNS-style) — reduces 98% of gross trade volume to minimal settlement instructions
- **Central Counterparty (CCP)** — hub-and-spoke model, 600+ member firm support, DVP atomic settlement
- **Monte Carlo margin calculator** — 10K simulations, VaR (95th/99th percentile), stress testing, intraday recalculation
- **Stock borrow program** — automated fail resolution by lending excess securities from members
- **Obligation warehouse** — tracks failed instructions with daily mark-to-market and penalties
- **AI settlement predictor** — Claude API + heuristic fallback for 24-hour-ahead fail prediction with probability scoring

### Added — Risk Management & Circuit Breakers
- **SEC market-wide circuit breakers** — Level 1 (7%), Level 2 (13%), Level 3 (20%) with automatic halt/resume
- **LULD (Limit Up-Limit Down)** — per-security dynamic price bands (±5% Tier 1, ±10% Tier 2), 15s limit state, trading pause escalation
- **Pre-trade risk engine** — sub-5μs target: kill switch, order size, notional, price collar, position limit, buying power, rate limit (L1 cache optimized)
- **Kill switch manager** — per-participant trade termination in <100μs, cancels all orders, blocks new submissions

### Added — Market Data Feed Architecture
- **SIP processor** — gap detection, sequence ordering, retransmission requests, multi-exchange consolidation
- **NBBO calculator** — National Best Bid/Offer across all connected venues
- **Tiered feed distribution** — Direct (HFT/UDP-style), Standard (TCP), Delayed (15-min retail)
- **Feed statistics** — throughput, latency, gap resolution, byte processing

### Added — API Endpoints
- 30+ new REST endpoints for settlement, circuit breakers, LULD, kill switch, risk engine, market data, NBBO
- Settlement pipeline: member registration, deposit, trade ingestion, netting, execution, margin, warehouse
- AI predictions: fail probability, risk level, recommended action, high-risk member dashboard

### Changed
- Version bumped to 0.3.0
- Updated project description and keywords
- API module now initializes settlement engine, circuit breakers, LULD, pre-trade risk, and market data feed on startup

### Testing
- 76 new tests (152 total), all passing in 0.75s
- `test_settlement.py` — 31 tests: netting, margin, DVP, fails, borrow, warehouse, AI prediction
- `test_circuit_breaker.py` — 29 tests: LULD bands, circuit breakers, kill switch, pre-trade risk
- `test_market_data.py` — 16 tests: SIP gap detection, NBBO, feed tiers, filtering

---

## [0.2.0] - 2026-03-30

### Added
- **SortedDict (Red-Black tree) order book** — O(log n) sorted price levels replacing heap-based implementation
- **IOC (Immediate-or-Cancel) orders** — fill what's available, cancel the rest
- **FOK (Fill-or-Kill) orders** — fill completely or reject entirely
- **L3 market data** — individual order visibility at each price level
- **Book imbalance detection** — Flash Crash indicator with circuit breaker
- **Memory pool** — pre-allocated order objects to eliminate GC pauses
- **Mid-price and imbalance ratio** in order book snapshots
- **L3 and imbalance API endpoints** — `/book/{symbol}/l3` and `/book/{symbol}/imbalance`
- 19 new tests (76 total) covering IOC, FOK, L3, imbalance, SortedDict, OrderPool

### Changed
- Order book internals upgraded from heapq to SortedDict (sortedcontainers)
- Matching engine now supports imbalance-based circuit breaker
- API accepts IOC and FOK order types
- Snapshot includes mid_price, imbalance_ratio, total_bid/ask_quantity

---

## [0.1.0] - 2026-03-30

### Added
- **Order Matching Engine** with LMAX Disruptor-inspired single-threaded architecture
  - Price-time priority (FIFO) matching
  - Limit and market order support
  - Partial fill handling
  - Order cancellation
  - Lock-free 64K ring buffer for event sequencing
- **Order Book** with O(log n) best-price lookup
  - Separate FIFO queues per price level
  - Heap-based price level management
  - Real-time snapshot generation
- **Write-Ahead Log** for crash recovery
  - Binary length-prefixed format
  - Sequential replay for state reconstruction
  - Truncation after checkpoint
- **Risk Management System**
  - Pre-trade checks: quantity, value, price band, rate limit, position limit
  - Post-trade: position tracking, P&L calculation, volume spike detection
  - Automatic circuit breaker (kill switch)
- **AI Anomaly Detection** (hybrid approach)
  - Rule-based fast path: flash crash, spoofing, pump-and-dump detection
  - Claude API deep analysis with tool_use for structured reports
  - Graceful fallback when API key not configured
- **REST API** via FastAPI
  - Order CRUD (submit, cancel, query)
  - Order book snapshots
  - Engine statistics
  - Risk and position endpoints
  - AI alert history
  - Admin halt/resume controls
- **WebSocket Market Data**
  - Real-time trade stream
  - Real-time order book updates per symbol
- **Test Suite** — 53 tests across 6 test files
- **Load Testing Harness**
- **Interactive Demo** — simulates normal trading, flash crash, pump-and-dump
- **Docker Support** — single-command deployment
- **Documentation** — README, ARCHITECTURE.md, STRATEGY.md
