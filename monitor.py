import time
import os
import json
import oandapyV20
import oandapyV20.endpoints.pricing as pricing
from oandapyV20.endpoints.trades import TradeClose, TradeDetails, TradeCRCDO
from trade_cache import remove_trade
from trading_log import add_log_entry
from validators import get_momentum_signals, get_h4_trend_adx_atr_percent  # ADX live
# ^ added get_h4_trend_adx_atr_percent
from db_persistence import update_trade_close_from_oanda_account
from datetime import datetime, timezone

CACHE_FILE = "active_trades.json"


def _safe_price_from_pricing(resp, side, instrument):
    try:
        prices = resp["prices"][0]
        bid = float(prices["bids"][0]["price"])
        ask = float(prices["asks"][0]["price"])
        px = ask if side == "buy" else bid
        return round_price_by_pair(instrument, px)
    except Exception as e:
        # Log full response once to debug schema changes
        try:
            print(f"[MONITOR] Pricing schema error: {e}; raw={json.dumps(resp)[:500]}")
        except Exception:
            print(f"[MONITOR] Pricing schema error: {e}")
        return None


def round_price_by_pair(pair, price):
    pair = pair.upper().replace("_", "/").strip()
    if "JPY" in pair:
        return round(price, 3)
    elif "XAU" in pair or "XAG" in pair:
        return round(price, 2)
    else:
        return round(price, 5)


def calculate_trailing_stop(entry_price, current_price, side, trail_distance):
    """Calculate trailing stop loss based on favorable price movement"""
    if side == "buy":
        return round_price_by_pair("", current_price - trail_distance)
    else:
        return round_price_by_pair("", current_price + trail_distance)


def update_trailing_stop(client, account_id, trade_id, new_sl_price):
    """Update the stop loss for a trade"""
    try:
        data = {"stopLoss": {"price": str(new_sl_price)}}
        r = TradeCRCDO(accountID=account_id, tradeID=trade_id, data=data)
        client.request(r)
        print(f"[MONITOR] ✅ Trailing/SL updated to: {new_sl_price}")
        return True
    except Exception as e:
        print(f"[MONITOR] ❌ Error updating trailing stop: {e}")
        return False


def check_partial_profit_taking(client, account_id, trade_id, instrument, entry_price, current_price, side, position_size):
    """Take partial profits optimized for 4H trading"""
    try:
        if "JPY" in instrument:
            pip_multiplier = 100
            profit_target_pips = 30
        else:
            pip_multiplier = 10000
            profit_target_pips = 35

        if side == "buy":
            pips_profit = (current_price - entry_price) * pip_multiplier
        else:
            pips_profit = (entry_price - current_price) * pip_multiplier

        if pips_profit >= profit_target_pips:
            partial_size = int(position_size * 0.4)  # Close 40%
            close_data = {"units": str(partial_size)}
            close_req = TradeClose(accountID=account_id, tradeID=trade_id, data=close_data)
            client.request(close_req)
            print(f"[MONITOR] 💰 4H Partial profit taken: {partial_size} units at {current_price} ({pips_profit:.1f} pips)")
            print(f"[MONITOR] 📊 Remaining position: {position_size - partial_size} units")
            return True
    except Exception as e:
        print(f"[MONITOR] ❌ Error taking 4H partial profit: {e}")
    return False


def monitor_trade(trade_details):
    account_id = os.getenv("OANDA_ACCOUNT_ID")
    token = os.getenv("OANDA_API_KEY")
    client = oandapyV20.API(access_token=token, environment="live")

    instrument = trade_details["instrument"]
    entry_price = float(trade_details["entry_price"])
    sl_price = float(trade_details["sl_price"])
    tp_price = float(trade_details["tp_price"])
    side = trade_details["side"].lower()
    trade_id = trade_details.get("trade_id", "manual")
    position_size = trade_details.get("position_size", 1000)
    atr = trade_details.get("atr")

    # NEW: read smart meta if provided
    meta = trade_details.get("meta", {})
    trail_start_r = float(meta.get("trail_start_r", 1.0))      # start trailing at +1R
    trail_step_pips = float(meta.get("trail_step_pips", 5.0))  # gentle step increments
    plan_tp2 = meta.get("plan_tp2")
    quality_score = meta.get("quality_score")

    print(f"[MONITOR] Watching {instrument} | Entry: {entry_price}, SL: {sl_price}, TP: {tp_price}")

    # Initialize trailing stop variables
    trailing_stop_activated = False
    current_sl = sl_price
    partial_profit_taken = False
    partial_15_taken = False
    partial_20_taken = False
    moved_to_break_even = False
    guarantee_applied = False

    # Calculate trailing distance baseline using ATR with clamped multiplier
    if atr:
        try:
            atr_mult_env = float(os.getenv("ATR_TRAIL_MULTIPLIER", "1.3"))
        except Exception:
            atr_mult_env = 1.3
        # Clamp to [1.0, 1.5]
        atr_mult = max(1.0, min(1.5, atr_mult_env))
        trail_distance = atr * atr_mult
    else:
        trail_distance = 0.004 if "JPY" not in instrument else 0.04
    print(f"[MONITOR] 4H Trailing baseline distance: {trail_distance:.5f} ({'ATR-based' if atr else 'default'})")

    # Precompute risk/reward
    if side == "buy":
        risk_price = entry_price - sl_price
        reward_price = tp_price - entry_price
    else:
        risk_price = sl_price - entry_price
        reward_price = entry_price - tp_price

    if "JPY" in instrument:
        pip_value = 0.01
        pip_multiplier = 100
    else:
        pip_value = 0.0001
        pip_multiplier = 10000

    risk_pips = risk_price / pip_value if pip_value else 0
    reward_pips = reward_price / pip_value if pip_value else 0

    risk_per_unit = abs(entry_price - sl_price) if side == "buy" else abs(sl_price - entry_price)

    while True:
        try:
            # 🔍 Check if trade still exists
            try:
                trade_check = TradeDetails(accountID=account_id, tradeID=trade_id)
                client.request(trade_check)
                td = trade_check.response.get("trade", {})
                current_units = abs(int(trade_check.response["trade"]["currentUnits"]))
                unrealized_pl = float(td.get("unrealizedPL", "0") or 0.0)
            except oandapyV20.exceptions.V20Error:
                print(f"[MONITOR] Trade {trade_id} no longer exists. Removing from cache.")
                # Try to get final trade details before it's gone
                try:
                    # Get the last known price as exit price
                    r = pricing.PricingInfo(accountID=account_id, params={"instruments": instrument})
                    client.request(r)
                    exit_price = _safe_price_from_pricing(r.response, side, instrument)
                    
                    # Calculate P/L estimate (we don't have the actual realized P/L anymore)
                    if exit_price and entry_price:
                        if side == "buy":
                            pnl_estimate = (exit_price - entry_price) * position_size
                        else:
                            pnl_estimate = (entry_price - exit_price) * position_size
                    else:
                        pnl_estimate = None
                    
                    # Update database (persistence layer)
                    try:
                        update_trade_close_from_oanda_account(
                            oanda_account_id=account_id,
                            external_id=str(trade_id),
                            exit_price=exit_price,
                            pnl_net=pnl_estimate,
                            closed_at=datetime.now(timezone.utc),
                            reason_close="CLOSED_EXTERNALLY",
                        )
                        print(f"[DB] ✅ Trade {trade_id} closed status saved to database (externally)")
                    except Exception as db_error:
                        print(f"[DB] ❌ Error saving trade close to database: {db_error}")
                except Exception as e:
                    print(f"[MONITOR] ⚠️ Could not get final trade details: {e}")
                
                remove_trade(trade_id)
                return {"status": "CLOSED_EXTERNALLY", "message": "Trade was closed externally"}

            if current_units == 0:
                print(f"[MONITOR] Trade {trade_id} closed (units=0). Removing from cache.")
                
                # Get final trade details and update database
                try:
                    # Get exit price and realized P/L from trade details
                    exit_price = None
                    pnl_net = None
                    
                    try:
                        # When trade is closed (units=0), trade details may still be queryable
                        # Try to get average close price and realized P/L
                        avg_close_price = td.get("averageClosePrice")
                        if avg_close_price:
                            exit_price = float(avg_close_price)
                        
                        # Try to get realized P/L (OANDA may provide this when trade is closed)
                        realized_pl = td.get("realizedPL")
                        if realized_pl is not None:
                            pnl_net = float(realized_pl)
                    except Exception:
                        pass
                    
                    # If we don't have exit price, get current market price as fallback
                    if not exit_price:
                        try:
                            r = pricing.PricingInfo(accountID=account_id, params={"instruments": instrument})
                            client.request(r)
                            exit_price = _safe_price_from_pricing(r.response, side, instrument)
                        except Exception:
                            pass
                    
                    # If we don't have P/L, calculate estimate from entry/exit prices
                    if pnl_net is None and exit_price and entry_price:
                        if side == "buy":
                            pnl_net = (exit_price - entry_price) * position_size
                        else:
                            pnl_net = (entry_price - exit_price) * position_size
                    
                    # Update database (persistence layer)
                    try:
                        update_trade_close_from_oanda_account(
                            oanda_account_id=account_id,
                            external_id=str(trade_id),
                            exit_price=exit_price,
                            pnl_net=pnl_net,
                            closed_at=datetime.now(timezone.utc),
                            reason_close="CLOSED",
                        )
                        print(f"[DB] ✅ Trade {trade_id} closed status saved to database")
                    except Exception as db_error:
                        print(f"[DB] ❌ Error saving trade close to database: {db_error}")
                except Exception as e:
                    print(f"[MONITOR] ⚠️ Could not update database for closed trade: {e}")
                
                remove_trade(trade_id)
                return {"status": "CLOSED", "message": "Units zero; broker shows trade closed"}

            # 📈 Get current price
            r = pricing.PricingInfo(accountID=account_id, params={"instruments": instrument})
            client.request(r)
            current_price = _safe_price_from_pricing(r.response, side, instrument)
            if current_price is None:
                time.sleep(15)
                continue

            print(f"[MONITOR] Current price: {current_price} | Units: {current_units}")

            pips_profit = ((current_price - entry_price) if side == "buy" else (entry_price - current_price)) * pip_multiplier
            print(f"[MONITOR] Current profit: {pips_profit:.1f} pips")
            # # Current pips profit
            # if side == "buy":
            #     pips_profit = (current_price - entry_price) * pip_multiplier
            # else:
            #     pips_profit = (entry_price - current_price) * pip_multiplier
            # print(f"[MONITOR] Current profit: {pips_profit:.1f} pips")

            # # (All your trailing stop, partial, and exit logic continues here...)

            # time.sleep(10)
            # === Move SL to breakeven at +R ===
            if not moved_to_break_even and risk_pips > 0 and pips_profit >= trail_start_r * risk_pips:
                be_price = entry_price if side == "buy" else entry_price
                if update_trailing_stop(client, account_id, trade_id, be_price):
                    moved_to_break_even = True
                    current_sl = be_price
                    print(f"[MONITOR] 🛡️ Moved SL to breakeven at {be_price}")

            # === Start trailing once profit continues ===
            if moved_to_break_even and atr:
                step_price = calculate_trailing_stop(entry_price, current_price, side, trail_distance)
                if (side == "buy" and step_price > current_sl) or (side == "sell" and step_price < current_sl):
                    if update_trailing_stop(client, account_id, trade_id, step_price):
                        current_sl = step_price
                        print(f"[MONITOR] 🔧 Trailing SL moved to {step_price}")
                        
                        # === Optional partial at fixed pips or RR milestones ===
                if not partial_profit_taken and pips_profit >= max(30 if "JPY" in instrument else 35, risk_pips):
                    try:
                        partial_size = max(1, int(current_units * 0.4))
                        close_req = TradeClose(accountID=account_id, tradeID=trade_id, data={"units": str(partial_size)})
                        client.request(close_req)
                        partial_profit_taken = True
                        print(f"[MONITOR] 💰 Partial close {partial_size} units at {current_price}")
                    except Exception as e:
                        print(f"[MONITOR] ❌ Partial close failed: {e}")

                time.sleep(10)
        except Exception as e:
            print("[MONITOR] Error:", e)
            time.sleep(30)
    
    # If we exit the loop, the trade should be closed
    # Update database before removing from cache
    try:
        # Get current price as exit price
        r = pricing.PricingInfo(accountID=account_id, params={"instruments": instrument})
        client.request(r)
        exit_price = _safe_price_from_pricing(r.response, side, instrument)
        
        # Calculate P/L estimate
        pnl_net = None
        if exit_price and entry_price:
            if side == "buy":
                pnl_net = (exit_price - entry_price) * position_size
            else:
                pnl_net = (entry_price - exit_price) * position_size
        
        # Update database (persistence layer)
        try:
            update_trade_close_from_oanda_account(
                oanda_account_id=account_id,
                external_id=str(trade_id),
                exit_price=exit_price,
                pnl_net=pnl_net,
                closed_at=datetime.now(timezone.utc),
                reason_close="CLOSED",
            )
            print(f"[DB] ✅ Trade {trade_id} closed status saved to database")
        except Exception as db_error:
            print(f"[DB] ❌ Error saving trade close to database: {db_error}")
    except Exception as e:
        print(f"[MONITOR] ⚠️ Could not update database before exit: {e}")

    remove_trade(trade_id)
    return {"status": "DONE"}


def monitor_open_trades():
    if not os.path.exists(CACHE_FILE):
        print("[MONITOR] No trades to monitor.")
        return

    with open(CACHE_FILE, "r") as f:
        trades = json.load(f)

    if not trades:
        print("[MONITOR] Trade cache is empty.")
        return

    print(f"[MONITOR] Found {len(trades)} open trade(s) to monitor...")

    for trade in trades:
        try:
            monitor_trade(trade)
        except Exception as e:
            print(f"[MONITOR] Error monitoring trade {trade.get('symbol')}: {e}")
