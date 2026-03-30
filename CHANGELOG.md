# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  - Order book: matching, priority, partial fills, cancellation
  - Matching engine: pipeline, WAL integration, callbacks
  - WAL: persistence, replay, truncation
  - Risk manager: limits, bands, positions
  - AI detector: flash crash, spoofing, pump detection
  - API: all endpoints, validation, halt/resume
- **Load Testing Harness**
  - Direct engine benchmark
  - API benchmark with configurable concurrency
- **Interactive Demo** — simulates normal trading, flash crash, pump-and-dump
- **Docker Support** — single-command deployment
- **Documentation** — README, ARCHITECTURE.md, STRATEGY.md
