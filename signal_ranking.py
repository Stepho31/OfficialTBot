# signal_ranking.py
# Signal Ranking Engine: rank and prioritize trade opportunities by composite quality
# so the highest-quality trades execute first. Does not replace strategy or risk logic.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple, Any, Optional

logger = logging.getLogger(__name__)

# Weights for composite ranking (sum = 1.0). Output 0-100.
WEIGHT_BASE_SCORE = 0.40
WEIGHT_TREND_STRENGTH = 0.20
WEIGHT_MOMENTUM = 0.15
WEIGHT_VOLATILITY_QUALITY = 0.10
WEIGHT_CONFIRMATIONS = 0.10
WEIGHT_SESSION = 0.05
# Risk-reward uses remaining weight (0.10) so total = 1.0
WEIGHT_RR = 0.10

# Risk allocation multipliers by ranking score (applied after strategy scaling, before portfolio engine)
RISK_MULT_HIGH = 1.2   # ranking_score >= 85
RISK_MULT_NORMAL = 1.0 # 75 <= ranking_score < 85
RISK_MULT_LOW = 0.8    # 65 <= ranking_score < 75
# Below 65: use 0.8 (same as low band) so we don't increase risk for weak signals


def _trend_strength_score(opportunity: Any) -> float:
    """0-100: aligned trend = 100, neutral = 50, opposite = 0."""
    trend = (getattr(opportunity, "trend", None) or "").lower()
    direction = (getattr(opportunity, "direction", "") or "").lower()
    if not trend or not direction:
        return 50.0
    if (direction == "buy" and trend == "bullish") or (direction == "sell" and trend == "bearish"):
        return 100.0
    if trend == "neutral":
        return 50.0
    return 0.0


def _momentum_score(opportunity: Any) -> float:
    """0-100 from opportunity.momentum (dict with strength, short, medium)."""
    momentum = getattr(opportunity, "momentum", None) or {}
    if not isinstance(momentum, dict):
        return 50.0
    strength = momentum.get("strength", 0) or 0
    # strength often 0-1 or similar; scale to 0-100
    if isinstance(strength, (int, float)):
        return min(100.0, max(0.0, float(strength) * 100.0))
    return 50.0


def _volatility_quality_score(opportunity: Any) -> float:
    """0-100: prefer moderate ATR% (e.g. 0.5-1.5). Too low or too high = lower score."""
    vol = getattr(opportunity, "volatility", None)
    if vol is None:
        return 50.0
    try:
        v = float(vol)
    except (TypeError, ValueError):
        return 50.0
    if 0.5 <= v <= 1.5:
        return 100.0
    if 0.3 <= v <= 2.0:
        return 80.0
    if 0.2 <= v <= 2.5:
        return 60.0
    return max(0.0, 50.0 - abs(v - 1.0) * 20.0)


def _confirmations_score(opportunity: Any) -> float:
    """0-100 from number of reasons (confirmations). Cap at 8 reasons = 100."""
    reasons = getattr(opportunity, "reasons", None) or []
    n = len(reasons) if isinstance(reasons, list) else 0
    if n >= 8:
        return 100.0
    if n >= 4:
        return 50.0 + (n - 4) * 12.5  # 4->50, 5->62.5, 6->75, 7->87.5
    return n * 12.5  # 0->0, 1->12.5, 2->25, 3->37.5


def _session_score(opportunity: Any) -> float:
    """0-100 from session_strength (typically 0-1)."""
    s = getattr(opportunity, "session_strength", None)
    if s is None:
        return 50.0
    try:
        x = float(s)
    except (TypeError, ValueError):
        return 50.0
    return min(100.0, max(0.0, x * 100.0))


def _risk_reward_score(opportunity: Any) -> float:
    """0-100 from suggested R:R. 1.5R = 75, 2R = 100, 1R = 50."""
    entry = getattr(opportunity, "entry_price", None)
    sl = getattr(opportunity, "suggested_sl", None)
    tp = getattr(opportunity, "suggested_tp", None)
    direction = (getattr(opportunity, "direction", "") or "").lower()
    if entry is None or sl is None or tp is None:
        return 50.0
    try:
        entry, sl, tp = float(entry), float(sl), float(tp)
    except (TypeError, ValueError):
        return 50.0
    if direction == "buy":
        risk = entry - sl
        reward = tp - entry
    else:
        risk = sl - entry
        reward = entry - tp
    if risk <= 0:
        return 50.0
    rr = reward / risk
    if rr >= 2.0:
        return 100.0
    if rr >= 1.5:
        return 75.0
    if rr >= 1.0:
        return 50.0 + (rr - 1.0) * 50.0  # 1.0->50, 1.25->62.5, 1.5->75
    return max(0.0, rr * 50.0)


def compute_ranking_score(opportunity: Any) -> Tuple[float, Dict[str, float]]:
    """
    Compute composite ranking score 0-100 and component breakdown.
    Uses: base score, trend strength, momentum, volatility quality, confirmations, session, R:R.
    """
    base = min(100.0, max(0.0, float(getattr(opportunity, "score", 0) or 0)))
    trend = _trend_strength_score(opportunity)
    momentum = _momentum_score(opportunity)
    vol_quality = _volatility_quality_score(opportunity)
    confirmations = _confirmations_score(opportunity)
    session = _session_score(opportunity)
    rr = _risk_reward_score(opportunity)

    ranking = (
        WEIGHT_BASE_SCORE * base
        + WEIGHT_TREND_STRENGTH * trend
        + WEIGHT_MOMENTUM * momentum
        + WEIGHT_VOLATILITY_QUALITY * vol_quality
        + WEIGHT_CONFIRMATIONS * confirmations
        + WEIGHT_SESSION * session
        + WEIGHT_RR * rr
    )
    ranking = min(100.0, max(0.0, ranking))
    components = {
        "base_score": base,
        "trend_strength": trend,
        "momentum": momentum,
        "volatility_quality": vol_quality,
        "confirmations": confirmations,
        "session_score": session,
        "risk_reward_score": rr,
    }
    return round(ranking, 2), components


def rank_and_sort_opportunities(
    opportunities: List[Any],
) -> List[Tuple[Any, float, Dict[str, float]]]:
    """
    Rank all opportunities and return list of (opportunity, ranking_score, components)
    sorted by ranking_score descending (highest first).
    """
    if not opportunities:
        return []
    scored = []
    for opp in opportunities:
        ranking_score, components = compute_ranking_score(opp)
        scored.append((opp, ranking_score, components))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def get_risk_multiplier_by_ranking(ranking_score: float) -> float:
    """
    Return risk multiplier for position sizing based on ranking.
    ≥85: 1.2, 75-84: 1.0, 65-74: 0.8. Below 65: 0.8.
    Result is applied to risk_pct before portfolio engine (which still enforces cap).
    """
    if ranking_score >= 85:
        return RISK_MULT_HIGH
    if ranking_score >= 75:
        return RISK_MULT_NORMAL
    if ranking_score >= 65:
        return RISK_MULT_LOW
    return RISK_MULT_LOW


def log_ranking_decision(
    selected_symbol: str,
    selected_direction: str,
    selected_ranking_score: float,
    selected_base_score: float,
    reason: str,
    skipped: List[Tuple[str, str, float]],
) -> None:
    """Structured log for ranking decision (e.g. why this trade was chosen over others)."""
    msg = (
        f"[RANKING] Selected {selected_symbol} {selected_direction.upper()} "
        f"(ranking_score={selected_ranking_score:.1f}, base_score={selected_base_score:.1f}). {reason}"
    )
    logger.info(msg)
    print(msg)
    if skipped:
        for sym, direction, rs in skipped[:5]:  # cap at 5 for readability
            print(f"[RANKING]   - Skipped {sym} {direction.upper()} (ranking_score={rs:.1f})")


# Correlation groups for correlation-aware ordering (prefer best in group)
CORRELATION_GROUPS = [
    {"name": "USD_MAJORS", "members": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]},
    {"name": "YEN_CROSSES", "members": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY"]},
]


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("_", "").replace("/", "")


def get_correlation_group(symbol: str) -> Optional[str]:
    """Return group name if symbol belongs to a correlation group."""
    sym = _normalize_symbol(symbol)
    for g in CORRELATION_GROUPS:
        if sym in g["members"]:
            return g["name"]
    return None


def apply_correlation_aware_order(
    ranked: List[Tuple[Any, float, Dict[str, float]]],
) -> List[Tuple[Any, float, Dict[str, float]]]:
    """
    Already sorted by ranking_score desc. When multiple are in same correlation group,
    they are already in best-first order. This is a no-op for ordering but can be used
    to log that we prefer the highest-ranked in each group. Return as-is.
    """
    return ranked
