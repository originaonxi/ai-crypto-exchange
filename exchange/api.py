"""
REST API & WebSocket server for the exchange.

- POST /orders — submit new order
- DELETE /orders/{id} — cancel order
- GET /orders/{id} — get order status
- GET /book/{symbol} — order book snapshot
- GET /stats — engine statistics
- GET /risk — risk management summary
- GET /alerts — AI anomaly alerts
- WS /ws/trades — real-time trade stream
- WS /ws/book/{symbol} — real-time order book updates
- POST /admin/halt — halt trading
- POST /admin/resume — resume trading
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from exchange.ai_detector import AIAnomalyDetector
from exchange.matching_engine import MatchingEngine
from exchange.order_book import (
    Execution,
    Order,
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    Side,
)
from exchange.risk_manager import RiskLimits, RiskManager
from exchange.wal import WAL

logger = logging.getLogger(__name__)


# --- Request/Response Models ---

class OrderRequest(BaseModel):
    symbol: str
    side: str = Field(..., pattern="^(BUY|SELL)$")
    order_type: str = Field(..., pattern="^(LIMIT|MARKET|IOC|FOK)$")
    quantity: float = Field(..., gt=0)
    price: float = Field(0.0, ge=0)
    client_id: str = "anonymous"


class OrderResponse(BaseModel):
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: float
    remaining_quantity: float
    status: str
    timestamp_ns: int


class HaltRequest(BaseModel):
    reason: str


# --- WebSocket Manager ---

class ConnectionManager:
    def __init__(self):
        self.trade_connections: list[WebSocket] = []
        self.book_connections: dict[str, list[WebSocket]] = {}

    async def connect_trades(self, ws: WebSocket):
        await ws.accept()
        self.trade_connections.append(ws)

    async def connect_book(self, ws: WebSocket, symbol: str):
        await ws.accept()
        if symbol not in self.book_connections:
            self.book_connections[symbol] = []
        self.book_connections[symbol].append(ws)

    def disconnect_trades(self, ws: WebSocket):
        self.trade_connections.remove(ws)

    def disconnect_book(self, ws: WebSocket, symbol: str):
        if symbol in self.book_connections:
            self.book_connections[symbol].remove(ws)

    async def broadcast_trade(self, execution: dict):
        dead = []
        for ws in self.trade_connections:
            try:
                await ws.send_json(execution)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.trade_connections.remove(ws)

    async def broadcast_book(self, symbol: str, snapshot: dict):
        dead = []
        for ws in self.book_connections.get(symbol, []):
            try:
                await ws.send_json(snapshot)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.book_connections[symbol].remove(ws)


# --- Globals ---
ws_manager = ConnectionManager()
engine: Optional[MatchingEngine] = None
risk_mgr: Optional[RiskManager] = None
ai_detector: Optional[AIAnomalyDetector] = None
wal: Optional[WAL] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _execution_to_dict(exe: Execution) -> dict:
    return {
        "execution_id": exe.execution_id,
        "symbol": exe.symbol,
        "price": exe.price,
        "quantity": exe.quantity,
        "buyer_order_id": exe.buyer_order_id,
        "seller_order_id": exe.seller_order_id,
        "aggressor_side": exe.aggressor_side.value,
        "timestamp_ns": exe.timestamp_ns,
    }


def _snapshot_to_dict(snap: OrderBookSnapshot) -> dict:
    return {
        "symbol": snap.symbol,
        "bids": [{"price": l.price, "quantity": l.total_quantity, "orders": l.order_count} for l in snap.bids],
        "asks": [{"price": l.price, "quantity": l.total_quantity, "orders": l.order_count} for l in snap.asks],
        "best_bid": snap.best_bid,
        "best_ask": snap.best_ask,
        "spread": snap.spread,
        "mid_price": snap.mid_price,
        "imbalance_ratio": snap.imbalance_ratio if snap.imbalance_ratio != float('inf') else None,
        "total_bid_quantity": snap.total_bid_quantity,
        "total_ask_quantity": snap.total_ask_quantity,
        "timestamp_ns": snap.timestamp_ns,
    }


def _order_to_response(order: Order) -> OrderResponse:
    return OrderResponse(
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side.value,
        order_type=order.order_type.value,
        quantity=order.quantity,
        price=order.price,
        remaining_quantity=order.remaining_quantity,
        status=order.status.value,
        timestamp_ns=order.timestamp_ns,
    )


# --- Callbacks from engine (sync, called from engine thread) ---

def _on_execution(exe: Execution):
    if risk_mgr:
        risk_mgr.post_trade_update(exe)
    if ai_detector:
        ai_detector.record_trade(exe.symbol, exe.price, exe.quantity, exe.buyer_client_id, exe.seller_client_id)
        ai_detector.check_rules(exe.symbol)
    if _event_loop:
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast_trade(_execution_to_dict(exe)),
            _event_loop,
        )


def _on_book_update(snap: OrderBookSnapshot):
    if _event_loop:
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast_book(snap.symbol, _snapshot_to_dict(snap)),
            _event_loop,
        )


# --- App Setup ---

SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "ADA-USD"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, risk_mgr, ai_detector, wal, _event_loop

    _event_loop = asyncio.get_event_loop()

    wal = WAL("data/wal.log")
    wal.open()

    risk_mgr = RiskManager(
        limits=RiskLimits(),
        halt_callback=lambda reason: engine.halt_trading(reason) if engine else None,
    )

    def risk_check(order: Order, executions: list[Execution]) -> bool:
        ok, reason = risk_mgr.pre_trade_check(order)
        if not ok:
            logger.warning(f"Risk rejection: {reason}")
        return ok

    engine = MatchingEngine(symbols=SYMBOLS, wal=wal, risk_check=risk_check, imbalance_check_enabled=False)
    engine.on_execution(_on_execution)
    engine.on_book_update(_on_book_update)

    ai_detector = AIAnomalyDetector(
        halt_callback=lambda reason: engine.halt_trading(reason),
    )

    logger.info(f"Exchange started with symbols: {SYMBOLS}")
    yield

    wal.close()
    logger.info("Exchange shut down")


app = FastAPI(
    title="AI-Enhanced Crypto Exchange",
    description="LMAX Disruptor-inspired order matching with Claude-powered anomaly detection",
    version="1.0.0",
    lifespan=lifespan,
)


# --- REST Endpoints ---

@app.post("/orders", response_model=OrderResponse)
async def submit_order(req: OrderRequest):
    order = Order(
        order_id=uuid4().hex[:16],
        symbol=req.symbol,
        side=Side(req.side),
        order_type=OrderType(req.order_type),
        quantity=req.quantity,
        price=req.price,
        timestamp_ns=time.time_ns(),
        client_id=req.client_id,
    )
    result = engine.submit_order(order)
    if result.status == OrderStatus.REJECTED:
        raise HTTPException(status_code=400, detail=f"Order rejected: {result.order_id}")
    return _order_to_response(result)


@app.delete("/orders/{order_id}")
async def cancel_order(order_id: str):
    result = engine.cancel_order(order_id)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found or already filled/cancelled")
    return _order_to_response(result)


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str):
    order = engine.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return _order_to_response(order)


@app.get("/book/{symbol}")
async def get_order_book(symbol: str, depth: int = 20):
    snap = engine.get_book_snapshot(symbol, depth)
    if not snap:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")
    return _snapshot_to_dict(snap)


@app.get("/book/{symbol}/l3")
async def get_order_book_l3(symbol: str, depth: int = 10):
    """L3 market data — individual orders visible at each price level."""
    snap = engine.get_l3_snapshot(symbol, depth)
    if not snap:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")
    return {
        "symbol": snap.symbol,
        "bids": [
            {"price": l.price, "orders": [
                {"order_id": o.order_id, "quantity": o.quantity, "timestamp_ns": o.timestamp_ns}
                for o in l.orders
            ]}
            for l in snap.bids
        ],
        "asks": [
            {"price": l.price, "orders": [
                {"order_id": o.order_id, "quantity": o.quantity, "timestamp_ns": o.timestamp_ns}
                for o in l.orders
            ]}
            for l in snap.asks
        ],
        "timestamp_ns": snap.timestamp_ns,
    }


@app.get("/book/{symbol}/imbalance")
async def get_book_imbalance(symbol: str, levels: int = 5):
    """Book imbalance ratio — key Flash Crash indicator."""
    imbalance = engine.get_book_imbalance(symbol, levels)
    if imbalance is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")
    return {
        "symbol": symbol,
        "imbalance_ratio": round(imbalance, 4),
        "interpretation": "balanced" if 0.3 <= imbalance <= 0.7
            else "heavy_buy_pressure" if imbalance > 0.7
            else "heavy_sell_pressure",
        "levels_analyzed": levels,
    }


@app.get("/stats")
async def get_stats():
    s = engine.stats
    return {
        "orders_processed": s.orders_processed,
        "orders_matched": s.orders_matched,
        "orders_rejected": s.orders_rejected,
        "orders_cancelled": s.orders_cancelled,
        "total_executions": s.total_executions,
        "total_volume": s.total_volume,
        "avg_latency_us": round(s.avg_latency_us, 2),
        "halted": engine.halted,
        "halt_reason": engine.halt_reason,
    }


@app.get("/risk")
async def get_risk():
    return risk_mgr.get_risk_summary()


@app.get("/risk/{client_id}")
async def get_client_risk(client_id: str):
    positions = risk_mgr.get_client_positions(client_id)
    return {
        symbol: {
            "quantity": p.quantity,
            "avg_price": p.avg_price,
            "realized_pnl": p.realized_pnl,
        }
        for symbol, p in positions.items()
    }


@app.get("/alerts")
async def get_alerts(limit: int = 50):
    alerts = ai_detector.get_recent_alerts(limit)
    return [asdict(a) for a in alerts]


@app.post("/analyze/{symbol}")
async def trigger_analysis(symbol: str):
    """Trigger on-demand Claude AI analysis of a symbol."""
    if symbol not in SYMBOLS:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")
    alert = await ai_detector.deep_analysis(symbol)
    if alert:
        return asdict(alert)
    return {"status": "no_anomaly_detected"}


@app.post("/admin/halt")
async def halt_trading(req: HaltRequest):
    engine.halt_trading(req.reason)
    return {"status": "halted", "reason": req.reason}


@app.post("/admin/resume")
async def resume_trading():
    engine.resume_trading()
    return {"status": "resumed"}


@app.get("/symbols")
async def list_symbols():
    return {"symbols": SYMBOLS}


# --- WebSocket Endpoints ---

@app.websocket("/ws/trades")
async def ws_trades(websocket: WebSocket):
    await ws_manager.connect_trades(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_manager.disconnect_trades(websocket)


@app.websocket("/ws/book/{symbol}")
async def ws_book(websocket: WebSocket, symbol: str):
    await ws_manager.connect_book(websocket, symbol)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect_book(websocket, symbol)
