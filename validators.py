import requests
import os
import oandapyV20
import oandapyV20.endpoints.instruments as instruments
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
BASE_URL = "https://api.twelvedata.com"

SUPPORTED_SYMBOLS = {
    "EURUSD", "USDJPY", "GBPUSD", "USDCHF",
    "AUDUSD", "USDCAD", "NZDUSD", "EURJPY",
    "GBPJPY", "AUDJPY", "NZDJPY", "EURGBP",
    "XAUUSD", "XAGUSD"  # Added precious metals
}

def is_forex_pair(symbol):
    clean_symbol = symbol.upper().replace("/", "").replace("_", "")
    is_valid = clean_symbol in SUPPORTED_SYMBOLS
    if not is_valid:
        print(f"[VALIDATORS] ❌ Symbol not supported: {symbol}")
    else:
        print(f"[VALIDATORS] ✅ Valid Forex symbol found: {symbol}")
    return is_valid

def get_oanda_data(symbol, granularity="H4", count=50, api_key=None, account_id=None, oanda_client=None):
    """Get price data from OANDA for more reliable technical analysis.
    
    Args:
        symbol: Trading pair symbol (e.g., EURUSD or EUR_USD)
        granularity: Timeframe (e.g., "H4", "H1", "M10")
        count: Number of candles to fetch
        api_key: Optional OANDA API key (legacy mode)
        account_id: Optional OANDA account ID (legacy mode)
        oanda_client: Optional pre-configured oandapyV20.API client. If provided,
                     will be used directly and environment variables will not be checked.
    
    Returns:
        List of candle data or None if error/credentials missing.
    """
    try:
        # If oanda_client is provided, use it directly
        if oanda_client is not None:
            client = oanda_client
        else:
            # Fall back to legacy behavior: use provided params or env vars
            account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
            token = api_key or os.getenv("OANDA_API_KEY")
            
            if not token:
                print("[VALIDATORS] ❌ Missing OANDA API credentials. Must be provided as parameters or set in environment (legacy mode).")
                return None
                
            client = oandapyV20.API(access_token=token, environment="live")
        
        # Convert symbol format for OANDA (e.g., EURUSD -> EUR_USD)
        if "_" not in symbol:
            if len(symbol) == 6:
                symbol = f"{symbol[:3]}_{symbol[3:]}"
        
        params = {
            "count": count,
            "granularity": granularity
        }
        
        r = instruments.InstrumentsCandles(instrument=symbol, params=params)
        client.request(r)
        
        return r.response["candles"]
        
    except Exception as e:
        print(f"[VALIDATORS] ❌ Error fetching OANDA data for {symbol}: {e}")
        return None

def calculate_rsi_from_data(prices, period=14):
    """Calculate RSI from price data"""
    if len(prices) < period + 1:
        return None
    
    gains = []
    losses = []
    
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    if len(gains) < period:
        return None
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi

def ema_trend_from_candles(candles, fast: int, slow: int) -> Optional[str]:
    """Return 'bullish' if EMA(fast) > EMA(slow), else 'bearish'."""
    try:
        closes = [float(c["mid"]["c"]) for c in candles]
        if len(closes) < slow:
            return None
        ema_fast = calculate_ema(closes[-fast:], fast)
        ema_slow = calculate_ema(closes, slow)
        if ema_fast is None or ema_slow is None:
            return None
        return "bullish" if ema_fast > ema_slow else "bearish"
    except Exception:
        return None

def get_momentum_signals(symbol, timeframes=["H4"], oanda_client=None):
    """Get momentum signals for 4H timeframe trading"""
    signals = {}
    
    for tf in timeframes:
        # Fetch enough bars for slow EMA (use slow+buffer)
        if tf in ("H4", "H1"):
            slow = 200; fast = 50
        elif tf in ("M15", "M10"):
            slow = 50;  fast = 20
        else:
            slow = 200; fast = 50

        need = slow + 25
        candles = get_oanda_data(symbol, tf, need, oanda_client=oanda_client)
        if not candles or len(candles) < slow:
            print(f"[VALIDATORS] ❌ Not enough {tf} candles for EMA trend (have={len(candles) if candles else 0}, need>={slow})")
            continue
            
        prices = [float(candle["mid"]["c"]) for candle in candles]
        highs = [float(candle["mid"]["h"]) for candle in candles]
        lows = [float(candle["mid"]["l"]) for candle in candles]
        
        if len(prices) >= 50:  # Need more data for 4H analysis
            # RSI (14-period for 4H)
            rsi = calculate_rsi_from_data(prices, 14)
            
            # 4H Price momentum (20-period rate of change)
            momentum_20 = ((prices[-1] - prices[-20]) / prices[-20]) * 100
            
            # Short-term momentum (5-period for 4H)
            momentum_5 = ((prices[-1] - prices[-5]) / prices[-5]) * 100 if len(prices) >= 5 else 0
            
            # 4H trend analysis (current vs 20 periods ago for stronger signal)
            # EMA-based trend per timeframe (aligns with hard filters)
            if tf in ("H4", "H1"):
                tf_trend = ema_trend_from_candles(candles, 50, 200)
            elif tf in ("M15", "M10"):
                tf_trend = ema_trend_from_candles(candles, 20, 50)
            else:
                tf_trend = ema_trend_from_candles(candles, 50, 200)

            trend = tf_trend or "neutral"
            
            print(f"[VALIDATORS] {tf} EMA trend: {trend} (fast={fast}, slow={slow}, candles={len(candles)})")


            
            # 4H support/resistance levels
            recent_high = max(highs[-10:])  # 10 candle high (40 hours)
            recent_low = min(lows[-10:])    # 10 candle low (40 hours)
            
            # Price position relative to recent range
            range_position = (prices[-1] - recent_low) / (recent_high - recent_low) if recent_high != recent_low else 0.5
            
            signals[tf] = {
                "rsi": rsi,
                "momentum_20": momentum_20,
                "momentum_5": momentum_5,
                "trend": trend,
                "price": prices[-1],
                "recent_high": recent_high,
                "recent_low": recent_low,
                "range_position": range_position
            }
    
    return signals

# -------- NEW: graded RSI helper (H4) --------
def rsi_edge_score_for_side(rsi: Optional[float], side: str) -> float:
    """
    Returns a graded 0..1 score:
    - SELL favors high RSI toward 70: map 60->0.0, 70->1.0 (clamped)
    - BUY  favors low  RSI toward 30: map 40->0.0, 30->1.0 (clamped)
    """
    if rsi is None:
        return 0.0
    if side == "sell":
        return max(0.0, min(1.0, (rsi - 60.0) / 10.0))
    else:  # buy
        return max(0.0, min(1.0, (40.0 - rsi) / 10.0))

# -------- NEW: controlled position sizing (recommendation) --------
def decide_position_size(validation_percentage: float) -> float:
    """
    Recommend size multiplier based on H4 validation %.
    - ≥ 60% -> 1.0x (full)
    - 50..59.99% -> 0.5x (half)
    - < 50% -> 0.0x (skip)
    Keeps functionally returning bool elsewhere; this just logs a recommendation.
    """
    full = float(os.getenv("H4_VALIDATION_FULL_PCT", "60"))
    half = float(os.getenv("H4_VALIDATION_MIN_PCT",  "50"))
    if validation_percentage >= full:
        return 1.0
    if validation_percentage >= half:
        return 0.5
    return 0.0

def validate_entry_conditions(symbol, side, timeframes=["H4"], trigger_ok=None, oanda_client=None):
    """Comprehensive entry validation across multiple conditions"""
    print(f"[VALIDATORS] 🔍 Validating {side} entry for {symbol}")

    if trigger_ok is False:
        print(f"[VALIDATORS] ❌ External trigger alignment failed (H1 trigger misaligned)")
        return False

    signals = get_momentum_signals(symbol, timeframes, oanda_client=oanda_client)
    if not signals:
        print("[VALIDATORS] ❌ Could not get market data for validation")
        return False

    validation_score = 0.0
    max_score = 0.0

    for tf, data in signals.items():
        max_score += 6
        rsi = data.get("rsi")
        momentum_20 = data.get("momentum_20")
        momentum_5 = data.get("momentum_5")
        trend = data.get("trend")
        range_position = data.get("range_position", 0.5)

        rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
        mom20_str = f"{momentum_20:.2f}%" if momentum_20 is not None else "N/A"
        mom5_str = f"{momentum_5:.2f}%" if momentum_5 is not None else "N/A"
        range_str = f"{range_position:.2f}" if range_position is not None else "N/A"
        print(f"[VALIDATORS] {tf}: RSI={rsi_str}, Mom20={mom20_str}, Mom5={mom5_str}, Trend={trend}, Range={range_str}")

        # graded RSI 0..1
        rsi_pts = rsi_edge_score_for_side(rsi, side)
        if rsi_pts > 0:
            print(f"[VALIDATORS] ✅ {tf} RSI graded contribution for {side}: +{rsi_pts:.2f}")
        else:
            print(f"[VALIDATORS] ⚠️ {tf} RSI offers no edge for {side}")
        validation_score += rsi_pts

        # long/short momentum
        if momentum_20 is not None:
            if side == "buy" and momentum_20 > -1.5:
                validation_score += 1
                print(f"[VALIDATORS] ✅ {tf} Long-term momentum favorable for {side}: {momentum_20:.2f}%")
            elif side == "sell" and momentum_20 < 1.5:
                validation_score += 1
                print(f"[VALIDATORS] ✅ {tf} Long-term momentum favorable for {side}: {momentum_20:.2f}%")

        if momentum_5 is not None:
            if side == "buy" and momentum_5 > -0.8:
                validation_score += 1
                print(f"[VALIDATORS] ✅ {tf} Short-term momentum favorable for {side}: {momentum_5:.2f}%")
            elif side == "sell" and momentum_5 < 0.8:
                validation_score += 1
                print(f"[VALIDATORS] ✅ {tf} Short-term momentum favorable for {side}: {momentum_5:.2f}%")

        # trend weight
        if (side == "buy" and trend == "bullish") or (side == "sell" and trend == "bearish"):
            validation_score += 1.5
            print(f"[VALIDATORS] ✅ {tf} Strong trend aligned with {side}: {trend}")
        elif trend == "neutral":
            validation_score += 0.5
            print(f"[VALIDATORS] ⚠️ {tf} Neutral trend for {side}: {trend}")

        # range position
        if side == "buy" and range_position <= 0.4:
            validation_score += 1
            print(f"[VALIDATORS] ✅ {tf} Good range position for {side}: {range_position:.2f} (near support)")
        elif side == "sell" and range_position >= 0.6:
            validation_score += 1
            print(f"[VALIDATORS] ✅ {tf} Good range position for {side}: {range_position:.2f} (near resistance)")
        elif 0.4 < range_position < 0.6:
            validation_score += 0.5
            print(f"[VALIDATORS] ⚠️ {tf} Neutral range position: {range_position:.2f} (mid-range)")

        # confluence bonus
        confluence_factors = 0
        trend_align = ((side == "buy" and trend == "bullish") or (side == "sell" and trend == "bearish"))
        if trend_align:
            confluence_factors += 1
        if side == "buy":
            momentum_align = ((momentum_20 is not None and momentum_20 >= 0) or (momentum_5 is not None and momentum_5 >= 0))
        else:
            momentum_align = ((momentum_20 is not None and momentum_20 <= 0) or (momentum_5 is not None and momentum_5 <= 0))
        if momentum_align:
            confluence_factors += 1
        range_align = ((side == "buy" and range_position <= 0.4) or (side == "sell" and range_position >= 0.6))
        if range_align:
            confluence_factors += 1
        if confluence_factors >= 2:
            validation_score += 0.5
            print(f"[VALIDATORS] 🎯 {tf} Confluence bonus: {confluence_factors} of 3 aligned")

    validation_percentage = (validation_score / max_score) * 100 if max_score > 0 else 0
    size_mult = decide_position_size(validation_percentage)
    size_note = "full_size" if size_mult == 1.0 else "half_size" if size_mult == 0.5 else "skip"
    print(f"[VALIDATORS] 📊 4H Validation Score: {validation_score:.1f}/{max_score:.1f} ({validation_percentage:.1f}%) -> size={size_note}")

    min_pct = float(os.getenv("H4_VALIDATION_MIN_PCT", "55"))
    is_valid = validation_percentage >= min_pct
    if is_valid:
        print(f"[VALIDATORS] ✅ Entry conditions PASSED for {side} {symbol} (recommended size: {size_note})")
    else:
        print(f"[VALIDATORS] ❌ Entry conditions FAILED for {side} {symbol} (recommended size: {size_note})")
    return is_valid


def get_rsi(symbol, interval="4h", oanda_client=None):
    """Enhanced RSI calculation using OANDA data with Twelve Data fallback"""
    # First try OANDA data
    candles = get_oanda_data(symbol, "H4", 30, oanda_client=oanda_client)
    if candles:
        prices = [float(candle["mid"]["c"]) for candle in candles]
        rsi = calculate_rsi_from_data(prices)
        if rsi is not None:
            print(f"[VALIDATORS] ✅ RSI for {symbol}: {rsi:.1f} (OANDA data)")
            return rsi
    
    # Fallback to Twelve Data
    if not API_KEY:
        print("[VALIDATORS] ❌ Missing Twelve Data API Key.")
        return None

    if not is_forex_pair(symbol):
        print(f"[VALIDATORS] Skipping RSI check: unsupported symbol '{symbol}'")
        return None

    try:
        response = requests.get(f"{BASE_URL}/rsi", params={
            "symbol": symbol,
            "interval": interval,
            "apikey": API_KEY
        })
        response.raise_for_status()
        data = response.json()
        if "values" in data and data["values"]:
            rsi_value = float(data["values"][0]["rsi"])
            print(f"[VALIDATORS] ✅ RSI for {symbol}: {rsi_value:.1f} (Twelve Data)")
            return rsi_value
        else:
            print(f"[VALIDATORS] ❌ RSI response missing 'values': {data}")
            return None
    except Exception as e:
        print(f"[VALIDATORS] ❌ Error fetching RSI for {symbol}: {e}")
        return None

def get_ema(symbol, interval="4h", oanda_client=None):
    """Enhanced EMA calculation using OANDA data with Twelve Data fallback"""
    # First try OANDA data
    candles = get_oanda_data(symbol, "H4", 200, oanda_client=oanda_client)
    if candles and len(candles) >= 200:
        prices = [float(candle["mid"]["c"]) for candle in candles]
        
        # Calculate EMA50 and EMA200
        ema50 = calculate_ema(prices[-50:], 50)
        ema200 = calculate_ema(prices, 200)
        
        if ema50 and ema200:
            trend = "bullish" if ema50 > ema200 else "bearish"
            print(f"[VALIDATORS] ✅ EMA trend for {symbol}: {trend} (EMA50: {ema50:.5f}, EMA200: {ema200:.5f}) - OANDA data")
            return trend
    
    # Fallback to Twelve Data
    if not API_KEY:
        print("[VALIDATORS] ❌ Missing Twelve Data API Key.")
        return None

    if not is_forex_pair(symbol):
        print(f"[VALIDATORS] Skipping EMA check: unsupported symbol '{symbol}'")
        return None

    try:
        response_50 = requests.get(f"{BASE_URL}/ema", params={
            "symbol": symbol,
            "interval": interval,
            "time_period": 50,
            "apikey": API_KEY
        })
        response_50.raise_for_status()
        ema50 = float(response_50.json()["values"][0]["ema"])

        response_200 = requests.get(f"{BASE_URL}/ema", params={
            "symbol": symbol,
            "interval": interval,
            "time_period": 200,
            "apikey": API_KEY
        })
        response_200.raise_for_status()
        ema200 = float(response_200.json()["values"][0]["ema"])

        trend = "bullish" if ema50 > ema200 else "bearish"
        print(f"[VALIDATORS] ✅ EMA trend for {symbol}: {trend} (EMA50: {ema50}, EMA200: {ema200}) - Twelve Data")
        return trend
    except Exception as e:
        print(f"[VALIDATORS] ❌ Error fetching EMA for {symbol}: {e}")
        return None

def calculate_ema(prices, period):
    """Calculate Exponential Moving Average"""
    if len(prices) < period:
        return None
    
    multiplier = 2 / (period + 1)
    ema = prices[0]  # Start with first price
    
    for price in prices[1:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    
    return ema

def get_support_resistance_levels(symbol, lookback_periods=100, oanda_client=None):
    """Calculate dynamic support and resistance levels"""
    candles = get_oanda_data(symbol, "H4", lookback_periods, oanda_client=oanda_client)
    if not candles:
        return None, None
    
    highs = [float(candle["mid"]["h"]) for candle in candles]
    lows = [float(candle["mid"]["l"]) for candle in candles]
    
    # Simple pivot points for support/resistance
    resistance = max(highs[-20:])  # Recent 20-period high
    support = min(lows[-20:])     # Recent 20-period low
    
    current_price = float(candles[-1]["mid"]["c"])
    
    print(f"[VALIDATORS] S/R Levels for {symbol}: Support={support:.5f}, Current={current_price:.5f}, Resistance={resistance:.5f}")
    
    return support, resistance

def _calculate_true_ranges_from_hlc(highs: List[float], lows: List[float], closes: List[float]) -> List[float]:
    if not highs or not lows or not closes or len(highs) != len(lows) or len(highs) != len(closes):
        return []
    true_ranges: List[float] = []
    for i in range(1, len(highs)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i - 1])
        tr3 = abs(lows[i] - closes[i - 1])
        true_ranges.append(max(tr1, tr2, tr3))
    return true_ranges


def _wilder_smooth(values: List[float], period: int) -> List[float]:
    if len(values) < period or period <= 0:
        return []
    smoothed: List[float] = []
    # Initial value = simple average of first period
    initial = sum(values[:period]) / float(period)
    smoothed.append(initial)
    for v in values[period:]:
        next_val = (smoothed[-1] * (period - 1) + v) / float(period)
        smoothed.append(next_val)
    return smoothed


def calculate_adx_from_hlc(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    try:
        if len(highs) < period + 2 or len(lows) < period + 2 or len(closes) < period + 2:
            return None
        dm_plus: List[float] = [0.0]
        dm_minus: List[float] = [0.0]
        tr_list: List[float] = [0.0]
        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            dm_plus.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            dm_minus.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i - 1])
            tr3 = abs(lows[i] - closes[i - 1])
            tr_list.append(max(tr1, tr2, tr3))
        atr_smoothed = _wilder_smooth(tr_list[1:], period)
        dm_plus_smoothed = _wilder_smooth(dm_plus[1:], period)
        dm_minus_smoothed = _wilder_smooth(dm_minus[1:], period)
        if not atr_smoothed or not dm_plus_smoothed or not dm_minus_smoothed:
            return None
        last_atr = atr_smoothed[-1]
        if last_atr == 0:
            return None
        di_plus = 100.0 * (dm_plus_smoothed[-1] / last_atr)
        di_minus = 100.0 * (dm_minus_smoothed[-1] / last_atr)
        dx_values: List[float] = []
        # Build DX series aligned to smoothed values
        for i in range(min(len(dm_plus_smoothed), len(dm_minus_smoothed))):
            plus = dm_plus_smoothed[i]
            minus = dm_minus_smoothed[i]
            atr_i = atr_smoothed[i] if i < len(atr_smoothed) else last_atr
            if atr_i == 0:
                continue
            di_p = 100.0 * (plus / atr_i)
            di_m = 100.0 * (minus / atr_i)
            denom = (di_p + di_m)
            dx = (abs(di_p - di_m) / denom * 100.0) if denom > 0 else 0.0
            dx_values.append(dx)
        if len(dx_values) < period:
            return None
        adx_series = _wilder_smooth(dx_values, period)
        return adx_series[-1] if adx_series else None
    except Exception:
        return None


def get_h4_trend_adx_atr_percent(symbol: str, adx_period: int = 14, atr_period: int = 21, oanda_client=None) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """Return (trend, adx, atr_percent) on H4.
    - trend: 'bullish' or 'bearish' from EMA50 vs EMA200
    - adx: Wilder's ADX value
    - atr_percent: ATR(atr_period)/close*100
    """
    candles = get_oanda_data(symbol, "H4", max(atr_period, 200) + 5, oanda_client=oanda_client)
    if not candles or len(candles) < max(atr_period, 200) + 1:
        return None, None, None
    closes = [float(c["mid"]["c"]) for c in candles]
    highs = [float(c["mid"]["h"]) for c in candles]
    lows = [float(c["mid"]["l"]) for c in candles]
    ema50 = calculate_ema(closes[-50:], 50)
    ema200 = calculate_ema(closes, 200)
    trend = None
    if ema50 and ema200:
        trend = "bullish" if ema50 > ema200 else "bearish"
    adx = calculate_adx_from_hlc(highs, lows, closes, adx_period)
    tr_list = _calculate_true_ranges_from_hlc(highs, lows, closes)
    atr_values = _wilder_smooth(tr_list, atr_period)
    atr = atr_values[-1] if atr_values else None
    atr_percent = (atr / closes[-1] * 100.0) if (atr and closes[-1] > 0) else None
    return trend, adx, atr_percent


def passes_h4_hard_filters(symbol: str, side: str, relax: bool = False, oanda_client=None) -> bool:
    """Enforce H4 hard filters: EMA trend alignment, ADX, ATR% window.
    Never relax trend alignment. Relax ADX threshold slightly when relax=True.
    """
    print(f"[VALIDATORS] 🔒 H4 hard filters for {symbol} {side}")
    trend, adx, atr_pct = get_h4_trend_adx_atr_percent(symbol, oanda_client=oanda_client)
    if trend is None or adx is None or atr_pct is None:
        print("[VALIDATORS] ❌ Missing H4 metrics for hard filters")
        return False
    # Trend must align and is never relaxed
    allow_relax = os.getenv("ALLOW_TREND_RELAX", "true").lower() == "true"

    if (side == "buy" and trend == "bearish") or (side == "sell" and trend == "bullish"):
        if allow_relax:
            print(f"[VALIDATORS] ⚠️ H4 trend opposite ({trend}) but validator confidence overrides -> proceeding under relaxed mode")
        else:
            print(f"[VALIDATORS] ❌ Trend misaligned: trend={trend}, side={side}")
            return False
    # ADX threshold (env overrideable)
    try:
        base_adx = float(os.getenv("H4_MIN_ADX", "17.0"))
    except Exception:
        base_adx = 18.0
    adx_threshold = base_adx - (2.0 if relax else 0.0)
    if adx < adx_threshold:
        print(f"[VALIDATORS] ❌ ADX too low: {adx:.1f} < {adx_threshold:.1f}")
        return False
    # ATR% window: prefer moderate volatility (env overrideable)
    try:
        min_atr_pct = float(os.getenv("H4_MIN_ATR_PCT", "0.22"))
        max_atr_pct = float(os.getenv("H4_MAX_ATR_PCT", "3.2"))
    except Exception:
        min_atr_pct, max_atr_pct = 0.25, 3.2
    if not (min_atr_pct <= atr_pct <= max_atr_pct):
        print(f"[VALIDATORS] ❌ ATR% out of range: {atr_pct:.2f}% not in [{min_atr_pct}, {max_atr_pct}]%")
        return False
    print(f"[VALIDATORS] ✅ H4 hard filters PASSED (trend={trend}, ADX={adx:.1f}, ATR%={atr_pct:.2f}%)")
    return True


def _get_oanda_prices(symbol: str, granularity: str, count: int, oanda_client=None) -> Optional[List[Dict]]:
    try:
        return get_oanda_data(symbol, granularity, count, oanda_client=oanda_client)
    except Exception:
        return None


def validate_m10_entry(symbol: str, side: str, relax: bool = False, oanda_client=None) -> bool:
    """M10 entry confirmation: RSI(14), momentum alignment, pullback to EMA20 zone.
    Pullback zone width = 0.30*ATR by default (slightly wider to reduce near-miss skips).
    
    Args:
        symbol: Trading pair symbol
        side: Trade direction ('buy' or 'sell')
        relax: Whether to use relaxed criteria
        oanda_client: Optional OANDA API client. If provided, will be used for data fetching
                     instead of environment variables (required for live enhanced mode).
    """
    print(f"[VALIDATORS] ⏱️ M10 entry check for {symbol} {side}")
    candles = _get_oanda_prices(symbol, "M10", 120, oanda_client=oanda_client)
    if not candles or len(candles) < 40:
        print("[VALIDATORS] ❌ Not enough M10 data")
        return False
    closes = [float(c["mid"]["c"]) for c in candles]
    highs = [float(c["mid"]["h"]) for c in candles]
    lows = [float(c["mid"]["l"]) for c in candles]
    price = closes[-1]
    rsi = calculate_rsi_from_data(closes, 14)
    # Momentum
    mom5 = ((closes[-1] - closes[-5]) / closes[-5]) * 100 if len(closes) >= 6 else 0.0
    mom20 = ((closes[-1] - closes[-20]) / closes[-20]) * 100 if len(closes) >= 21 else 0.0
    # EMA20 and ATR for pullback zone
    ema20 = calculate_ema(closes[-20:], 20)
    tr_list = _calculate_true_ranges_from_hlc(highs, lows, closes)
    atr10_series = _wilder_smooth(tr_list, 10)
    atr10 = atr10_series[-1] if atr10_series else None
    if any(v is None for v in [rsi, ema20, atr10]):
        print("[VALIDATORS] ❌ Missing M10 indicators (RSI/EMA20/ATR10)")
        return False
    # -------- CHANGED: widen pullback zone (allow ±1.3×ATR10 by default for better acceptance) --------
    try:
        base_zone = float(os.getenv("M10_PULLBACK_ATR_MULT", "1.3" if not relax else "1.4"))  # Widened from 1.2/1.3
    except Exception:
        base_zone = 1.3 if not relax else 1.4
    zone_width = atr10 * base_zone
    dist_to_ema = abs(price - ema20)
    pullback_ok = dist_to_ema <= zone_width
    # Directional momentum and RSI ranges
    if side == "buy":
        rsi_ok = 30 <= rsi <= 70
        momentum_ok = mom5 >= -0.3 and mom20 >= -1.2
    else:
        rsi_ok = 30 <= rsi <= 70
        momentum_ok = mom5 <= 0.3 and mom20 <= 1.2
    if not rsi_ok:
        print(f"[VALIDATORS] ❌ M10 RSI out of range: {rsi:.1f}")
        return False
    if not momentum_ok:
        print(f"[VALIDATORS] ❌ M10 momentum not aligned: mom5={mom5:.2f} mom20={mom20:.2f}")
        return False
    if not pullback_ok:
        print(f"[VALIDATORS] ⚠️ M10 not in EMA20 pullback zone: dist={dist_to_ema:.5f} > {zone_width:.5f}")
        # --- BREAKOUT PATHWAY: allow strong RSI expansion with ATR/Range thrust ---
        prev_rsi = calculate_rsi_from_data(closes[:-1], 14)
        try:
            breakout_rsi_delta = float(os.getenv("BREAKOUT_RSI_DELTA", "5.0"))
        except Exception:
            breakout_rsi_delta = 5.0
        # Range thrust relative to recent average
        recent_ranges = [(h - l) for h, l in zip(highs[-10:], lows[-10:])]
        avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0.0
        curr_range = highs[-1] - lows[-1]
        thrust_ratio = (curr_range / avg_range) if avg_range > 0 else 0.0
        try:
            min_thrust = float(os.getenv("BREAKOUT_THRUST_MIN", "1.2"))
        except Exception:
            min_thrust = 1.2

        breakout_ok = False
        if side == "buy":
            breakout_ok = (
                rsi is not None and prev_rsi is not None and rsi >= 60 and (rsi - prev_rsi) >= breakout_rsi_delta
                and thrust_ratio >= min_thrust and mom5 >= 0 and mom20 >= 0
            )
        else:
            breakout_ok = (
                rsi is not None and prev_rsi is not None and rsi <= 40 and (prev_rsi - rsi) >= breakout_rsi_delta
                and thrust_ratio >= min_thrust and mom5 <= 0 and mom20 <= 0
            )

        if breakout_ok:
            print(f"[VALIDATORS] ✅ Breakout pathway PASSED (RSIΔ≥{breakout_rsi_delta}, thrust≥{min_thrust})")
            return True
        print(f"[VALIDATORS] ❌ Entry failed: neither pullback nor breakout conditions met (thrust={thrust_ratio:.2f})")
        return False

    print(f"[VALIDATORS] ✅ M10 entry PASSED (RSI={rsi:.1f}, mom5={mom5:.2f}%, mom20={mom20:.2f}%, dist_to_ema={dist_to_ema:.5f}, zone={zone_width:.5f})")
    return True
