"""
AI-Powered Settlement Analytics — Claude-driven predictive fail detection.

Uses Anthropic's API to analyze settlement patterns and predict fails 24 hours
ahead. Processes member history, position concentrations, and market conditions
to generate actionable risk assessments.

Example output:
"Based on Member X's historical patterns and current positions,
 73% probability of settlement fail on TSLA position."

Cost: ~$0.003/1K tokens → $200/month for 100K analyses.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FailPrediction:
    """AI-generated settlement fail prediction."""
    prediction_id: str
    member_id: str
    symbol: str
    probability: float  # 0.0 to 1.0
    risk_level: str  # LOW, MEDIUM, HIGH, CRITICAL
    reasoning: str
    recommended_action: str
    position_size: float
    historical_fail_rate: float
    market_volatility: float
    confidence: float
    timestamp_ns: int = 0


@dataclass
class SettlementPattern:
    """Tracked settlement behavior for a member."""
    total_settlements: int = 0
    total_fails: int = 0
    consecutive_fails: int = 0
    avg_settlement_delay_hours: float = 0.0
    securities_concentration: dict[str, float] = field(default_factory=dict)
    last_fail_timestamp: int = 0
    fail_by_symbol: dict[str, int] = field(default_factory=dict)


class SettlementPredictor:
    """
    AI-powered settlement fail predictor using Claude API.

    Analyzes member behavior patterns, position concentrations, and market
    conditions to predict settlement failures before they happen.
    """

    def __init__(
        self,
        anthropic_api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
        high_risk_threshold: float = 0.7,
        critical_risk_threshold: float = 0.9,
    ):
        self.api_key = anthropic_api_key
        self.model = model
        self.high_risk_threshold = high_risk_threshold
        self.critical_risk_threshold = critical_risk_threshold

        # Member pattern tracking
        self.member_patterns: dict[str, SettlementPattern] = defaultdict(SettlementPattern)

        # Prediction history
        self.predictions: deque[FailPrediction] = deque(maxlen=10000)

        # Anthropic client (lazy init)
        self._client = None

    def _get_client(self):
        """Lazy-initialize Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
            except ImportError:
                logger.warning("anthropic package not installed — AI predictions will use heuristic fallback")
                return None
        return self._client

    # --- Pattern Tracking ---

    def record_settlement(self, member_id: str, symbol: str, success: bool, delay_hours: float = 0.0):
        """Record a settlement outcome for pattern learning."""
        pattern = self.member_patterns[member_id]
        pattern.total_settlements += 1

        if not success:
            pattern.total_fails += 1
            pattern.consecutive_fails += 1
            pattern.last_fail_timestamp = time.time_ns()
            pattern.fail_by_symbol[symbol] = pattern.fail_by_symbol.get(symbol, 0) + 1
        else:
            pattern.consecutive_fails = 0

        # Rolling average settlement delay
        n = pattern.total_settlements
        pattern.avg_settlement_delay_hours = (
            (pattern.avg_settlement_delay_hours * (n - 1) + delay_hours) / n
        )

    def record_position(self, member_id: str, symbol: str, position_value: float):
        """Track position concentration for a member."""
        pattern = self.member_patterns[member_id]
        pattern.securities_concentration[symbol] = position_value

    # --- Heuristic Predictor (no API needed) ---

    def predict_fail_heuristic(
        self,
        member_id: str,
        symbol: str,
        position_size: float,
        available_securities: float,
        market_volatility: float,
    ) -> FailPrediction:
        """
        Rule-based fail prediction — works without API key.
        Combines historical patterns with current position data.
        """
        pattern = self.member_patterns.get(member_id, SettlementPattern())

        # Factor 1: Historical fail rate (0.0 - 0.3 contribution)
        if pattern.total_settlements > 0:
            hist_fail_rate = pattern.total_fails / pattern.total_settlements
        else:
            hist_fail_rate = 0.05  # assume 5% for unknown members

        # Factor 2: Securities coverage (0.0 - 0.4 contribution)
        coverage_ratio = available_securities / max(position_size, 1e-9)
        coverage_risk = max(0, 1.0 - coverage_ratio)

        # Factor 3: Volatility impact (0.0 - 0.2 contribution)
        vol_risk = min(market_volatility / 2.0, 1.0)  # normalize, cap at 1

        # Factor 4: Consecutive fail momentum (0.0 - 0.1 contribution)
        momentum_risk = min(pattern.consecutive_fails * 0.03, 0.1)

        # Factor 5: Symbol-specific fail history
        symbol_fails = pattern.fail_by_symbol.get(symbol, 0)
        symbol_risk = min(symbol_fails * 0.02, 0.1)

        # Weighted combination
        probability = (
            hist_fail_rate * 0.3
            + coverage_risk * 0.4
            + vol_risk * 0.15
            + momentum_risk * 0.1
            + symbol_risk * 0.05
        )
        probability = min(max(probability, 0.0), 1.0)

        # Risk level classification
        if probability >= self.critical_risk_threshold:
            risk_level = "CRITICAL"
            action = "BLOCK new trades, issue immediate margin call, prepare forced buy-in"
        elif probability >= self.high_risk_threshold:
            risk_level = "HIGH"
            action = "Issue margin call, pre-arrange stock borrow, notify risk desk"
        elif probability >= 0.4:
            risk_level = "MEDIUM"
            action = "Monitor closely, consider increasing margin requirement"
        else:
            risk_level = "LOW"
            action = "Standard monitoring"

        reasoning = (
            f"Historical fail rate: {hist_fail_rate:.1%}, "
            f"Securities coverage: {coverage_ratio:.1%}, "
            f"Market volatility: {market_volatility:.2f}, "
            f"Consecutive fails: {pattern.consecutive_fails}, "
            f"Symbol-specific fails: {symbol_fails}"
        )

        prediction = FailPrediction(
            prediction_id=f"pred_{int(time.time())}_{member_id[:8]}",
            member_id=member_id,
            symbol=symbol,
            probability=round(probability, 4),
            risk_level=risk_level,
            reasoning=reasoning,
            recommended_action=action,
            position_size=position_size,
            historical_fail_rate=round(hist_fail_rate, 4),
            market_volatility=round(market_volatility, 4),
            confidence=0.75,  # heuristic confidence
            timestamp_ns=time.time_ns(),
        )

        self.predictions.append(prediction)
        return prediction

    # --- AI-Powered Predictor (Claude API) ---

    async def predict_fail_ai(
        self,
        member_id: str,
        symbol: str,
        position_size: float,
        available_securities: float,
        cash_balance: float,
        market_volatility: float,
        current_price: float,
        recent_price_change_pct: float,
    ) -> FailPrediction:
        """
        Claude-powered deep analysis of settlement fail risk.
        Provides nuanced reasoning beyond rule-based heuristics.
        """
        client = self._get_client()
        pattern = self.member_patterns.get(member_id, SettlementPattern())

        # Build context for Claude
        context = {
            "member_id": member_id,
            "symbol": symbol,
            "position_size": position_size,
            "available_securities": available_securities,
            "cash_balance": cash_balance,
            "coverage_ratio": available_securities / max(position_size, 1e-9),
            "market_volatility": market_volatility,
            "current_price": current_price,
            "recent_price_change_pct": recent_price_change_pct,
            "historical_fail_rate": (
                pattern.total_fails / max(pattern.total_settlements, 1)
            ),
            "consecutive_fails": pattern.consecutive_fails,
            "total_settlements": pattern.total_settlements,
            "symbol_specific_fails": pattern.fail_by_symbol.get(symbol, 0),
            "avg_settlement_delay_hours": pattern.avg_settlement_delay_hours,
            "position_concentration": pattern.securities_concentration,
        }

        prompt = f"""You are a settlement risk analyst at a Central Counterparty (CCP).
Analyze the following member's settlement risk and predict the probability of a settlement fail.

Member Settlement Data:
{json.dumps(context, indent=2)}

Provide your analysis as JSON with these fields:
- probability: float 0.0-1.0 (likelihood of settlement fail)
- risk_level: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
- reasoning: string (2-3 sentence explanation)
- recommended_action: string (specific actionable steps)
- confidence: float 0.0-1.0 (your confidence in this prediction)

Consider:
1. Securities coverage vs position size
2. Historical behavior patterns
3. Market volatility and recent price movements
4. Concentration risk
5. Cash adequacy for margin requirements

Respond with ONLY the JSON object, no markdown."""

        if client is None:
            # Fallback to heuristic
            return self.predict_fail_heuristic(
                member_id, symbol, position_size, available_securities, market_volatility
            )

        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text.strip()
            # Strip markdown fences if present
            if response_text.startswith("```"):
                response_text = response_text.split("\n", 1)[1]
                if response_text.endswith("```"):
                    response_text = response_text[:-3]

            result = json.loads(response_text)

            prediction = FailPrediction(
                prediction_id=f"ai_pred_{int(time.time())}_{member_id[:8]}",
                member_id=member_id,
                symbol=symbol,
                probability=float(result.get("probability", 0.5)),
                risk_level=result.get("risk_level", "MEDIUM"),
                reasoning=result.get("reasoning", "AI analysis"),
                recommended_action=result.get("recommended_action", "Monitor"),
                position_size=position_size,
                historical_fail_rate=context["historical_fail_rate"],
                market_volatility=market_volatility,
                confidence=float(result.get("confidence", 0.85)),
                timestamp_ns=time.time_ns(),
            )

            self.predictions.append(prediction)

            logger.info(
                f"AI prediction for {member_id}/{symbol}: "
                f"{prediction.probability:.1%} fail probability ({prediction.risk_level})"
            )

            return prediction

        except Exception as e:
            logger.error(f"AI prediction failed, falling back to heuristic: {e}")
            return self.predict_fail_heuristic(
                member_id, symbol, position_size, available_securities, market_volatility
            )

    # --- Batch Analysis ---

    async def analyze_all_members(
        self,
        members: dict[str, dict],  # member_id -> {symbol: position_size, ...}
        securities_inventory: dict[str, dict[str, float]],  # member_id -> {symbol: available}
        market_data: dict[str, dict],  # symbol -> {volatility, price, change_pct}
    ) -> list[FailPrediction]:
        """
        Batch analyze all members for settlement risk.
        Returns predictions sorted by probability (highest risk first).
        """
        predictions = []

        for member_id, positions in members.items():
            inventory = securities_inventory.get(member_id, {})

            for symbol, position_size in positions.items():
                if abs(position_size) < 1e-9:
                    continue

                available = inventory.get(symbol, 0.0)
                mkt = market_data.get(symbol, {})
                volatility = mkt.get("volatility", 0.5)

                prediction = self.predict_fail_heuristic(
                    member_id=member_id,
                    symbol=symbol,
                    position_size=abs(position_size),
                    available_securities=available,
                    market_volatility=volatility,
                )

                if prediction.probability >= 0.3:  # only report meaningful risk
                    predictions.append(prediction)

        predictions.sort(key=lambda p: p.probability, reverse=True)
        return predictions

    # --- Query ---

    def get_recent_predictions(self, limit: int = 50) -> list[dict]:
        recent = list(self.predictions)[-limit:]
        return [
            {
                "prediction_id": p.prediction_id,
                "member_id": p.member_id,
                "symbol": p.symbol,
                "probability": p.probability,
                "risk_level": p.risk_level,
                "reasoning": p.reasoning,
                "recommended_action": p.recommended_action,
                "confidence": p.confidence,
                "timestamp_ns": p.timestamp_ns,
            }
            for p in recent
        ]

    def get_high_risk_members(self) -> list[dict]:
        """Get members with recent high-risk predictions."""
        high_risk = {}
        for p in self.predictions:
            if p.probability >= self.high_risk_threshold:
                if p.member_id not in high_risk or p.timestamp_ns > high_risk[p.member_id].timestamp_ns:
                    high_risk[p.member_id] = p

        return [
            {
                "member_id": p.member_id,
                "highest_risk_symbol": p.symbol,
                "probability": p.probability,
                "risk_level": p.risk_level,
                "recommended_action": p.recommended_action,
            }
            for p in sorted(high_risk.values(), key=lambda x: x.probability, reverse=True)
        ]

    def get_member_pattern(self, member_id: str) -> Optional[dict]:
        pattern = self.member_patterns.get(member_id)
        if not pattern:
            return None
        return {
            "total_settlements": pattern.total_settlements,
            "total_fails": pattern.total_fails,
            "fail_rate": pattern.total_fails / max(pattern.total_settlements, 1),
            "consecutive_fails": pattern.consecutive_fails,
            "avg_settlement_delay_hours": pattern.avg_settlement_delay_hours,
            "fail_by_symbol": dict(pattern.fail_by_symbol),
            "position_concentration": dict(pattern.securities_concentration),
        }
