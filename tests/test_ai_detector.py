"""Tests for AI Anomaly Detector (rule-based path — no API key needed)."""

import time
import pytest
from exchange.ai_detector import AIAnomalyDetector, AnomalyAlert


class TestRuleBasedDetection:
    def setup_method(self):
        self.halts = []
        self.detector = AIAnomalyDetector(
            halt_callback=lambda reason: self.halts.append(reason),
        )

    def test_no_alert_normal_trading(self):
        for i in range(10):
            self.detector.record_trade("BTC-USD", 100.0 + i * 0.1, 1.0, f"buyer{i}", f"seller{i}")
            alert = self.detector.check_rules("BTC-USD")
            assert alert is None

    def test_flash_crash_detection(self):
        # Normal price
        for _ in range(5):
            self.detector.record_trade("BTC-USD", 100.0, 1.0, "a", "b")

        # Sudden crash
        self.detector.record_trade("BTC-USD", 90.0, 1.0, "a", "b")
        alert = self.detector.check_rules("BTC-USD")

        assert alert is not None
        assert alert.alert_type == "flash_crash"
        assert alert.severity == "CRITICAL"
        assert len(self.halts) == 1

    def test_spoofing_detection(self):
        now = time.time()

        # Simulate a client placing many orders then cancelling most
        # record_cancel uses time.time() so we inject directly for determinism
        for i in range(15):
            self.detector._orders["BTC-USD"].append((now, "spoofer", "BUY"))
            self.detector._prices["BTC-USD"].append((now, 100.0))
        for i in range(13):
            self.detector._cancels["BTC-USD"].append((now, "spoofer"))

        alert = self.detector.check_rules("BTC-USD")
        assert alert is not None
        assert alert.alert_type == "spoofing"

    def test_pump_detection(self):
        base_time = time.time()
        # Concentrated buying driving price up >50%
        for i in range(15):
            price = 100.0 + i * 5.0  # 100 -> 170 (70% increase)
            self.detector._prices["ETH-USD"].append((base_time + i * 0.5, price))
            self.detector._orders["ETH-USD"].append((base_time + i * 0.5, "whale1", "BUY"))
            self.detector._volumes["ETH-USD"].append((base_time + i * 0.5, price * 10))

        alert = self.detector.check_rules("ETH-USD")
        assert alert is not None
        assert alert.alert_type == "pump_dump"
        assert alert.severity == "CRITICAL"

    def test_get_recent_alerts(self):
        for _ in range(5):
            self.detector.record_trade("BTC-USD", 100.0, 1.0, "a", "b")
        self.detector.record_trade("BTC-USD", 90.0, 1.0, "a", "b")
        self.detector.check_rules("BTC-USD")

        alerts = self.detector.get_recent_alerts()
        assert len(alerts) >= 1
