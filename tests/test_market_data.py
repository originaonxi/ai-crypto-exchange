"""Tests for Market Data Feed Architecture."""

from __future__ import annotations

import time

import pytest

from exchange.market_data import (
    FeedTier,
    MarketDataFeedManager,
    MarketDataMessage,
    MessageType,
    NBBOCalculator,
    SIPProcessor,
    VenueQuote,
)


class TestSIPProcessor:
    def test_in_order_processing(self):
        sip = SIPProcessor()
        messages = []
        sip.on_message(lambda m: messages.append(m))

        for i in range(5):
            msg = MarketDataMessage(
                exchange_id="NYSE", sequence_num=i, message_type=MessageType.TRADE,
                symbol="BTC-USD", price=50000.0 + i, quantity=1.0,
                timestamp_ns=time.time_ns(),
            )
            sip.process_message(msg)

        assert len(messages) == 5
        assert all(m.consolidated_seq == i + 1 for i, m in enumerate(messages))

    def test_gap_detection_and_buffering(self):
        sip = SIPProcessor()
        messages = []
        sip.on_message(lambda m: messages.append(m))

        # Send seq 0, then skip to 2 (gap at 1)
        sip.process_message(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))
        sip.process_message(MarketDataMessage(
            exchange_id="NYSE", sequence_num=2, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50002.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))

        assert len(messages) == 1  # only seq 0 processed
        assert sip.stats.total_gaps_detected == 1

        # Fill the gap
        sip.process_message(MarketDataMessage(
            exchange_id="NYSE", sequence_num=1, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50001.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))

        assert len(messages) == 3  # seq 0, 1, 2 all processed now

    def test_duplicate_dropped(self):
        sip = SIPProcessor()

        sip.process_message(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))
        sip.process_message(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))

        assert sip.stats.total_duplicates_dropped == 1

    def test_multi_exchange_independent_sequences(self):
        sip = SIPProcessor()
        messages = []
        sip.on_message(lambda m: messages.append(m))

        # NYSE seq 0 and NASDAQ seq 0 are independent
        sip.process_message(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="AAPL", price=175.0, quantity=100.0, timestamp_ns=time.time_ns(),
        ))
        sip.process_message(MarketDataMessage(
            exchange_id="NASDAQ", sequence_num=0, message_type=MessageType.TRADE,
            symbol="AAPL", price=175.01, quantity=200.0, timestamp_ns=time.time_ns(),
        ))

        assert len(messages) == 2
        assert messages[0].consolidated_seq == 1
        assert messages[1].consolidated_seq == 2

    def test_large_gap_buffer_overflow(self):
        sip = SIPProcessor(max_gap_buffer_size=5)

        # Send seq 0 then skip far ahead
        sip.process_message(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))

        # Buffer 6 messages (exceeds limit of 5)
        for i in range(2, 8):
            sip.process_message(MarketDataMessage(
                exchange_id="NYSE", sequence_num=i, message_type=MessageType.TRADE,
                symbol="BTC-USD", price=50000.0 + i, quantity=1.0, timestamp_ns=time.time_ns(),
            ))

        # Should have force-skipped and drained buffer
        assert sip.stats.total_messages_processed > 1

    def test_stats_tracking(self):
        sip = SIPProcessor()
        for i in range(100):
            sip.process_message(MarketDataMessage(
                exchange_id="NYSE", sequence_num=i, message_type=MessageType.TRADE,
                symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
            ))

        stats = sip.get_stats()
        assert stats["total_messages_processed"] == 100
        assert stats["bytes_processed"] == 100 * 200
        assert stats["avg_latency_ns"] > 0


class TestNBBOCalculator:
    def test_single_venue(self):
        calc = NBBOCalculator()
        nbbo = calc.update_quote(VenueQuote(
            exchange_id="NYSE", symbol="AAPL",
            bid_price=175.00, ask_price=175.05,
            bid_size=1000.0, ask_size=500.0,
            timestamp_ns=time.time_ns(),
        ))
        assert nbbo.best_bid == 175.00
        assert nbbo.best_ask == 175.05
        assert nbbo.spread == pytest.approx(0.05, abs=0.001)

    def test_best_across_venues(self):
        calc = NBBOCalculator()

        calc.update_quote(VenueQuote(
            exchange_id="NYSE", symbol="AAPL",
            bid_price=175.00, ask_price=175.10,
            bid_size=1000.0, ask_size=500.0,
            timestamp_ns=time.time_ns(),
        ))
        nbbo = calc.update_quote(VenueQuote(
            exchange_id="NASDAQ", symbol="AAPL",
            bid_price=175.02, ask_price=175.05,
            bid_size=2000.0, ask_size=300.0,
            timestamp_ns=time.time_ns(),
        ))

        # Best bid from NASDAQ (175.02), best ask from NASDAQ (175.05)
        assert nbbo.best_bid == 175.02
        assert nbbo.best_bid_exchange == "NASDAQ"
        assert nbbo.best_ask == 175.05
        assert nbbo.best_ask_exchange == "NASDAQ"

    def test_get_nbbo_dict(self):
        calc = NBBOCalculator()
        calc.update_quote(VenueQuote(
            exchange_id="NYSE", symbol="BTC-USD",
            bid_price=50000.0, ask_price=50010.0,
            bid_size=1.0, ask_size=2.0,
            timestamp_ns=time.time_ns(),
        ))

        result = calc.get_nbbo("BTC-USD")
        assert result is not None
        assert result["best_bid"] == 50000.0
        assert result["spread"] == pytest.approx(10.0, abs=0.01)

    def test_unknown_symbol(self):
        calc = NBBOCalculator()
        assert calc.get_nbbo("UNKNOWN") is None


class TestMarketDataFeedManager:
    def test_direct_subscriber_receives_messages(self):
        manager = MarketDataFeedManager()
        received = []
        manager.subscribe(FeedTier.DIRECT, lambda m: received.append(m))

        manager.ingest(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))

        assert len(received) == 1
        assert received[0].symbol == "BTC-USD"

    def test_symbol_filtering(self):
        manager = MarketDataFeedManager()
        received = []
        manager.subscribe(FeedTier.DIRECT, lambda m: received.append(m), symbols={"ETH-USD"})

        manager.ingest(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))
        manager.ingest(MarketDataMessage(
            exchange_id="NYSE", sequence_num=1, message_type=MessageType.TRADE,
            symbol="ETH-USD", price=3000.0, quantity=10.0, timestamp_ns=time.time_ns(),
        ))

        assert len(received) == 1
        assert received[0].symbol == "ETH-USD"

    def test_standard_subscriber(self):
        manager = MarketDataFeedManager()
        received = []
        manager.subscribe(FeedTier.STANDARD, lambda m: received.append(m))

        manager.ingest(MarketDataMessage(
            exchange_id="NASDAQ", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))

        assert len(received) == 1

    def test_quote_updates_nbbo(self):
        manager = MarketDataFeedManager()

        manager.ingest(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.QUOTE,
            symbol="BTC-USD", price=0, quantity=0,
            bid_price=50000.0, ask_price=50010.0,
            bid_size=1.0, ask_size=2.0,
            timestamp_ns=time.time_ns(),
        ))

        nbbo = manager.get_nbbo("BTC-USD")
        assert nbbo is not None
        assert nbbo["best_bid"] == 50000.0

    def test_feed_stats(self):
        manager = MarketDataFeedManager()

        for i in range(50):
            manager.ingest(MarketDataMessage(
                exchange_id="NYSE", sequence_num=i, message_type=MessageType.TRADE,
                symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
            ))

        stats = manager.get_feed_stats()
        assert stats["total_messages_processed"] == 50
        assert stats["bytes_processed"] == 50 * 200

    def test_multi_tier_delivery(self):
        manager = MarketDataFeedManager()
        direct = []
        standard = []

        manager.subscribe(FeedTier.DIRECT, lambda m: direct.append(m))
        manager.subscribe(FeedTier.STANDARD, lambda m: standard.append(m))

        manager.ingest(MarketDataMessage(
            exchange_id="NYSE", sequence_num=0, message_type=MessageType.TRADE,
            symbol="BTC-USD", price=50000.0, quantity=1.0, timestamp_ns=time.time_ns(),
        ))

        assert len(direct) == 1
        assert len(standard) == 1
