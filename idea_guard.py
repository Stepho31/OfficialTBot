import os
import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

import oandapyV20
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.instruments as instruments

from validators import get_oanda_data

REGISTRY_FILE = "idea_registry.json"

# Environment-configurable parameters (with sensible defaults)
# Reduced cooldown to allow same-day re-entry when signals remain valid
COOLDOWN_HOURS = float(os.getenv("IDEA_COOLDOWN_HOURS", "6"))  # Reduced from 12 to 6 hours
COOLDOWN_ATR_MULT = float(os.getenv("IDEA_COOLDOWN_ATR_MULT", "0.8"))  # Reduced from 1.0 to 0.8
COOLDOWN_PCT_MOVE = float(os.getenv("IDEA_COOLDOWN_PCT_MOVE", "0.6"))  # Reduced from 0.8% to 0.6%
FRESHNESS_LOOKBACK_DAYS = int(os.getenv("FRESHNESS_LOOKBACK_DAYS", "14"))
FRESHNESS_SIMILARITY_THRESHOLD = float(os.getenv("FRESHNESS_SIMILARITY_THRESHOLD", "0.85"))
HTF_TREND_GRANULARITY = os.getenv("HTF_TREND_GRANULARITY", "D")  # Daily


def _load_registry() -> Dict:
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {"history": []}
        except (json.JSONDecodeError, FileNotFoundError):
            return {"history": []}
    return {"history": []}


def _save_registry(registry: Dict):
    try:
        with open(REGISTRY_FILE, "w") as f:
            json.dump(registry, f, indent=2)
    except Exception as e:
        print(f"[IDEA_GUARD] Error saving registry: {e}")


def _normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9\.\:_]+", _normalize_text(text))


def _jaccard_similarity(a_tokens: List[str], b_tokens: List[str]) -> float:
    if not a_tokens or not b_tokens:
        return 0.0
    set_a = set(a_tokens)
    set_b = set(b_tokens)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return float(intersection) / float(union) if union else 0.0


def _now_utc() -> datetime:
    return datetime.utcnow()


def _ensure_oanda_client(api_key=None, account_id=None) -> Tuple[Optional[oandapyV20.API], Optional[str]]:
    """Get OANDA client. Requires api_key and account_id to be provided explicitly or set in env (legacy mode)."""
    token = api_key or os.getenv("OANDA_API_KEY")
    account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
    if not token or not account_id:
        # Return None instead of raising - allows graceful degradation
        return None, None
    try:
        client = oandapyV20.API(access_token=token, environment="live")
        return client, account_id
    except Exception as e:
        print(f"[IDEA_GUARD] OANDA client init failed: {e}")
        return None, None


def format_instrument(symbol: str) -> str:
    """Return OANDA instrument format (XXX_YYY). Accepts 'EURUSD' or 'EUR_USD'."""
    symbol = symbol.upper().replace("/", "").strip()
    if "_" in symbol:
        parts = symbol.split("_")
        if len(parts) == 2 and len(parts[0]) == 3 and len(parts[1]) == 3:
            return f"{parts[0]}_{parts[1]}"
    if len(symbol) == 6:
        return f"{symbol[:3]}_{symbol[3:]}"
    # Metals and others might already be correct (e.g., XAU_USD)
    return symbol if "_" in symbol else symbol


def _get_current_price(instrument: str, side: str, api_key=None, account_id=None) -> Optional[float]:
    client, account_id = _ensure_oanda_client(api_key=api_key, account_id=account_id)
    if not client or not account_id:
        return None
    try:
        r = pricing.PricingInfo(accountID=account_id, params={"instruments": instrument})
        client.request(r)
        prices = r.response["prices"][0]
        bid = float(prices["bids"][0]["price"])
        ask = float(prices["asks"][0]["price"])
        return ask if side == "buy" else bid
    except Exception as e:
        print(f"[IDEA_GUARD] Error getting price for {instrument}: {e}")
        return None


def _calculate_atr_from_candles(candles: List[Dict]) -> Optional[float]:
    try:
        if len(candles) < 21:
            return None
        true_ranges = []
        for i in range(1, len(candles)):
            cur = candles[i]
            prev = candles[i - 1]
            high = float(cur["mid"]["h"]) if "mid" in cur else float(cur["mid"]["h"])
            low = float(cur["mid"]["l"]) if "mid" in cur else float(cur["mid"]["l"])
            prev_close = float(prev["mid"]["c"]) if "mid" in prev else float(prev["mid"]["c"])
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            true_ranges.append(max(tr1, tr2, tr3))
        multiplier = 2.0 / (21 + 1)
        ema_atr = true_ranges[0]
        for tr in true_ranges[1:]:
            ema_atr = (tr * multiplier) + (ema_atr * (1 - multiplier))
        return ema_atr
    except Exception:
        return None


def _get_h4_atr(instrument: str, api_key=None, account_id=None) -> Optional[float]:
    candles = get_oanda_data(instrument, "H4", 60, api_key=api_key, account_id=account_id)
    return _calculate_atr_from_candles(candles) if candles else None


def _get_daily_trend(instrument: str, api_key=None, account_id=None) -> Optional[str]:
    """Return 'bullish', 'bearish', or None based on EMA50 vs EMA200 on Daily."""
    try:
        candles = get_oanda_data(instrument, HTF_TREND_GRANULARITY, 210, api_key=api_key, account_id=account_id)
        if not candles or len(candles) < 200:
            return None
        closes = [float(c["mid"]["c"]) for c in candles]
        ema50 = _calculate_ema(closes[-50:], 50)
        ema200 = _calculate_ema(closes, 200)
        if ema50 and ema200:
            if ema50 > ema200 * 1.0005:
                return "bullish"
            elif ema50 < ema200 * 0.9995:
                return "bearish"
        return None
    except Exception as e:
        print(f"[IDEA_GUARD] Daily trend calc failed for {instrument}: {e}")
        return None


def _calculate_ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    mult = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = (v * mult) + (ema * (1 - mult))
    return ema


def _has_swing_break(instrument: str, direction: str, api_key=None, account_id=None) -> bool:
    """Check if price recently broke prior 20-bar swing high/low on H4."""
    try:
        candles = get_oanda_data(instrument, "H4", 60, api_key=api_key, account_id=account_id)
        if not candles or len(candles) < 25:
            return False
        highs = [float(c["mid"]["h"]) for c in candles]
        lows = [float(c["mid"]["l"]) for c in candles]
        closes = [float(c["mid"]["c"]) for c in candles]
        prior_high = max(highs[-25:-5])  # prior swing excluding last 5 bars
        prior_low = min(lows[-25:-5])
        last_close = closes[-1]
        if direction == "buy":
            return last_close > prior_high
        else:
            return last_close < prior_low
    except Exception as e:
        print(f"[IDEA_GUARD] Swing break check failed for {instrument}: {e}")
        return False


def _break_and_retest(instrument: str, direction: str, api_key=None, account_id=None) -> bool:
    """Simple break-and-retest heuristic on H4."""
    try:
        candles = get_oanda_data(instrument, "H4", 80, api_key=api_key, account_id=account_id)
        if not candles or len(candles) < 40:
            return False
        highs = [float(c["mid"]["h"]) for c in candles]
        lows = [float(c["mid"]["l"]) for c in candles]
        closes = [float(c["mid"]["c"]) for c in candles]
        atr = _calculate_atr_from_candles(candles)
        tol = atr * 0.3 if atr else 0
        prior_high = max(highs[-40:-15])
        prior_low = min(lows[-40:-15])
        recent_high = max(highs[-15:])
        recent_low = min(lows[-15:])
        if direction == "buy":
            broke = recent_high > prior_high
            retested = any(abs(l - prior_high) <= tol for l in lows[-10:]) if tol else any(l <= prior_high for l in lows[-10:])
            return broke and retested and closes[-1] > prior_high
        else:
            broke = recent_low < prior_low
            retested = any(abs(h - prior_low) <= tol for h in highs[-10:]) if tol else any(h >= prior_low for h in highs[-10:])
            return broke and retested and closes[-1] < prior_low
    except Exception as e:
        print(f"[IDEA_GUARD] Break-retest check failed for {instrument}: {e}")
        return False


def _last_trade_for_symbol(registry: Dict, instrument: str, direction: str) -> Optional[Dict]:
    history = registry.get("history", [])
    instrument_clean = instrument.replace("_", "")
    for entry in reversed(history):
        if entry.get("symbol_clean") == instrument_clean and entry.get("direction") == direction:
            return entry
    return None


def filter_fresh_ideas_by_registry(ideas: List[Dict]) -> List[Dict]:
    """Filter out ideas that are near-duplicates of previously traded ideas (global)."""
    registry = _load_registry()
    history = registry.get("history", [])
    if not history:
        return ideas
    kept: List[Dict] = []
    for idea in ideas:
        text = idea.get("description", "")
        new_tokens = _tokenize(text)
        if not new_tokens:
            kept.append(idea)
            continue
        is_duplicate = False
        for prev in history[-500:]:  # limit comparisons for speed
            prev_tokens = prev.get("idea_tokens", [])
            sim = _jaccard_similarity(new_tokens, prev_tokens)
            if sim >= FRESHNESS_SIMILARITY_THRESHOLD:
                is_duplicate = True
                print(f"[IDEA_GUARD] ❌ Idea filtered as stale (similarity {sim:.2f})")
                break
        if not is_duplicate:
            kept.append(idea)
    return kept


def evaluate_trade_gate(symbol: str, direction: str, idea_text: str, api_key=None, account_id=None) -> Dict:
    """Evaluate whether a trade should be blocked based on cooldown, freshness, and structure.
    Returns { 'allow': bool, 'blocks': [reasons] }.
    """
    instrument = format_instrument(symbol)
    blocks: List[str] = []

    # Freshness check (per symbol/direction within lookback)
    registry = _load_registry()
    idea_tokens = _tokenize(idea_text)
    cutoff_time = _now_utc() - timedelta(days=FRESHNESS_LOOKBACK_DAYS)
    for prev in reversed(registry.get("history", [])):
        try:
            prev_time = datetime.fromisoformat(prev.get("timestamp", ""))
        except Exception:
            continue
        if prev_time < cutoff_time:
            break
        if prev.get("direction") != direction:
            continue
        if prev.get("symbol_clean") != instrument.replace("_", ""):
            continue
        sim = _jaccard_similarity(idea_tokens, prev.get("idea_tokens", []))
        if sim >= FRESHNESS_SIMILARITY_THRESHOLD:
            blocks.append(f"STALE_IDEA(similarity={sim:.2f})")
            break

    # Cooldown check (time and price movement)
    last = _last_trade_for_symbol(registry, instrument, direction)
    if last is not None:
        try:
            last_time = datetime.fromisoformat(last.get("timestamp", ""))
        except Exception:
            last_time = None
        time_ok = False
        if last_time:
            hours_since = (_now_utc() - last_time).total_seconds() / 3600.0
            time_ok = hours_since >= COOLDOWN_HOURS
            if not time_ok:
                blocks.append(f"COOLDOWN_TIME({hours_since:.1f}h<{COOLDOWN_HOURS}h)")
        price_ok = False
        current_price = _get_current_price(instrument, direction, api_key=api_key, account_id=account_id)
        if current_price is not None:
            last_entry = float(last.get("entry_price", 0) or 0)
            if last_entry > 0:
                pct_move = abs(current_price - last_entry) / last_entry * 100.0
                atr = _get_h4_atr(instrument, api_key=api_key, account_id=account_id)
                atr_move_ok = (abs(current_price - last_entry) >= (atr * COOLDOWN_ATR_MULT)) if atr else False
                pct_ok = pct_move >= COOLDOWN_PCT_MOVE
                price_ok = atr_move_ok or pct_ok
                if not price_ok:
                    detail = f"pct={pct_move:.2f}%<={COOLDOWN_PCT_MOVE:.2f}%"
                    if atr:
                        detail += f", atr_move={'ok' if atr_move_ok else 'no'}"
                    blocks.append(f"COOLDOWN_PRICE({detail})")
        # Require BOTH time and price movement to be satisfied
        if not (time_ok and price_ok):
            pass

       # ---- Structure confirmation: SOFT TAGS ONLY ----
    structure_checks = []
    daily_trend = _get_daily_trend(instrument, api_key=api_key, account_id=account_id)
    if daily_trend:
        if (direction == "buy" and daily_trend == "bullish") or (direction == "sell" and daily_trend == "bearish"):
            structure_checks.append("HTF_TREND")
    if _has_swing_break(instrument, direction, api_key=api_key, account_id=account_id):
        structure_checks.append("SWING_BREAK")
    if _break_and_retest(instrument, direction, api_key=api_key, account_id=account_id):
        structure_checks.append("BREAK_RETEST")

    # Do NOT block if structure_checks is empty; just tag it
    tags = []
    if len(structure_checks) == 0:
        tags.append("IDEA_STRUCTURE_NOT_CONFIRMED")

    # ---- Decide allow/deny (HARD ONLY: cooldown + stale) ----
    allow = not any(b.startswith("COOLDOWN_") for b in blocks) and not any(b.startswith("STALE_IDEA") for b in blocks)

    # Specifically enforce: if last trade exists and any cooldown blocks, deny
    last = _last_trade_for_symbol(registry, instrument, direction)
    if last is not None and any(b.startswith("COOLDOWN_") for b in blocks):
        allow = False

    # NOTE: Structure is soft — never force allow=False here.

    return {"allow": allow, "blocks": blocks, "structure": structure_checks, "tags": tags}



def record_executed_idea(symbol: str, direction: str, idea_text: str, entry_price: float):
    """Record an executed trade idea in the registry for future freshness/cooldown checks."""
    registry = _load_registry()
    history = registry.setdefault("history", [])
    instrument = format_instrument(symbol)
    entry = {
        "timestamp": _now_utc().isoformat(),
        "symbol": instrument,
        "symbol_clean": instrument.replace("_", ""),
        "direction": direction,
        "idea_tokens": _tokenize(idea_text),
        "entry_price": float(entry_price),
    }
    history.append(entry)
    _save_registry(registry)
    print(f"[IDEA_GUARD] 📌 Recorded idea for {instrument} {direction.upper()} at {entry_price}")