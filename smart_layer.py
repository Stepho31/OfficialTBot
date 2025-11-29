# smart_layer.py
from dataclasses import dataclass
from typing import Optional, Dict, Any

import os
from validators import passes_h4_hard_filters

# Reuse your existing signals from validators (keep validators as-is)
from validators import (
    is_forex_pair,
    get_momentum_signals,
    get_h4_trend_adx_atr_percent,
    validate_m10_entry,
)

# ---------------------------
# Utilities & Data Structures
# ---------------------------

@dataclass
class TradeContext:
    symbol: str
    side: str                 # "buy" | "sell"
    h4: Dict[str, Dict[str, float]]
    trend: str
    adx: float
    atr_pct: float
    m10_ok: bool
    price: float
    spread_pips: float = 0.8  # pass your live spread if you have it
    risk_per_trade_max: float = 0.010  # 1.0% risk for A+ setups
    risk_per_trade_min: float = 0.005  # 0.5% for weaker setups
    
def final_pretrade_ok(symbol: str, side: str) -> bool:
    """
    Final safeguard before execution — reuses the relaxed H4 hard-filter.
    Prevents duplicate MA checks from blocking valid trades.
    """
    relax = os.getenv("ALLOW_TREND_RELAX", "true").lower() == "true"
    ok = passes_h4_hard_filters(symbol, side)
    if not ok and relax:
        print(f"[VALIDATION] ⚠️ MA trend opposite but relaxed mode active for {symbol} ({side})")
        return True
    elif not ok:
        print(f"[VALIDATION] ❌ MA trend misaligned for {symbol}: strict mode active")
        return False
    else:
        print(f"[VALIDATION] ✅ Post-scan hard filters passed for {symbol}")
        return True

def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _pip_factor(symbol: str) -> float:
    """
    Convert price units → pips. Adjust metals if broker differs.
    """
    s = symbol.upper().replace("_", "").replace("/", "")
    if s.endswith("JPY"):  # USDJPY etc.
        return 0.01
    if s == "XAUUSD":
        return 0.1
    if s == "XAGUSD":
        return 0.01
    return 0.0001

# ---------------------------
# Quality Score (0–100)
# ---------------------------

def compute_quality_score(ctx: TradeContext) -> float:
    """
    Soft scoring—no hard blocks. You’ll scale risk by this score.
    """
    score = 0.0
    w = {
        "trend_align":         0.22,
        "adx_health":          0.14,
        "atr_moderate":        0.12,
        "h4_rsi_band":         0.12,
        "h4_momentum":         0.14,
        "range_position":      0.10,
        "m10_trigger":         0.10,
        "microstructure_cost": 0.06,
    }

    # Trend alignment (reward, not gate)
    trend_ok = ((ctx.side == "buy" and ctx.trend == "bullish") or
                (ctx.side == "sell" and ctx.trend == "bearish"))
    score += w["trend_align"] * (100 if trend_ok else 60)

    # ADX curve
    adx = ctx.adx or 0.0
    if adx < 10: adx_pts = 40
    elif adx < 15: adx_pts = 60
    elif adx < 20: adx_pts = 75
    elif adx < 30: adx_pts = 90
    else: adx_pts = 85
    score += w["adx_health"] * adx_pts

    # ATR% moderation
    atr_pct = ctx.atr_pct or 0.0
    if atr_pct < 0.3: atr_pts = 55
    elif atr_pct < 0.5: atr_pts = 75
    elif atr_pct < 2.0: atr_pts = 90
    elif atr_pct < 3.5: atr_pts = 78
    else: atr_pts = 60
    score += w["atr_moderate"] * atr_pts

    # H4 bundle
    h4 = ctx.h4.get("H4", {})
    rsi = h4.get("rsi")
    mom5 = h4.get("momentum_5", 0.0)
    mom20 = h4.get("momentum_20", 0.0)
    rp = h4.get("range_position")

    # RSI (closer to 50 = better)
    if rsi is not None:
        dist = abs(rsi - 50.0)
        rsi_pts = _clip(100 - dist * 2.0, 50, 95)
        score += w["h4_rsi_band"] * rsi_pts

    # Momentum confluence
    if ctx.side == "buy":
        mom_pts = 70 + (10 if mom5 >= 0 else 0) + (10 if mom20 >= 0 else 0) + (5 if (mom5 >= 0 and mom20 >= 0) else 0)
    else:
        mom_pts = 70 + (10 if mom5 <= 0 else 0) + (10 if mom20 <= 0 else 0) + (5 if (mom5 <= 0 and mom20 <= 0) else 0)
    score += w["h4_momentum"] * _clip(mom_pts, 55, 95)

    # Range position (buys prefer ~0.2, sells ~0.8)
    if rp is not None:
        rp_ideal = 0.2 if ctx.side == "buy" else 0.8
        rp_pts = 95 - abs(rp - rp_ideal) * 200
        score += w["range_position"] * _clip(rp_pts, 55, 95)

    # M10 trigger
    score += w["m10_trigger"] * (92 if ctx.m10_ok else 68)

    # Spread penalty (microstructure costs)
    sp = ctx.spread_pips
    if sp <= 0.5: cost_pts = 95
    elif sp <= 1.0: cost_pts = 88
    elif sp <= 1.5: cost_pts = 80
    elif sp <= 2.0: cost_pts = 72
    else: cost_pts = 60
    score += w["microstructure_cost"] * cost_pts

    return float(round(score, 2))

# ---------------------------
# Risk Sizing
# ---------------------------

def position_size_from_score(score: float, ctx: TradeContext) -> float:
    """
    Map score → account risk %. Never 0; small for weak ideas, higher for strong.
    """
    if score <= 55:
        return ctx.risk_per_trade_min
    elif score <= 70:
        return ctx.risk_per_trade_min + (ctx.risk_per_trade_max * 0.25 - ctx.risk_per_trade_min) * (score - 55) / 15.0
    elif score <= 85:
        return ctx.risk_per_trade_max * 0.25 + (ctx.risk_per_trade_max * 0.40 - ctx.risk_per_trade_max * 0.25) * (score - 70) / 15.0
    else:
        return ctx.risk_per_trade_max * 0.40 + (ctx.risk_per_trade_max - ctx.risk_per_trade_max * 0.40) * (score - 85) / 15.0

# ---------------------------
# Exits (ATR-based; BE + trail later in monitor)
# ---------------------------

def compute_smart_exits(symbol: str, side: str, entry_price: float, atr_price_units: float) -> Dict[str, float]:
    """
    - SL ≈ 1x ATR (min 8 pips)
    - TP1 = 1.2R, TP2 = 2R
    """
    pip = _pip_factor(symbol)
    atr_pips = atr_price_units / pip if pip > 0 else 10.0
    sl_pips = max(atr_pips * 1.0, 8.0)
    tp1_pips = sl_pips * 1.2
    tp2_pips = sl_pips * 2.0

    sl_units  = sl_pips * pip
    tp1_units = tp1_pips * pip
    tp2_units = tp2_pips * pip

    if side == "buy":
        sl = entry_price - sl_units
        tp1 = entry_price + tp1_units
        tp2 = entry_price + tp2_units
    else:
        sl = entry_price + sl_units
        tp1 = entry_price - tp1_units
        tp2 = entry_price - tp2_units

    return {
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "sl_pips": sl_pips,
        "trail_start_r": 1.0,
        "trail_step_pips": max(atr_pips * 0.5, 5.0),
    }

# ---------------------------
# Planner (builds context → score → risk → exits)
# ---------------------------

def build_trade_context(symbol: str, side: str, spread_pips: float = 0.8) -> Optional[TradeContext]:
    """
    Pull features from validators and assemble a TradeContext.
    """
    if not is_forex_pair(symbol):
        return None

    h4 = get_momentum_signals(symbol, ["H4"])
    if "H4" not in h4:
        return None

    trend, adx, atr_pct = get_h4_trend_adx_atr_percent(symbol)
    m10_ok = validate_m10_entry(symbol, side, relax=True)

    # Safe fallbacks so we don’t choke volume
    trend = trend or "bullish"
    adx = adx if adx is not None else 18.0
    atr_pct = atr_pct if atr_pct is not None else 1.0

    price = h4["H4"]["price"]
    return TradeContext(
        symbol=symbol,
        side=side,
        h4=h4,
        trend=trend,
        adx=adx,
        atr_pct=atr_pct,
        m10_ok=m10_ok,
        price=price,
        spread_pips=spread_pips
    )

def plan_trade(symbol: str, side: str, spread_pips: float = 0.8) -> Optional[Dict[str, Any]]:
    """
    Main entry:
      - Build context
      - Compute Quality Score
      - Derive risk % from score
      - Compute ATR-based exits (H4 ATR% → price units)
    """
    ctx = build_trade_context(symbol, side, spread_pips=spread_pips)
    if ctx is None:
        print("[SMART] ❌ Could not build trade context.")
        return None

    score = compute_quality_score(ctx)
    risk_pct = position_size_from_score(score, ctx)

    # Convert H4 ATR% to price units for exits
    atr_price_units = (ctx.atr_pct / 100.0) * ctx.price
    exits = compute_smart_exits(ctx.symbol, ctx.side, ctx.price, atr_price_units)

    plan = {
        "symbol": ctx.symbol,
        "side": ctx.side,
        "entry_price": ctx.price,
        "quality_score": score,
        "risk_pct": risk_pct,
        "exits": exits,
        "diagnostics": {
            "trend": ctx.trend,
            "adx": ctx.adx,
            "atr_pct": ctx.atr_pct,
            "h4": ctx.h4.get("H4", {}),
            "m10_ok": ctx.m10_ok,
            "spread_pips": ctx.spread_pips
        }
    }

    print(f"[SMART] 📊 Quality={score:.1f} | Risk={risk_pct*100:.2f}% | Trend={ctx.trend} | ADX={ctx.adx:.1f} | ATR%={ctx.atr_pct:.2f}")
    print(f"[SMART] 🎯 Exits: SL={exits['sl']:.6f} TP1={exits['tp1']:.6f} TP2={exits['tp2']:.6f} (trail at +{exits['trail_start_r']}R, step={exits['trail_step_pips']:.1f} pips)")
    return plan
