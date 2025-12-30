import os
import re
import json
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.accounts as account
from oandapyV20.endpoints.accounts import AccountInstruments
import datetime
import time
from datetime import timezone
from typing import Tuple, Optional

from trade_cache import add_trade, get_active_trades
from trading_config import get_config
from validators import (
    get_oanda_data,
    calculate_ema,
    get_support_resistance_levels,
    get_h4_trend_adx_atr_percent,
    passes_h4_hard_filters,
)
from news_filter import is_news_blackout
from db_persistence import save_trade_from_oanda_account
from datetime import datetime

# --- Correlation groups (prevent stacking highly correlated exposure) ---
CORRELATION_GROUPS = [
    {"name": "USD_MAJORS", "members": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]},
    {"name": "YEN_CROSSES", "members": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY"]},
]

def _normalize_symbol(symbol: str) -> str:
    return symbol.upper().replace("_", "")

def _is_correlated_with_open(symbol: str, user_id=None, allow_low_risk_increment: bool = True) -> bool:
    """Return True if symbol belongs to a correlation group with any active trade symbol.
    If user_id is provided, only checks trades for that user's account.
    If allow_low_risk_increment is True, allows new trades when incremental risk is low.
    """
    try:
        active = get_active_trades()
        # TODO: Filter active trades by user_id if provided (requires trade_cache enhancement)
        # For now, we check all active trades but this should be per-user in the future
        active_syms = {_normalize_symbol(t.get("symbol", t.get("instrument", ""))) for t in active}
        sym = _normalize_symbol(symbol)
        
        for group in CORRELATION_GROUPS:
            members = set(group["members"])
            if sym in members:
                correlated_count = sum(1 for a in active_syms if a in members)
                
                # If no correlated trades, allow
                if correlated_count == 0:
                    continue
                
                # If allow_low_risk_increment is enabled, allow up to 2 correlated trades
                # This provides diversification while controlling stacking
                if allow_low_risk_increment and correlated_count < 2:
                    print(f"[VALIDATION] ⚠️ Correlation warning: {correlated_count} correlated trade(s), but allowing (low incremental risk)")
                    continue
                
                # Block if too many correlated trades
                return True
        
        return False
    except Exception:
        return False

def _find_recent_swing_levels(symbol: str, side: str, lookback: int = 30) -> tuple:
    """Find recent swing high/low on H4 within lookback candles for swing-based SL."""
    try:
        candles = get_oanda_data(symbol.replace("_", ""), "H4", max(lookback, 20))
        if not candles:
            return None, None
        highs = [float(c["mid"]["h"]) for c in candles]
        lows = [float(c["mid"]["l"]) for c in candles]
        recent_high = max(highs[-lookback:]) if len(highs) >= lookback else max(highs)
        recent_low = min(lows[-lookback:]) if len(lows) >= lookback else min(lows)
        return recent_low, recent_high
    except Exception:
        return None, None

def _ma_trend_direction(symbol: str, oanda_client=None) -> str:
    """Return 'bullish' or 'bearish' via EMA50 vs EMA200 on H4."""
    try:
        candles = get_oanda_data(symbol.replace("_", ""), "H4", 210, oanda_client=oanda_client)
        if not candles or len(candles) < 200:
            return "unknown"
        closes = [float(c["mid"]["c"]) for c in candles]
        ema50 = calculate_ema(closes[-50:], 50)
        ema200 = calculate_ema(closes, 200)
        if ema50 is None or ema200 is None:
            return "unknown"
        return "bullish" if ema50 > ema200 else "bearish"
    except Exception:
        return "unknown"

def calculate_atr(client, account_id, instrument, periods=21):
    """Calculate Average True Range optimized for 4H trading"""
    try:
        # For 4H trading, use 21 periods (about 3.5 days of data)
        # This gives a good balance of responsiveness and stability
        params = {
            "count": periods + 1,
            "granularity": "H4"  # 4-hour candles
        }
        r = instruments.InstrumentsCandles(instrument=instrument, params=params)
        client.request(r)
        candles = r.response["candles"]
        
        if len(candles) < periods + 1:
            print(f"[ATR] Insufficient data for ATR calculation: {len(candles)} candles")
            return None
        
        true_ranges = []
        for i in range(1, len(candles)):
            current = candles[i]
            previous = candles[i-1]
            
            high = float(current["mid"]["h"])
            low = float(current["mid"]["l"])
            prev_close = float(previous["mid"]["c"])
            
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            
            true_ranges.append(max(tr1, tr2, tr3))
        
        # Use Exponential Moving Average for ATR (more responsive for 4H)
        atr = calculate_ema_atr(true_ranges, periods)
        
        print(f"[ATR] 4H ATR calculated: {atr:.5f} over {len(true_ranges)} periods")
        return atr
    except Exception as e:
        print(f"[ATR] Error calculating 4H ATR: {e}")
        return None

def calculate_ema_atr(true_ranges, periods):
    """Calculate EMA-based ATR for more responsive 4H calculations"""
    if not true_ranges:
        return None
    
    multiplier = 2.0 / (periods + 1)
    ema_atr = true_ranges[0]  # Start with first value
    
    for tr in true_ranges[1:]:
        ema_atr = (tr * multiplier) + (ema_atr * (1 - multiplier))
    
    return ema_atr

def get_market_spread(client, account_id, instrument):
    """Get current market spread to assess liquidity"""
    try:
        r = pricing.PricingInfo(accountID=account_id, params={"instruments": instrument})
        client.request(r)
        prices = r.response["prices"][0]
        bid = float(prices["bids"][0]["price"])
        ask = float(prices["asks"][0]["price"])
        spread = ask - bid
        return spread, bid, ask
    except Exception as e:
        print(f"[SPREAD] Error getting spread: {e}")
        return None, None, None

def calculate_dynamic_position_size(balance, risk_percent, atr, instrument):
    """Calculate position size based on account balance, risk percentage, and volatility"""
    # Risk per trade as percentage of account balance
    risk_amount = balance * (risk_percent / 100)
    
    # Adjust for instrument type
    if "JPY" in instrument:
        pip_value = 0.01  # For JPY pairs, 1 pip = 0.01
        atr_pips = atr * 100
    else:
        pip_value = 0.0001  # For most pairs, 1 pip = 0.0001
        atr_pips = atr * 10000
    
    # Calculate position size based on ATR (using 2x ATR as stop loss distance)
    stop_distance_pips = atr_pips * 2
    
    # Position size = Risk Amount / (Stop Distance in Pips * Pip Value * Units per Lot)
    if stop_distance_pips > 0:
        position_size = int(risk_amount / (stop_distance_pips * pip_value))
        # Ensure minimum and maximum position sizes
        position_size = max(1000, min(position_size, 100000))
    else:
        position_size = 1000
    
    return position_size

def is_market_hours_favorable(instrument):
    """Check if current time is favorable for 4H trading"""
    now = datetime.datetime.utcnow()
    hour = now.hour
    
    # For 4H trading, we need sustained momentum periods
    # Focus on major session overlaps and high-volume periods
    
    if "JPY" in instrument:
        # Asian session + Asian-European overlap: 22:00-10:00 UTC
        # Peak Asian liquidity for 4H moves
        return hour >= 22 or hour <= 10
    elif any(pair in instrument for pair in ["EUR", "GBP", "CHF"]):
        # European session + overlaps: 06:00-18:00 UTC
        # Covers Euro open through NY overlap
        return 6 <= hour <= 18
    elif any(pair in instrument for pair in ["USD", "CAD"]):
        # American session focus: 12:00-22:00 UTC
        # Peak USD volatility for 4H moves
        return 12 <= hour <= 22
    else:
        # General major session overlaps (best for 4H momentum)
        # European-American overlap: 12:00-17:00 UTC
        return 12 <= hour <= 17

def _check_volatility_spike(instrument: str, oanda_client=None) -> Tuple[bool, Optional[float]]:
    """
    Check for abnormal ATR expansion (volatility spike).
    Returns (is_spike, current_atr_pct) where is_spike=True if ATR% > 1.5x recent average.
    """
    try:
        from validators import get_h4_trend_adx_atr_percent, get_oanda_data
        from validators import calculate_ema
        
        # Get current ATR%
        trend, adx, current_atr_pct = get_h4_trend_adx_atr_percent(instrument.replace("_", ""), oanda_client=oanda_client)
        if current_atr_pct is None:
            return False, None
        
        # Get historical ATR% values (last 20 H4 candles = ~3.3 days)
        candles = get_oanda_data(instrument.replace("_", ""), "H4", 60, oanda_client=oanda_client)
        if not candles or len(candles) < 20:
            return False, current_atr_pct
        
        # Calculate ATR% for each period
        atr_pcts = []
        for i in range(20, len(candles)):
            period_candles = candles[i-20:i]
            closes = [float(c["mid"]["c"]) for c in period_candles]
            highs = [float(c["mid"]["h"]) for c in period_candles]
            lows = [float(c["mid"]["l"]) for c in period_candles]
            
            # Calculate ATR for this period
            tr_list = []
            for j in range(1, len(period_candles)):
                tr1 = highs[j] - lows[j]
                tr2 = abs(highs[j] - closes[j-1])
                tr3 = abs(lows[j] - closes[j-1])
                tr_list.append(max(tr1, tr2, tr3))
            
            if tr_list:
                # Simple ATR (average of TR)
                atr = sum(tr_list) / len(tr_list)
                atr_pct = (atr / closes[-1] * 100.0) if closes[-1] > 0 else None
                if atr_pct:
                    atr_pcts.append(atr_pct)
        
        if not atr_pcts:
            return False, current_atr_pct
        
        # Calculate average ATR%
        avg_atr_pct = sum(atr_pcts) / len(atr_pcts)
        
        # Check if current ATR% is > 1.5x average (spike threshold)
        spike_threshold = avg_atr_pct * 1.5
        is_spike = current_atr_pct > spike_threshold
        
        if is_spike:
            print(f"[VALIDATION] ⚠️ Volatility spike detected: ATR%={current_atr_pct:.2f}% > {spike_threshold:.2f}% (avg={avg_atr_pct:.2f}%)")
        
        return is_spike, current_atr_pct
    except Exception as e:
        print(f"[VALIDATION] ⚠️ Error checking volatility spike: {e}")
        return False, None


def _is_weekend_risk_period() -> bool:
    """
    Check if current time is in weekend risk period (Friday 20:00 UTC - Sunday 22:00 UTC).
    Returns True if in weekend risk period.
    """
    now = datetime.utcnow()
    weekday = now.weekday()  # 0=Monday, 4=Friday, 6=Sunday
    hour = now.hour
    
    # Friday after 20:00 UTC through Sunday before 22:00 UTC
    if weekday == 4 and hour >= 20:  # Friday 20:00+
        return True
    if weekday == 5:  # Saturday
        return True
    if weekday == 6 and hour < 22:  # Sunday before 22:00
        return True
    
    return False


def validate_trade_entry(client, account_id, instrument, side, trade_idea, user_id=None, skip_duplicate_validation=False):
    """Enhanced validation before placing trade.
    
    Args:
        client: OANDA API client (required for live trading)
        account_id: OANDA account ID
        instrument: Trading instrument
        side: Trade direction ('buy' or 'sell')
        trade_idea: Trade idea text
        user_id: User ID for tracking (optional)
        skip_duplicate_validation: If True, skip validation that was already done in enhanced path.
                                   This prevents legacy validation from blocking trades after enhanced validation has passed.
    """
    # For live enhanced mode, if enhanced validation has already passed, skip duplicate validation
    if skip_duplicate_validation:
        print("[VALIDATION] ℹ️ Skipping legacy trade context build — enhanced execution active (validation already passed)")
        # Still do basic safety checks (spread, correlation, concurrent trades) but skip technical analysis
        # that was already done in enhanced validation
        try:
            # Portfolio constraints: cap concurrent trades and correlation groups
            active_trades = get_active_trades()
            if len(active_trades) >= get_config().risk_management.max_open_trades:
                print(f"[VALIDATION] ❌ Max open trades reached: {len(active_trades)}")
                return False
            if _is_correlated_with_open(instrument, user_id=user_id, allow_low_risk_increment=True):
                print(f"[VALIDATION] ❌ Correlation lockout: too many correlated positions (max 2 per group)")
                return False
            
            # Check spread (basic liquidity check)
            spread, bid, ask = get_market_spread(client, account_id, instrument)
            if spread:
                max_spread = get_config().get_max_spread(instrument)
                if spread > max_spread:
                    print(f"[VALIDATION] ❌ Spread too wide: {spread:.5f} > {max_spread:.5f}")
                    return False
                print(f"[VALIDATION] ✅ Spread acceptable: {spread:.5f}")
            
            return True
        except Exception as e:
            print(f"[VALIDATION] Error during basic validation: {e}")
            return False
    
    try:
        # Config-driven favorable hours enforcement
        config = get_config()
        enforce_session_hours = os.getenv("ENFORCE_SESSION_HOURS", "true").lower() == "true"
        is_favorable_time = config.is_favorable_trading_time(instrument)
        if not is_favorable_time:
            if enforce_session_hours:
                print(f"[VALIDATION] ❌ Outside favorable session hours for {instrument}")
                return False
            else:
                print(f"[VALIDATION] Warning: Trading outside favorable hours for {instrument}")

        # Optional news blackout window
        if is_news_blackout(instrument):
            print(f"[VALIDATION] ❌ News blackout active for {instrument}")
            return False
        
        # Volatility spike protection: throttle entries during abnormal ATR expansion
        is_spike, atr_pct = _check_volatility_spike(instrument, oanda_client=client)
        if is_spike:
            # Allow only high-quality setups during volatility spikes (require higher score)
            print(f"[VALIDATION] ⚠️ Volatility spike detected (ATR%={atr_pct:.2f}%), requiring exceptional setup quality")
            # This will be checked by the enhanced validation layer (higher score threshold)
            # For now, we just log a warning but don't block (let enhanced layer decide)
        
        # Weekend risk protection: reduce exposure or require higher quality
        if _is_weekend_risk_period():
            print(f"[VALIDATION] ⚠️ Weekend risk period detected - requiring higher quality setup")
            # Enhanced validation layer should apply stricter criteria
            # For now, we just log a warning but don't block (let enhanced layer decide)
        
        # Portfolio constraints: cap concurrent trades and correlation groups
        # If user_id is provided, filter active_trades to this user's account
        active_trades = get_active_trades()
        if user_id is not None:
            # Filter to only this user's trades (check account_id in trade metadata if available)
            # For now, we'll use all active trades but this can be enhanced to filter per user
            # The trade_cache may need to be enhanced to track user_id per trade
            pass  # TODO: Enhance trade_cache to support per-user filtering
        
        if len(active_trades) >= get_config().risk_management.max_open_trades:
            print(f"[VALIDATION] ❌ Max open trades reached: {len(active_trades)}")
            return False
        if _is_correlated_with_open(instrument, user_id=user_id, allow_low_risk_increment=True):
            print(f"[VALIDATION] ❌ Correlation lockout: too many correlated positions (max 2 per group)")
            return False

        # Check spread
        spread, bid, ask = get_market_spread(client, account_id, instrument)
        if spread:
            # Reject if spread is too wide (indicates poor liquidity)
            max_spread = get_config().get_max_spread(instrument)
            if spread > max_spread:
                print(f"[VALIDATION] ❌ Spread too wide: {spread:.5f} > {max_spread:.5f}")
                return False
            print(f"[VALIDATION] ✅ Spread acceptable: {spread:.5f}")
        
        # Technical confirmations: MA trend direction alignment (EMA50 vs EMA200)
        # Use the provided client to fetch data instead of env vars
        trend = _ma_trend_direction(instrument, oanda_client=client)
        relax = os.getenv("ALLOW_TREND_RELAX", "true").lower() == "true"

        if trend != "unknown":
            # Check alignment as before
            misaligned = (side == "buy" and trend != "bullish") or (side == "sell" and trend != "bearish")

            if misaligned:
                if relax:
                    print(f"[VALIDATION] ⚠️ MA trend opposite ({trend}) but relaxed mode active for {instrument} ({side})")
                else:
                    print(f"[VALIDATION] ❌ MA trend misaligned for {instrument}: {trend} vs side {side}")
                    return False
            else:
                print(f"[VALIDATION] ✅ MA trend aligned for {instrument}: {trend}")


        # Support/Resistance proximity: avoid chasing into nearby levels (<0.25*ATR)
        # Use the provided client to fetch data instead of env vars
        try:
            support, resistance = get_support_resistance_levels(instrument.replace("_", ""), 120, oanda_client=client)
        except Exception:
            support, resistance = (None, None)
        atr_for_prox = calculate_atr(client, account_id, instrument) or 0.0
        if support and resistance and atr_for_prox > 0:
            price_ref = bid if side == "sell" else ask
            buffer = max(atr_for_prox * 0.25, 0.0)
            if side == "buy" and (resistance - price_ref) <= buffer:
                print(f"[VALIDATION] ❌ Too close to resistance ({resistance:.5f}); buffer {buffer:.5f}")
                return False
            if side == "sell" and (price_ref - support) <= buffer:
                print(f"[VALIDATION] ❌ Too close to support ({support:.5f}); buffer {buffer:.5f}")
                return False

        # Regime hard-gate: require ADX and ATR% window to avoid chop
        # Use the provided client to fetch data instead of env vars
        try:
            if not passes_h4_hard_filters(instrument.replace("_", ""), side, oanda_client=client):
                return False
        except Exception as _:
            # If metrics unavailable, be conservative
            return False

        # Add more validation based on trade idea content
        idea_lower = trade_idea.lower()
        
        # Check for clear entry signals
        signal_words = ["breakout", "bounce", "rejection", "confirmation", "entry"]
        if not any(word in idea_lower for word in signal_words):
            print("[VALIDATION] ⚠️ Warning: No clear entry signal detected")
        
        return True
        
    except Exception as e:
        print(f"[VALIDATION] Error during validation: {e}")
        return False

def calculate_units_by_allocation(balance, allocation_percent, instrument, entry_price, account_currency):
    """Calculate units based on a percentage allocation of account balance.
    Attempts to map allocation in account currency to base units.
    For quote=account currency pairs (e.g., EUR_USD with USD account), units ≈ USD_alloc / price.
    For base=account currency pairs (e.g., USD_JPY with USD account), units ≈ USD_alloc.
    For other crosses, fallback to dividing by price as a conservative approximation.
    """
    try:
        allocated_value_in_acct_ccy = max(0.0, balance * (allocation_percent / 100.0))
        base, quote = instrument.split("_")
        if account_currency.upper() == quote.upper():
            units = int(allocated_value_in_acct_ccy / max(entry_price, 1e-9))
        elif account_currency.upper() == base.upper():
            units = int(allocated_value_in_acct_ccy)
        else:
            # Conservative fallback for cross-currency where neither leg matches account currency
            units = int(allocated_value_in_acct_ccy / max(entry_price, 1e-9))
        # Enforce reasonable bounds
        units = max(1000, min(units, 100000))
        return units
    except Exception:
        # Fallback minimum if anything goes wrong
        return 1000

def place_trade(trade_idea, direction=None, risk_pct=None, sl_price=None, tp_price=None, meta=None, client=None, account_id=None, user_id=None, trade_allocation=None):
    """
    Place a trade using the provided OANDA client and account_id.
    If client/account_id are not provided, falls back to environment variables (legacy behavior).
    NOTE: Per-user credentials should be passed explicitly via client and account_id parameters.
    
    Args:
        trade_idea: Trade idea text
        direction: Trade direction ('buy' or 'sell')
        risk_pct: Risk percentage (as fraction, e.g., 0.01 for 1%)
        sl_price: Stop loss price (optional)
        tp_price: Take profit price (optional)
        meta: Additional metadata dict
        client: OANDA API client (optional, uses env if not provided - legacy only)
        account_id: OANDA account ID (optional, uses env if not provided - legacy only)
        user_id: User ID for database tracking (optional)
        trade_allocation: Trade allocation percentage from user settings (optional, defaults to system logic)
    """
    if account_id is None:
        account_id = os.getenv("OANDA_ACCOUNT_ID")
        if not account_id:
            raise ValueError("OANDA_ACCOUNT_ID must be provided as parameter or set in environment (legacy mode)")
    if client is None:
        token = os.getenv("OANDA_API_KEY")
        if not token:
            raise ValueError("OANDA_API_KEY must be provided via client parameter or set in environment (legacy mode)")
        client = oandapyV20.API(access_token=token, environment="live")

    side = direction.lower() if direction else infer_trade_direction(trade_idea)
    if not side:
        raise ValueError("Could not determine trade direction.")

    instrument = extract_instrument(trade_idea, client, account_id=account_id)
    if not instrument:
        raise ValueError("Could not determine instrument/currency pair.")

    # Enhanced validation (pass user_id if available for per-user position checks)
    # For live enhanced mode, skip duplicate validation since enhanced validation already passed
    # This prevents legacy validation from blocking trades after enhanced validation has passed
    skip_duplicate = (user_id is not None)  # Skip duplicate validation for per-user trades (enhanced mode)
    if not validate_trade_entry(client, account_id, instrument, side, trade_idea, user_id=user_id, skip_duplicate_validation=skip_duplicate):
        raise ValueError("Trade validation failed - conditions not favorable")

    # Get current price with better timing
    current_price = get_current_price(client, account_id, instrument, side)
    
    # Wait for a more stable price (reduce slippage)
    time.sleep(1)
    stable_price = get_current_price(client, account_id, instrument, side)
    
    # Use the better price
    if side == "buy":
        intended_entry_price = min(current_price, stable_price)
    else:
        intended_entry_price = max(current_price, stable_price)
        
    entry_price = float(intended_entry_price)
    
    print(f"[PRICE] Initial: {current_price:.5f}, Stable: {stable_price:.5f}, Intended: {intended_entry_price:.5f}")

    # 📊 Get account balance and currency
    r_balance = account.AccountDetails(account_id)
    client.request(r_balance)
    balance = float(r_balance.response['account']['balance'])
    account_currency = r_balance.response['account'].get('currency', 'USD')

    # 📈 Calculate ATR for dynamic positioning
    atr = calculate_atr(client, account_id, instrument)
    if atr:
        print(f"[ATR] Average True Range: {atr:.5f}")
    
    # 🔒 Position sizing: use user trade_allocation if provided, otherwise use allocation-based or risk-based
    if trade_allocation is not None:
        # Use user's trade_allocation setting directly
        position_size = calculate_units_by_allocation(
            balance=balance,
            allocation_percent=trade_allocation,
            instrument=instrument,
            entry_price=entry_price,
            account_currency=account_currency,
        )
        sizing_mode = f"user allocation {trade_allocation:.2f}%"
    else:
        # Fallback to existing system logic
        use_allocation_percent = os.getenv("USE_ALLOCATION_PERCENT", "false").lower() == "true"
        allocation_percent = float(os.getenv("ALLOCATION_PERCENT", "10.0"))  # default 10% if enabled
        # Allow override via argument
        # If risk_pct provided from smart layer, it's a fraction (0.005..0.010). Convert to percent units.
        if risk_pct is not None:
            try:
                risk_percent = float(risk_pct) * 100.0
            except Exception:
                risk_percent = float(os.getenv("RISK_PERCENT", "1.0"))
        else:
            risk_percent = float(os.getenv("RISK_PERCENT", "1.0"))

        if use_allocation_percent:
            position_size = calculate_units_by_allocation(
                balance=balance,
                allocation_percent=allocation_percent,
                instrument=instrument,
                entry_price=entry_price,
                account_currency=account_currency,
            )
            sizing_mode = f"allocation {allocation_percent:.2f}%"
        elif atr:
            position_size = calculate_dynamic_position_size(balance, risk_percent, atr, instrument)
            sizing_mode = f"risk {risk_percent:.2f}% via ATR"
        else:
            # Fallback to percentage-based sizing (legacy behavior)
            position_size = int(balance * 0.02)
            position_size = max(1000, min(position_size, 50000))
            sizing_mode = "fallback 2% of balance"
    
    units = str(position_size) if side == "buy" else str(-position_size)

    # 📈 SL/TP logic with support for swing-based, fixed-percent, ATR-based, or explicit overrides
    use_fixed_sl_percent = os.getenv("USE_FIXED_SL_PERCENT", "false").lower() == "true"
    min_rr_ratio = float(os.getenv("MIN_RR_RATIO", "1.6"))

    # Respect explicit overrides if provided
    if sl_price is not None and tp_price is not None:
        sl_price = round_price(instrument, float(sl_price))
        tp_price = round_price(instrument, float(tp_price))
    elif use_fixed_sl_percent:
        fixed_sl_percent = float(os.getenv("FIXED_SL_PERCENT", "2.0"))  # e.g., 2% stop
        # Optional: user can also set TP as a percent; otherwise we keep R:R logic
        fixed_tp_percent_env = os.getenv("FIXED_TP_PERCENT")
        fixed_tp_percent = float(fixed_tp_percent_env) if fixed_tp_percent_env else None

        sl_delta = entry_price * (fixed_sl_percent / 100.0)
        if side == "buy":
            sl_price = round_price(instrument, entry_price - sl_delta)
            if fixed_tp_percent:
                tp_price = round_price(instrument, entry_price + entry_price * (fixed_tp_percent / 100.0))
            else:
                # provisional TP; will be adjusted by R:R check below
                tp_price = round_price(instrument, entry_price + sl_delta * max(min_rr_ratio, 1.8))
        else:
            sl_price = round_price(instrument, entry_price + sl_delta)
            if fixed_tp_percent:
                tp_price = round_price(instrument, entry_price - entry_price * (fixed_tp_percent / 100.0))
            else:
                tp_price = round_price(instrument, entry_price - sl_delta * max(min_rr_ratio, 1.8))
    elif atr:
        # Use ATR-based stops (more adaptive to market conditions)
        # Fix #1: Increased from 1.6 to 2.0x H4 ATR OR 2.5x M15 ATR (whichever larger) for 65-70% win rate
        atr_multiplier_sl = float(os.getenv("ATR_SL_MULTIPLIER", "2.0"))  # Increased for better win rate
        atr_multiplier_tp = float(os.getenv("ATR_TP_MULTIPLIER", "2.8"))  # tuned default 2.5–3.2
        
        # Get M15 ATR for execution-timeframe buffer (CRITICAL for 65-70% win rate)
        m15_atr_price_units = None
        pip_val = 0.01 if "JPY" in instrument else (0.1 if "XAU" in instrument else (0.01 if "XAG" in instrument else 0.0001))
        try:
            from validators import get_oanda_data, _calculate_true_ranges_from_hlc, _wilder_smooth
            m15_candles = get_oanda_data(instrument.replace("_", ""), "M15", 30, oanda_client=client)
            if m15_candles and len(m15_candles) >= 21:
                highs = [float(c["mid"]["h"]) for c in m15_candles]
                lows = [float(c["mid"]["l"]) for c in m15_candles]
                closes = [float(c["mid"]["c"]) for c in m15_candles]
                tr_list = _calculate_true_ranges_from_hlc(highs, lows, closes)
                m15_atr_series = _wilder_smooth(tr_list, 14)
                m15_atr = m15_atr_series[-1] if m15_atr_series else None
                if m15_atr:
                    m15_atr_price_units = m15_atr
                    m15_atr_pips = (m15_atr / pip_val) if pip_val > 0 else None
                    print(f"[TRADER] M15 ATR: {m15_atr_pips:.1f} pips (execution timeframe buffer)")
        except Exception as e:
            print(f"[TRADER] ⚠️ Could not calculate M15 ATR: {e}")
        
        # Use larger of: 2.0x H4 ATR or 2.5x M15 ATR (ensures execution noise buffer for 65-70% win rate)
        h4_sl_distance = atr * atr_multiplier_sl
        if m15_atr_price_units:
            m15_sl_distance = m15_atr_price_units * 2.5  # 2.5x M15 ATR for better protection
            sl_distance = max(h4_sl_distance, m15_sl_distance)
            print(f"[TRADER] SL calculation: H4={h4_sl_distance:.5f} ({atr_multiplier_sl}x), M15={m15_sl_distance:.5f} (2.5x) → Using {sl_distance:.5f}")
        else:
            sl_distance = h4_sl_distance
            print(f"[TRADER] SL calculation: H4={h4_sl_distance:.5f} ({atr_multiplier_sl}x) (M15 unavailable) → Using {sl_distance:.5f}")
        
        tp_distance = atr * atr_multiplier_tp
        
        # Ensure TP is always greater than SL
        min_rr_ratio_internal = max(1.8, min_rr_ratio)
        if tp_distance <= sl_distance * min_rr_ratio_internal:
            tp_distance = sl_distance * min_rr_ratio_internal
            print(f"[RISK] 🔧 Adjusted TP distance to {tp_distance:.5f} for better R:R")
        
        if side == "buy":
            sl_price = round_price(instrument, entry_price - sl_distance)
            tp_price = round_price(instrument, entry_price + tp_distance)
        else:
            sl_price = round_price(instrument, entry_price + sl_distance)
            tp_price = round_price(instrument, entry_price - tp_distance)
    else:
        # Fallback to percentage-based with guaranteed SL < TP
        base_sl_delta = float(os.getenv("SL_THRESHOLD", "0.004"))  # 0.4%
        base_tp_delta = float(os.getenv("TP_THRESHOLD", "0.008"))  # 0.8% (2:1 ratio)
        
        # Ensure minimum 1.8:1 ratio
        min_rr_ratio_internal = max(1.8, min_rr_ratio)
        if base_tp_delta <= base_sl_delta * min_rr_ratio_internal:
            base_tp_delta = base_sl_delta * min_rr_ratio_internal
            print(f"[RISK] 🔧 Adjusted TP delta to {base_tp_delta:.4f} for better R:R")
        
        if side == "buy":
            tp_price = round_price(instrument, entry_price * (1 + base_tp_delta))
            sl_price = round_price(instrument, entry_price * (1 - base_sl_delta))
        else:
            tp_price = round_price(instrument, entry_price * (1 - base_tp_delta))
            sl_price = round_price(instrument, entry_price * (1 + base_sl_delta))

    # Optional: swing-based stop override if enabled
    # IMPORTANT: Apply swing SL BEFORE TP adjustment to ensure proper R:R validation
    use_swing_sl = os.getenv("USE_SWING_SL", "true").lower() == "true"
    if use_swing_sl:
        recent_low, recent_high = _find_recent_swing_levels(instrument, side, lookback=30)
        if recent_low and recent_high:
            if side == "buy":
                swing_sl = round_price(instrument, recent_low)
                # Use tighter (lower) SL - swing SL should be <= ATR SL for buys
                if swing_sl <= sl_price:
                    sl_price = swing_sl
            else:
                swing_sl = round_price(instrument, recent_high)
                # Use tighter (higher) SL - swing SL should be >= ATR SL for sells
                if swing_sl >= sl_price:
                    sl_price = swing_sl

    # Calculate and validate risk-reward ratio
    if side == "buy":
        risk = entry_price - sl_price
        reward = tp_price - entry_price
    else:
        risk = sl_price - entry_price
        reward = entry_price - tp_price
    
    rr_ratio = reward / risk if risk > 0 else 0
    
    # Enforce minimum risk-reward ratio
    min_acceptable_rr = min_rr_ratio
    if rr_ratio < min_acceptable_rr:
        required_reward = risk * min_acceptable_rr
        
        if side == "buy":
            tp_price = round_price(instrument, entry_price + required_reward)
        else:
            tp_price = round_price(instrument, entry_price - required_reward)
        
        # Recalculate ratio
        if side == "buy":
            reward = tp_price - entry_price
        else:
            reward = entry_price - tp_price
        
        rr_ratio = reward / risk if risk > 0 else 0
        print(f"[RISK] 🔧 Adjusted TP to achieve minimum R:R: {rr_ratio:.2f}")
    
    # Final validation
    if rr_ratio < 1.5:
        print(f"[RISK] ⚠️ Warning: Risk-reward ratio still low: {rr_ratio:.2f}")
    else:
        print(f"[RISK] ✅ Good risk-reward ratio: {rr_ratio:.2f}")
    
    # SAFETY ASSERTION: Ensure SL distance is always less than TP distance
    if side == "buy":
        sl_distance_final = entry_price - sl_price
        tp_distance_final = tp_price - entry_price
    else:
        sl_distance_final = sl_price - entry_price
        tp_distance_final = entry_price - tp_price
    
    if sl_distance_final <= 0:
        raise ValueError(f"Invalid setup: SL distance must be positive, got {sl_distance_final:.5f}")
    
    if tp_distance_final <= 0:
        raise ValueError(f"Invalid setup: TP distance must be positive, got {tp_distance_final:.5f}")
    
    if sl_distance_final >= tp_distance_final:
        raise ValueError(
            f"Invalid setup: SL distance ({sl_distance_final:.5f}) >= TP distance ({tp_distance_final:.5f}). "
            f"This should not occur after swing SL and TP adjustments."
        )
    
    print(f"[RISK] 📏 SL distance: {sl_distance_final:.5f} | TP distance: {tp_distance_final:.5f}")

    order = {
        "order": {
            "instrument": instrument,
            "units": units,
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "takeProfitOnFill": {"price": str(tp_price)},
            "stopLossOnFill": {"price": str(sl_price)}
        }
    }

    print(f"[TRADE] Placing {side.upper()} order on {instrument}")
    print(f"[TRADE] Balance: ${balance:.2f} | Position Size: {abs(int(units))} | Sizing: {sizing_mode}")
    print(f"[TRADE] Intended Entry: {intended_entry_price:.5f} | TP: {tp_price:.5f} | SL: {sl_price:.5f}")
    print(f"[TRADE] Risk/Reward Ratio: {rr_ratio:.2f}")
    if atr:
        print(f"[TRADE] ATR: {atr:.5f} | {'SL fixed %' if use_fixed_sl_percent else 'ATR-based SL' if atr else 'fixed % fallback'}")

    # DIAGNOSTIC LOGGING: Pre-API call validation
    print(f"[OANDA][PRE-CALL] Preparing to send order to OANDA API")
    print(f"[OANDA][PRE-CALL] Account ID: {account_id}")
    print(f"[OANDA][PRE-CALL] Client initialized: {client is not None}")
    print(f"[OANDA][PRE-CALL] Order details: side={side}, units={units}, instrument={instrument}")
    print(f"[OANDA][PRE-CALL] Entry price: {intended_entry_price:.5f}, TP: {tp_price:.5f}, SL: {sl_price:.5f}")
    print(f"[OANDA][PRE-CALL] Order payload: {order}")

    try:
        # DIAGNOSTIC LOGGING: Before API call
        print(f"[OANDA][PRE-CALL] Sending order → side={side}, units={units}, price={intended_entry_price:.5f}, account={account_id}")
        
        r = orders.OrderCreate(accountID=account_id, data=order)
        print(f"[OANDA][PRE-CALL] OrderCreate object created, making API request...")
        
        client.request(r)
        print(f"[OANDA][RESPONSE] API request completed successfully")
        print(f"[OANDA][RESPONSE] Full response: {r.response}")
        
        # Extract trade ID with validation and fallback handling
        order_fill = r.response.get("orderFillTransaction", {})
        trade_id = None
        
        # Try primary path: tradeOpened
        if "tradeOpened" in order_fill:
            trade_id = order_fill["tradeOpened"].get("tradeID")
        
        # Fallback: tradesOpened (plural) for partial fills
        if not trade_id and "tradesOpened" in order_fill:
            trades_opened = order_fill["tradesOpened"]
            if isinstance(trades_opened, list) and len(trades_opened) > 0:
                trade_id = trades_opened[0].get("tradeID")
        
        # Final validation - fail if no trade ID found
        if not trade_id:
            error_msg = (
                f"Trade execution succeeded but no tradeID returned. "
                f"Response structure: {json.dumps(r.response, indent=2)[:1000]}"
            )
            print(f"[OANDA][ERROR] {error_msg}")
            raise ValueError(error_msg)
        
        fill_price = float(order_fill.get("price", intended_entry_price))
        
        print(f"[OANDA][RESPONSE] Trade ID: {trade_id}, Fill Price: {fill_price:.5f}")
        print(f"[TRADE] ✅ Order filled at: {fill_price:.5f}")

    except oandapyV20.exceptions.V20Error as e:
        print(f"[OANDA][ERROR] V20Error occurred during trade execution")
        print(f"[OANDA][ERROR] Error type: {type(e).__name__}")
        print(f"[OANDA][ERROR] Error message: {e}")
        print(f"[OANDA][ERROR] Error code: {getattr(e, 'code', 'N/A')}")
        print(f"[OANDA][ERROR] Error response: {e.response.text if hasattr(e, 'response') and hasattr(e.response, 'text') else 'No response body'}")
        print(f"[OANDA][ERROR] Full error details: {e}")
        import traceback
        print(f"[OANDA][ERROR] Traceback:")
        traceback.print_exc()
        print("[OANDA ERROR]", e)
        print("[OANDA ERROR BODY]", e.response.text if hasattr(e, 'response') else "No response body.")
        raise
    except Exception as e:
        print(f"[OANDA][ERROR] Unexpected exception during trade execution: {type(e).__name__}")
        print(f"[OANDA][ERROR] Error message: {e}")
        import traceback
        print(f"[OANDA][ERROR] Traceback:")
        traceback.print_exc()
        raise

    # Compute entry spread and slippage for logging
    try:
        spread_now, bid_now, ask_now = get_market_spread(client, account_id, instrument)
    except Exception:
        spread_now, bid_now, ask_now = (None, None, None)

    # pip value for pips conversion
    if "JPY" in instrument:
        pip_val = 0.01
    elif "XAU" in instrument:
        pip_val = 0.1
    elif "XAG" in instrument:
        pip_val = 0.01
    else:
        pip_val = 0.0001
    entry_spread_pips = (spread_now / pip_val) if (spread_now and pip_val) else 0.0
    entry_slippage_pips = abs(fill_price - intended_entry_price) / pip_val if pip_val else 0.0
    
    # Save trade to database (persistence layer) - AFTER computing spread/slippage
    if trade_id != "unknown":
        try:
            # Get commission_per_million from environment
            try:
                commission_per_million = float(os.getenv("COMMISSION_PER_MILLION", "0.0"))
            except Exception:
                commission_per_million = 0.0
            
            # Calculate commission if available
            commission = None
            if commission_per_million and units:
                # Commission is typically per million units
                commission_amount = (abs(units) / 1_000_000) * commission_per_million
                commission = commission_amount * fill_price if fill_price else None
            
            # Calculate spread cost
            spread_cost = None
            if spread_now and units:
                spread_cost = abs(spread_now * units)
            
            # Calculate slippage cost
            slippage_cost = None
            if entry_slippage_pips and pip_val and units:
                slippage_amount = entry_slippage_pips * pip_val
                slippage_cost = abs(slippage_amount * units)
            
            # Build reason_open from meta
            reason_parts = []
            if meta and isinstance(meta, dict):
                if meta.get("quality_score"):
                    reason_parts.append(f"quality_score={meta['quality_score']}")
                if meta.get("reasons"):
                    reason_parts.append(f"reasons={meta['reasons']}")
            if tp_price:
                reason_parts.append(f"tp={tp_price}")
            if sl_price:
                reason_parts.append(f"sl={sl_price}")
            reason_open = " ".join(reason_parts) if reason_parts else None
            
            # Save to database - REQUIRED, not optional
            # Try API sync first (if user_id provided), then fall back to direct DB write
            persistence_succeeded = False
            persistence_error = None
            
            if user_id is not None:
                # Enhanced mode: Try API sync first
                try:
                    from autopip_client import AutopipClient
                    autopip_client = AutopipClient()
                    autopip_client.post_trade({
                        "userId": user_id,
                        "externalTradeId": str(trade_id),
                        "symbol": instrument,
                        "side": side.upper(),
                        "size": abs(int(units)),
                        "entry": fill_price,
                        "tp": tp_price,
                        "sl": sl_price,
                        "status": "OPEN",
                        "pnl": None,
                        "openedAt": datetime.datetime.now(timezone.utc).isoformat(),
                        "closedAt": None,
                        "timeframe": meta.get("timeframe") if meta else None,
                        "oandaAccountId": account_id,
                    })
                    print(f"[DB] ✅ Trade {trade_id} saved to database via API sync for user {user_id}")
                    persistence_succeeded = True
                except ImportError as e:
                    # AutopipClient not available - fall through to direct DB
                    persistence_error = f"API client import failed: {e}"
                    print(f"[DB] ⚠️ API sync unavailable ({persistence_error}) - falling back to direct DB persistence")
                except Exception as api_error:
                    # API sync failed - fall through to direct DB
                    persistence_error = f"API sync failed: {api_error}"
                    print(f"[DB] ⚠️ API trade sync failed for trade {trade_id}, user {user_id}, account {account_id}")
                    print(f"[DB] ⚠️ Error: {persistence_error}")
                    print(f"[DB] 🔄 Falling back to direct DB persistence...")
            
            # Fallback to direct DB persistence if API sync failed or user_id not provided
            if not persistence_succeeded:
                try:
                    save_trade_from_oanda_account(
                        oanda_account_id=account_id,
                        external_id=str(trade_id),
                        instrument=instrument,
                        side=side,
                        units=abs(int(units)),
                        entry_price=fill_price,
                        opened_at=datetime.datetime.now(timezone.utc),
                        reason_open=reason_open,
                        commission=commission,
                        spread_cost=spread_cost,
                        slippage_cost=slippage_cost,
                    )
                    if persistence_error:
                        print(f"[DB] ✅ Trade {trade_id} saved to database via fallback (API sync had failed)")
                    else:
                        print(f"[DB] ✅ Trade {trade_id} saved to database (legacy mode)")
                    persistence_succeeded = True
                except Exception as db_error:
                    persistence_error = f"Direct DB persistence failed: {db_error}"
                    print(f"[DB] ❌ CRITICAL: All persistence paths failed for trade {trade_id}")
                    print(f"[DB] ❌ Trade executed on OANDA but NOT saved to database")
                    print(f"[DB] ❌ Final error: {persistence_error}")
                    import traceback
                    traceback.print_exc()
                    # Log critical failure - trade exists on OANDA but not in DB
                    # This will require reconciliation to fix
            
            if not persistence_succeeded:
                # This is a critical failure - trade exists on OANDA but not persisted
                print(f"[DB] 🚨 PERSISTENCE FAILURE: Trade {trade_id} on OANDA account {account_id} was NOT saved to database")
                print(f"[DB] 🚨 This trade will not appear in the dashboard until reconciliation runs")
        except Exception as db_error:
            # Log error but don't fail the trade execution
            print(f"[DB] ❌ Error saving trade to database: {db_error}")
            import traceback
            traceback.print_exc()

    # Provide smart trailing meta defaults so monitor can apply trailing
    # Reduced trail_start_r from 1.0 to 0.7 for earlier protection
    meta_out = meta.copy() if isinstance(meta, dict) else {}
    meta_out.setdefault("trail_start_r", 0.7)  # Reduced from 0.8 to 0.7 for earlier protection
    meta_out.setdefault("trail_step_pips", 4.0)
    # Attach execution/market microstructure details for downstream logging
    try:
        commission_per_million = float(os.getenv("COMMISSION_PER_MILLION", "0.0"))
    except Exception:
        commission_per_million = 0.0
    # Regime metrics snapshot
    try:
        regime_trend, regime_adx, regime_atr_pct = get_h4_trend_adx_atr_percent(instrument.replace("_", ""))
    except Exception:
        regime_trend, regime_adx, regime_atr_pct = (None, None, None)
    meta_out.update({
        "intended_entry_price": intended_entry_price,
        "entry_spread_pips": entry_spread_pips,
        "entry_slippage_pips": entry_slippage_pips,
        "commission_per_million": commission_per_million,
        "regime_trend": regime_trend,
        "regime_adx": regime_adx,
        "regime_atr_pct": regime_atr_pct,
    })

    return {
        "instrument": instrument,
        "side": side,
        "entry_price": fill_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "trade_id": trade_id,
        "position_size": abs(int(units)),
        "risk_reward_ratio": rr_ratio,
        "atr": atr,
        "meta": meta_out,
        "account_balance": balance,
        "account_id": account_id,
        "user_id": user_id,
    }

def round_price(pair, price):
    pair = pair.upper().replace("_", "/").strip()
    if "JPY" in pair:
        return round(price, 3)
    elif "XAU" in pair or "XAG" in pair:
        return round(price, 2)
    else:
        return round(price, 5)

def infer_trade_direction(text):
    text = text.lower()
    if re.search(r"\b(long|buy|bullish)\b", text):
        return "buy"
    elif re.search(r"\b(short|sell|bearish)\b", text):
        return "sell"
    return None

def extract_instrument(text, client, account_id=None):
    """Extract instrument from text. Requires account_id to be passed explicitly or set in env."""
    cleaned_text = text.lower().replace("/", "").replace(" ", "").replace("_", "")
    account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
    if not account_id:
        raise ValueError("OANDA_ACCOUNT_ID must be provided as parameter or set in environment")
    r = AccountInstruments(accountID=account_id)
    client.request(r)
    for item in r.response['instruments']:
        symbol = item['name']  # e.g., "EUR_USD"
        normalized = symbol.replace("_", "").lower()
        if normalized in cleaned_text:
            return symbol
    return None

def get_current_price(client, account_id, instrument, side):
    r = pricing.PricingInfo(accountID=account_id, params={"instruments": instrument})
    client.request(r)
    prices = r.response["prices"][0]
    bid = float(prices["bids"][0]["price"])
    ask = float(prices["asks"][0]["price"])
    return ask if side == "buy" else bid

