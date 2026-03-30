"""
Integration tests for the REST API.
Uses FastAPI's TestClient for synchronous testing.
"""

import pytest
from fastapi.testclient import TestClient
from exchange.api import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class TestAPIEndpoints:
    def test_list_symbols(self, client):
        resp = client.get("/symbols")
        assert resp.status_code == 200
        data = resp.json()
        assert "BTC-USD" in data["symbols"]

    def test_submit_limit_order(self, client):
        resp = client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 1.0,
            "price": 50000.0,
            "client_id": "test_client",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "NEW"
        assert data["symbol"] == "BTC-USD"

    def test_submit_and_match(self, client):
        client.post("/orders", json={
            "symbol": "ETH-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 5.0,
            "price": 3000.0,
            "client_id": "buyer",
        })
        resp = client.post("/orders", json={
            "symbol": "ETH-USD",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 5.0,
            "price": 3000.0,
            "client_id": "seller",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "FILLED"

    def test_get_order(self, client):
        resp = client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 1.0,
            "price": 40000.0,
        })
        order_id = resp.json()["order_id"]
        resp2 = client.get(f"/orders/{order_id}")
        assert resp2.status_code == 200
        assert resp2.json()["order_id"] == order_id

    def test_cancel_order(self, client):
        resp = client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 1.0,
            "price": 40000.0,
        })
        order_id = resp.json()["order_id"]
        resp2 = client.delete(f"/orders/{order_id}")
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "CANCELLED"

    def test_order_book(self, client):
        client.post("/orders", json={
            "symbol": "SOL-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10.0,
            "price": 150.0,
        })
        resp = client.get("/book/SOL-USD")
        assert resp.status_code == 200
        data = resp.json()
        assert data["best_bid"] == 150.0

    def test_stats(self, client):
        resp = client.get("/stats")
        assert resp.status_code == 200
        assert "orders_processed" in resp.json()

    def test_risk_endpoint(self, client):
        resp = client.get("/risk")
        assert resp.status_code == 200

    def test_alerts_endpoint(self, client):
        resp = client.get("/alerts")
        assert resp.status_code == 200

    def test_halt_and_resume(self, client):
        resp = client.post("/admin/halt", json={"reason": "test"})
        assert resp.json()["status"] == "halted"

        resp = client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 1.0,
            "price": 50000.0,
        })
        assert resp.status_code == 400  # rejected

        resp = client.post("/admin/resume")
        assert resp.json()["status"] == "resumed"

    def test_unknown_symbol_book(self, client):
        resp = client.get("/book/FAKE-USD")
        assert resp.status_code == 404

    def test_invalid_order(self, client):
        resp = client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "INVALID",
            "order_type": "LIMIT",
            "quantity": 1.0,
            "price": 50000.0,
        })
        assert resp.status_code == 422  # validation error

    def test_l3_order_book(self, client):
        client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 1.0,
            "price": 40000.0,
            "client_id": "alice",
        })
        resp = client.get("/book/BTC-USD/l3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["bids"]) >= 1
        assert "orders" in data["bids"][0]

    def test_book_imbalance(self, client):
        client.post("/orders", json={
            "symbol": "DOGE-USD",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 100.0,
            "price": 0.10,
        })
        client.post("/orders", json={
            "symbol": "DOGE-USD",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 100.0,
            "price": 0.11,
        })
        resp = client.get("/book/DOGE-USD/imbalance")
        assert resp.status_code == 200
        data = resp.json()
        assert "imbalance_ratio" in data
        assert "interpretation" in data

    def test_ioc_order(self, client):
        resp = client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "BUY",
            "order_type": "IOC",
            "quantity": 1.0,
            "price": 50000.0,
        })
        assert resp.status_code == 200
        # IOC with no counterparty should be cancelled
        assert resp.json()["status"] == "CANCELLED"

    def test_fok_order(self, client):
        resp = client.post("/orders", json={
            "symbol": "BTC-USD",
            "side": "BUY",
            "order_type": "FOK",
            "quantity": 1.0,
            "price": 50000.0,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "CANCELLED"  # no liquidity
