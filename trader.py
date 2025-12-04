import os
import re
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.accounts as account
from oandapyV20.endpoints.accounts import AccountInstruments
import datetime
import time
from datetime import timezone

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

# --- Correlation groups (prevent stacking highly correlated exposure) ---
CORRELATION_GROUPS = [
    {"name": "USD_MAJORS", "members": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]},
    {"name": "YEN_CROSSES", "members": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY"]},
]

def _normalize_symbol(symbol: str) -> str:
    return symbol.upper().replace("_", "")

def _is_correlated_with_open(symbol: str, user_id=None) -> bool:
    """Return True if symbol belongs to a correlation group with any active trade symbol.
    If user_id is provided, only checks trades for that user's account.
    """
    try:
        active = get_active_trades()
        # TODO: Filter active trades by user_id if provided (requires trade_cache enhancement)
        # For now, we check all active trades but this should be per-user in the future
        active_syms = {_normalize_symbol(t.get("symbol", t.get("instrument", ""))) for t in active}
        sym = _normalize_symbol(symbol)
        for group in CORRELATION_GROUPS:
            members = set(group["members"])
            if sym in members and any(a in members for a in active_syms):
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

def _ma_trend_direction(symbol: str) -> str:
    """Return 'bullish' or 'bearish' via EMA50 vs EMA200 on H4."""
    try:
        candles = get_oanda_data(symbol.replace("_", ""), "H4", 210)
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

def validate_trade_entry(client, account_id, instrument, side, trade_idea, user_id=None):
    """Enhanced validation before placing trade"""
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
        if _is_correlated_with_open(instrument, user_id=user_id):
            print(f"[VALIDATION] ❌ Correlation lockout: existing correlated exposure present")
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
       # Technical confirmations: MA trend direction alignment (EMA50 vs EMA200)
        trend = _ma_trend_direction(instrument)
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
        try:
            support, resistance = get_support_resistance_levels(instrument.replace("_", ""), 120)
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
        try:
            if not passes_h4_hard_filters(instrument.replace("_", ""), side):
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
    if not validate_trade_entry(client, account_id, instrument, side, trade_idea, user_id=user_id):
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
        atr_multiplier_sl = float(os.getenv("ATR_SL_MULTIPLIER", "1.6"))  # tuned default 1.5–1.8
        atr_multiplier_tp = float(os.getenv("ATR_TP_MULTIPLIER", "2.8"))  # tuned default 2.5–3.2
        
        # Ensure TP is always greater than SL
        min_rr_ratio_internal = max(1.8, min_rr_ratio)
        if atr_multiplier_tp <= atr_multiplier_sl * min_rr_ratio_internal:
            atr_multiplier_tp = atr_multiplier_sl * min_rr_ratio_internal
            print(f"[RISK] 🔧 Adjusted TP multiplier to {atr_multiplier_tp:.1f} for better R:R")
        
        if side == "buy":
            sl_distance = atr * atr_multiplier_sl
            tp_distance = atr * atr_multiplier_tp
            tp_price = round_price(instrument, entry_price + tp_distance)
            sl_price = round_price(instrument, entry_price - sl_distance)
        else:
            sl_distance = atr * atr_multiplier_sl
            tp_distance = atr * atr_multiplier_tp
            tp_price = round_price(instrument, entry_price - tp_distance)
            sl_price = round_price(instrument, entry_price + sl_distance)
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
    use_swing_sl = os.getenv("USE_SWING_SL", "true").lower() == "true"
    if use_swing_sl:
        recent_low, recent_high = _find_recent_swing_levels(instrument, side, lookback=30)
        if recent_low and recent_high:
            if side == "buy":
                swing_sl = round_price(instrument, min(sl_price, recent_low))
                if swing_sl < sl_price:
                    sl_price = swing_sl
            else:
                swing_sl = round_price(instrument, max(sl_price, recent_high))
                if swing_sl > sl_price:
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
    
    # Ensure SL distance is always less than TP distance
    if side == "buy":
        sl_distance_final = entry_price - sl_price
        tp_distance_final = tp_price - entry_price
    else:
        sl_distance_final = sl_price - entry_price
        tp_distance_final = entry_price - tp_price
    
    if sl_distance_final >= tp_distance_final:
        raise ValueError(f"Invalid setup: SL distance ({sl_distance_final:.5f}) >= TP distance ({tp_distance_final:.5f})")
    
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

    try:
        r = orders.OrderCreate(accountID=account_id, data=order)
        client.request(r)
        trade_id = r.response.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID", "unknown")
        fill_price = float(r.response.get("orderFillTransaction", {}).get("price", intended_entry_price))
        
        print(f"[TRADE] ✅ Order filled at: {fill_price:.5f}")

    except oandapyV20.exceptions.V20Error as e:
        print("[OANDA ERROR]", e)
        print("[OANDA ERROR BODY]", e.response.text if hasattr(e, 'response') else "No response body.")
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
            
            # Save to database (if user_id provided)
            if user_id is not None:
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
                    print(f"[DB] ✅ Trade {trade_id} saved to database for user {user_id}")
                except ImportError as e:
                    print(f"[DB] ⚠️ Optional API integration failed: {e}")
                    print("[DB] Continuing without syncing trades to the dashboard.")
                except Exception as db_error:
                    print(f"[DB] ❌ Error saving trade to database: {db_error}")
            else:
                # Legacy database save (for backward compatibility)
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
                    print(f"[DB] ✅ Trade {trade_id} saved to database")
                except Exception as db_error:
                    print(f"[DB] ❌ Error saving trade to database: {db_error}")
            print(f"[DB] ✅ Trade {trade_id} saved to database")
        except Exception as db_error:
            # Log error but don't fail the trade execution
            print(f"[DB] ❌ Error saving trade to database: {db_error}")
            import traceback
            traceback.print_exc()

    # Provide smart trailing meta defaults so monitor can apply trailing
    meta_out = meta.copy() if isinstance(meta, dict) else {}
    meta_out.setdefault("trail_start_r", 0.8)
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

