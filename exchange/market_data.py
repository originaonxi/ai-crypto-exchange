"""
Market Data Feed Architecture — SIP-Inspired Real-Time Price Distribution.

Implements:
- Securities Information Processor (SIP) with gap detection & sequencing
- Multi-protocol distribution (UDP multicast simulation, TCP, WebSocket)
- Consolidated tape (CTA-style) with cross-venue normalization
- Message buffering with out-of-order handling
- NBBO (National Best Bid/Offer) calculation across venues

Inspired by:
- NASDAQ's 2013 SIP failure (3-hour halt, $10M fine)
- CTA/UTP consolidated tape architecture
- NASDAQ ITCH protocol (nanosecond precision)
- NYSE Pillar protocol

Performance target: 10M+ messages/sec with sub-millisecond latency.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------

class MessageType(str, Enum):
    TRADE = "TRADE"
    QUOTE = "QUOTE"
    ORDER_ADD = "ORDER_ADD"
    ORDER_CANCEL = "ORDER_CANCEL"
    ORDER_MODIFY = "ORDER_MODIFY"
    TRADE_BREAK = "TRADE_BREAK"
    SYSTEM = "SYSTEM"
    HALT = "HALT"


class FeedTier(str, Enum):
    DIRECT = "DIRECT"      # UDP multicast, lowest latency
    STANDARD = "STANDARD"  # TCP, reliable
    DELAYED = "DELAYED"    # WebSocket/HTTP, 15-min delay for free tier


@dataclass
class MarketDataMessage:
    """
    Normalized market data message (~200 bytes, matching real SIP format).

    Fields align with CTA/UTP consolidated tape specification.
    """
    exchange_id: str
    sequence_num: int
    message_type: MessageType
    symbol: str
    price: float
    quantity: float
    bid_price: float = 0.0
    ask_price: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    timestamp_ns: int = 0
    # Set by SIP after consolidation
    consolidated_seq: int = 0
    sip_timestamp_ns: int = 0


@dataclass
class NBBO:
    """National Best Bid and Offer across all venues."""
    symbol: str
    best_bid: float = 0.0
    best_bid_size: float = 0.0
    best_bid_exchange: str = ""
    best_ask: float = float('inf')
    best_ask_size: float = 0.0
    best_ask_exchange: str = ""
    spread: float = 0.0
    mid_price: float = 0.0
    last_updated_ns: int = 0


@dataclass
class VenueQuote:
    """Per-venue quote for NBBO calculation."""
    exchange_id: str
    symbol: str
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    timestamp_ns: int


@dataclass
class FeedStats:
    """Real-time feed performance metrics."""
    total_messages_processed: int = 0
    total_gaps_detected: int = 0
    total_gaps_resolved: int = 0
    total_duplicates_dropped: int = 0
    messages_per_second: float = 0.0
    avg_latency_ns: float = 0.0
    max_latency_ns: int = 0
    bytes_processed: int = 0
    _latency_sum: int = 0
    _msg_timestamps: deque = field(default_factory=lambda: deque(maxlen=10000))


# ---------------------------------------------------------------------------
# SIP Processor — Gap Detection & Consolidation
# ---------------------------------------------------------------------------

class SIPProcessor:
    """
    Securities Information Processor (SIP).

    Receives raw messages from multiple exchanges, detects sequence gaps,
    maintains order, and produces a consolidated feed with global sequencing.

    Gap detection: each exchange has independent sequence numbers.
    When a gap is detected, buffer subsequent messages and request retransmission.

    Real SIP processes 10M+ messages/sec across 13 exchanges.
    """

    def __init__(self, max_gap_buffer_size: int = 100_000):
        # Per-exchange expected sequence numbers
        self.expected_sequences: dict[str, int] = defaultdict(int)

        # Gap buffer: exchange -> {sequence_num -> message}
        self.gap_buffer: dict[str, dict[int, MarketDataMessage]] = defaultdict(dict)
        self.max_gap_buffer_size = max_gap_buffer_size

        # Consolidated output sequence
        self._consolidated_seq = 0

        # Retransmission requests pending
        self._pending_retransmissions: list[tuple[str, int, int]] = []

        # Stats
        self.stats = FeedStats()

        # Output callbacks
        self._on_message: list[Callable[[MarketDataMessage], None]] = []

    def on_message(self, callback: Callable[[MarketDataMessage], None]):
        self._on_message.append(callback)

    def process_message(self, msg: MarketDataMessage) -> Optional[MarketDataMessage]:
        """
        Process incoming exchange message with gap detection.

        Returns consolidated message if in-sequence, None if buffered.
        """
        start_ns = time.perf_counter_ns()
        exchange = msg.exchange_id
        expected = self.expected_sequences[exchange]

        result = None

        if msg.sequence_num == expected:
            # In order — process immediately
            result = self._consolidate(msg)
            self.expected_sequences[exchange] += 1

            # Drain any buffered messages now in sequence
            self._drain_buffer(exchange)

        elif msg.sequence_num > expected:
            # Gap detected — buffer and request retransmission
            self.gap_buffer[exchange][msg.sequence_num] = msg
            self.stats.total_gaps_detected += 1

            # Limit buffer size to prevent memory exhaustion
            if len(self.gap_buffer[exchange]) > self.max_gap_buffer_size:
                # Force skip: advance expected sequence to oldest buffered
                min_seq = min(self.gap_buffer[exchange].keys())
                logger.warning(
                    f"Gap buffer overflow for {exchange}: skipping from {expected} to {min_seq}"
                )
                self.expected_sequences[exchange] = min_seq
                self._drain_buffer(exchange)

            self._request_retransmission(exchange, expected, msg.sequence_num - 1)
        else:
            # Duplicate or late — drop
            self.stats.total_duplicates_dropped += 1

        # Track latency
        latency = time.perf_counter_ns() - start_ns
        self.stats._latency_sum += latency
        self.stats.total_messages_processed += 1
        self.stats.avg_latency_ns = self.stats._latency_sum / self.stats.total_messages_processed
        if latency > self.stats.max_latency_ns:
            self.stats.max_latency_ns = latency

        # Track throughput
        now = time.time()
        self.stats._msg_timestamps.append(now)
        if len(self.stats._msg_timestamps) > 1:
            window = now - self.stats._msg_timestamps[0]
            if window > 0:
                self.stats.messages_per_second = len(self.stats._msg_timestamps) / window

        return result

    def _drain_buffer(self, exchange: str):
        """Process buffered messages that are now in sequence."""
        while True:
            expected = self.expected_sequences[exchange]
            if expected in self.gap_buffer[exchange]:
                msg = self.gap_buffer[exchange].pop(expected)
                self._consolidate(msg)
                self.expected_sequences[exchange] += 1
                self.stats.total_gaps_resolved += 1
            else:
                break

    def _consolidate(self, msg: MarketDataMessage) -> MarketDataMessage:
        """Add consolidated sequence number and SIP timestamp."""
        self._consolidated_seq += 1
        msg.consolidated_seq = self._consolidated_seq
        msg.sip_timestamp_ns = time.time_ns()

        # Estimate bytes (~200 bytes per message like real SIP)
        self.stats.bytes_processed += 200

        # Notify subscribers
        for cb in self._on_message:
            try:
                cb(msg)
            except Exception as e:
                logger.error(f"SIP subscriber error: {e}")

        return msg

    def _request_retransmission(self, exchange: str, start: int, end: int):
        """Request retransmission of missing sequence range."""
        self._pending_retransmissions.append((exchange, start, end))
        logger.debug(f"Retransmission request: {exchange} seq {start}-{end}")

    def inject_retransmission(self, msg: MarketDataMessage):
        """Handle a retransmitted message (same as normal processing)."""
        self.process_message(msg)

    def get_stats(self) -> dict:
        return {
            "total_messages_processed": self.stats.total_messages_processed,
            "total_gaps_detected": self.stats.total_gaps_detected,
            "total_gaps_resolved": self.stats.total_gaps_resolved,
            "total_duplicates_dropped": self.stats.total_duplicates_dropped,
            "messages_per_second": round(self.stats.messages_per_second, 1),
            "avg_latency_ns": round(self.stats.avg_latency_ns, 1),
            "max_latency_ns": self.stats.max_latency_ns,
            "bytes_processed": self.stats.bytes_processed,
            "bytes_processed_mb": round(self.stats.bytes_processed / (1024 * 1024), 2),
            "pending_retransmissions": len(self._pending_retransmissions),
            "buffered_messages": sum(len(b) for b in self.gap_buffer.values()),
        }


# ---------------------------------------------------------------------------
# NBBO Calculator — National Best Bid/Offer
# ---------------------------------------------------------------------------

class NBBOCalculator:
    """
    Calculates National Best Bid and Offer across all venues.

    The NBBO is the foundation of SEC Regulation NMS — all venues must
    route orders to the venue displaying the best price.
    """

    def __init__(self):
        self.venue_quotes: dict[str, dict[str, VenueQuote]] = defaultdict(dict)  # symbol -> exchange -> quote
        self.nbbo: dict[str, NBBO] = {}

    def update_quote(self, quote: VenueQuote) -> NBBO:
        """Update a venue's quote and recalculate NBBO."""
        self.venue_quotes[quote.symbol][quote.exchange_id] = quote
        return self._recalculate_nbbo(quote.symbol)

    def _recalculate_nbbo(self, symbol: str) -> NBBO:
        """Find best bid/ask across all venues."""
        quotes = self.venue_quotes.get(symbol, {})
        if not quotes:
            return NBBO(symbol=symbol)

        best_bid = 0.0
        best_bid_size = 0.0
        best_bid_exchange = ""
        best_ask = float('inf')
        best_ask_size = 0.0
        best_ask_exchange = ""

        for exchange_id, q in quotes.items():
            if q.bid_price > best_bid:
                best_bid = q.bid_price
                best_bid_size = q.bid_size
                best_bid_exchange = exchange_id
            if 0 < q.ask_price < best_ask:
                best_ask = q.ask_price
                best_ask_size = q.ask_size
                best_ask_exchange = exchange_id

        if best_ask == float('inf'):
            best_ask = 0.0

        spread = best_ask - best_bid if best_ask > best_bid > 0 else 0.0
        mid_price = (best_bid + best_ask) / 2 if best_ask > 0 and best_bid > 0 else 0.0

        nbbo = NBBO(
            symbol=symbol,
            best_bid=best_bid,
            best_bid_size=best_bid_size,
            best_bid_exchange=best_bid_exchange,
            best_ask=best_ask,
            best_ask_size=best_ask_size,
            best_ask_exchange=best_ask_exchange,
            spread=round(spread, 6),
            mid_price=round(mid_price, 6),
            last_updated_ns=time.time_ns(),
        )
        self.nbbo[symbol] = nbbo
        return nbbo

    def get_nbbo(self, symbol: str) -> Optional[dict]:
        nbbo = self.nbbo.get(symbol)
        if not nbbo:
            return None
        return {
            "symbol": nbbo.symbol,
            "best_bid": nbbo.best_bid,
            "best_bid_size": nbbo.best_bid_size,
            "best_bid_exchange": nbbo.best_bid_exchange,
            "best_ask": nbbo.best_ask,
            "best_ask_size": nbbo.best_ask_size,
            "best_ask_exchange": nbbo.best_ask_exchange,
            "spread": nbbo.spread,
            "mid_price": nbbo.mid_price,
        }

    def get_all_nbbos(self) -> list[dict]:
        return [self.get_nbbo(s) for s in self.nbbo]


# ---------------------------------------------------------------------------
# Market Data Feed Manager — Tiered Distribution
# ---------------------------------------------------------------------------

class MarketDataFeedManager:
    """
    Tiered market data distribution system.

    Direct (HFT): UDP multicast simulation — lowest latency, potential gaps
    Standard: TCP-style — reliable, ordered delivery
    Delayed: 15-minute delayed feed for free/retail tier

    Manages subscriber registration, feed filtering, and delivery.
    """

    def __init__(self):
        self.sip = SIPProcessor()
        self.nbbo_calculator = NBBOCalculator()

        # Subscribers by tier
        self.subscribers: dict[FeedTier, list[Callable]] = {
            FeedTier.DIRECT: [],
            FeedTier.STANDARD: [],
            FeedTier.DELAYED: [],
        }

        # Delayed message buffer (15 min)
        self.delayed_buffer: deque[tuple[int, MarketDataMessage]] = deque()
        self.delay_ns = 15 * 60 * 1_000_000_000  # 15 minutes in nanoseconds

        # Symbol filters per subscriber
        self._symbol_filters: dict[int, set[str]] = {}  # subscriber_id -> symbols

        # Wire SIP output to distribution
        self.sip.on_message(self._distribute)

    def subscribe(self, tier: FeedTier, callback: Callable, symbols: Optional[set[str]] = None) -> int:
        """Subscribe to market data feed. Returns subscriber ID."""
        self.subscribers[tier].append(callback)
        sub_id = id(callback)
        if symbols:
            self._symbol_filters[sub_id] = symbols
        return sub_id

    def ingest(self, msg: MarketDataMessage) -> Optional[MarketDataMessage]:
        """Ingest raw exchange message into the SIP pipeline."""
        result = self.sip.process_message(msg)

        # Update NBBO if quote message
        if msg.message_type == MessageType.QUOTE and msg.bid_price > 0:
            self.nbbo_calculator.update_quote(VenueQuote(
                exchange_id=msg.exchange_id,
                symbol=msg.symbol,
                bid_price=msg.bid_price,
                ask_price=msg.ask_price,
                bid_size=msg.bid_size,
                ask_size=msg.ask_size,
                timestamp_ns=msg.timestamp_ns,
            ))

        return result

    def _distribute(self, msg: MarketDataMessage):
        """Distribute consolidated message to all tiers."""
        # Direct tier — immediate, no filtering overhead
        for cb in self.subscribers[FeedTier.DIRECT]:
            sub_id = id(cb)
            if sub_id not in self._symbol_filters or msg.symbol in self._symbol_filters[sub_id]:
                try:
                    cb(msg)
                except Exception as e:
                    logger.error(f"Direct feed subscriber error: {e}")

        # Standard tier — immediate, with filtering
        for cb in self.subscribers[FeedTier.STANDARD]:
            sub_id = id(cb)
            if sub_id not in self._symbol_filters or msg.symbol in self._symbol_filters[sub_id]:
                try:
                    cb(msg)
                except Exception as e:
                    logger.error(f"Standard feed subscriber error: {e}")

        # Delayed tier — buffer for 15 minutes
        self.delayed_buffer.append((time.time_ns(), msg))

    def flush_delayed(self):
        """Deliver delayed messages that have aged past the delay window."""
        now = time.time_ns()
        delivered = 0

        while self.delayed_buffer and (now - self.delayed_buffer[0][0]) >= self.delay_ns:
            _, msg = self.delayed_buffer.popleft()
            for cb in self.subscribers[FeedTier.DELAYED]:
                sub_id = id(cb)
                if sub_id not in self._symbol_filters or msg.symbol in self._symbol_filters[sub_id]:
                    try:
                        cb(msg)
                    except Exception:
                        pass
            delivered += 1

        return delivered

    def get_nbbo(self, symbol: str) -> Optional[dict]:
        return self.nbbo_calculator.get_nbbo(symbol)

    def get_all_nbbos(self) -> list[dict]:
        return self.nbbo_calculator.get_all_nbbos()

    def get_feed_stats(self) -> dict:
        sip_stats = self.sip.get_stats()
        sip_stats["subscribers"] = {
            tier.value: len(subs) for tier, subs in self.subscribers.items()
        }
        sip_stats["delayed_buffer_size"] = len(self.delayed_buffer)
        return sip_stats
