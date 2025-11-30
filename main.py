import os
import json
import hashlib
import re
import time
from datetime import datetime, timezone
from scraper import get_trade_ideas
from gpt_utils import evaluate_top_ideas
from trader import place_trade
from monitor import monitor_trade
from email_utils import send_email
from signal_broadcast import send_signal
from trade_email_helpers import send_admin_trade_notification
from filters import rule_based_filter
from validators import get_rsi, get_ema
from trade_cache import is_trade_active, add_trade, remove_trade, get_active_trades
from trading_log import add_log_entry, get_daily_performance, load_log, get_pair_performance
from validators import is_forex_pair
from dotenv import load_dotenv
import openai
from oandapyV20.endpoints.accounts import AccountInstruments
from oandapyV20 import API
from idea_guard import filter_fresh_ideas_by_registry, evaluate_trade_gate, record_executed_idea

# NEW: Smart planner
from smart_layer import plan_trade
import oandapyV20.endpoints.pricing as pricing

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
TP_THRESHOLD = float(os.getenv("TP_THRESHOLD", 0.55))
SL_THRESHOLD = float(os.getenv("SL_THRESHOLD", 0.3))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
CACHE_FILE = "gpt_cache.json"


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def get_hash(ideas):
    return hashlib.sha256("".join([idea["description"] for idea in ideas]).encode()).hexdigest()


def extract_direction_from_text(text: str):
    """
    Infer 'buy' or 'sell' from:
      1) Explicit keywords (buy/sell/long/short, bullish/bearish, upside/downside, break above/below)
      2) Numeric inference: compare target vs. current/price -> target > current => buy; target < current => sell
    Returns: 'buy' | 'sell' | None
    """
    lowered = text.lower()

    # 1) Direct/strong keywords
    buy_terms = [
        "buy", "long", "bullish", "upside", "rally", "break above", "breakout above",
        "break up", "break to the upside", "higher highs", "higher low", "push higher",
    ]
    sell_terms = [
        "sell", "short", "bearish", "downside", "dump", "break below", "breakout below",
        "break down", "break to the downside", "lower lows", "lower high", "push lower",
    ]
    if any(t in lowered for t in buy_terms):
        return "buy"
    if any(t in lowered for t in sell_terms):
        return "sell"

    # 2) Generic "breakout" without direction → try to infer by context words
    if "breakout" in lowered or "break out" in lowered:
        if any(w in lowered for w in ["above", "over", "up", "higher", "upside"]):
            return "buy"
        if any(w in lowered for w in ["below", "under", "down", "lower", "downside"]):
            return "sell"
        # fall through to numeric inference if ambiguous

    # 3) Numeric inference (robust to phrasing)
    # current/price now
    current_re = re.search(
        r"(?:current(?:ly)?|price(?:\s+is)?|now|around|near)\s*(?:at|around|near|=|:)?\s*(-?\d+(?:\.\d+)?)",
        lowered
    )
    # target / projection / tp
    target_re = re.search(
        r"(?:target|tp|take\s*profit|projection|objective|goal|aim)\s*(?:zone|point|area)?\s*(?:at|near|around|=|:)?\s*(-?\d+(?:\.\d+)?)",
        lowered
    )

    if current_re and target_re:
        try:
            current = float(current_re.group(1))
            target = float(target_re.group(1))
            # Simple comparison tells direction
            if target > current:
                return "buy"
            elif target < current:
                return "sell"
        except ValueError:
            pass  # fall through

    # 4) Secondary numeric fallback:
    # If we see two numbers and the second is labeled like a target-ish phrase nearby,
    # assume first≈current, second≈target.
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", lowered)]
    if len(nums) >= 2:
        # Check if a target-ish word appears closer to the last number
        last_targetish = re.search(r"(target|tp|projection|objective|goal)[^0-9\-]*(-?\d+(?:\.\d+)?)", lowered)
        if last_targetish:
            try:
                # Compare last number to first number
                current, target = nums[0], float(last_targetish.group(2))
                if target > current:
                    return "buy"
                elif target < current:
                    return "sell"
            except ValueError:
                pass

    # 5) Still unknown
    return None


def extract_instrument_from_text(text, client, account_id=None):
    clean_text = text.lower().replace("/", "").replace("_", "").replace(" ", "")
    account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
    r = AccountInstruments(accountID=account_id)
    client.request(r)
    for item in r.response.get("instruments", []):
        symbol = item["name"]  # e.g., "USD_CAD"
        normalized = symbol.replace("_", "").lower()
        if normalized in clean_text:
            return symbol.replace("_", "")  # e.g., "USDCAD"
    return None


def _default_spread_pips(sym: str, api_key=None, account_id=None) -> float:
    """Fetch live spread in pips for symbol; fallback to 0.8 pips."""
    try:
        api_key = api_key or os.getenv("OANDA_API_KEY")
        account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
        if not api_key or not account_id:
            return 0.8
        client = API(access_token=api_key, environment="live")
        r = pricing.PricingInfo(accountID=account_id, params={"instruments": sym})
        client.request(r)
        prices = r.response["prices"][0]
        bid = float(prices["bids"][0]["price"])
        ask = float(prices["asks"][0]["price"])
        spread = max(0.0, ask - bid)
        s = sym.upper().replace("_", "")
        if s.endswith("JPY"):
            pip = 0.01
        elif s == "XAUUSD":
            pip = 0.1
        elif s == "XAGUSD":
            pip = 0.01
        else:
            pip = 0.0001
        return spread / pip if pip else 0.8
    except Exception:
        return 0.8


def log_render_debug_info():
    """Debug helper to verify script is running on Render."""
    print("\n" + "=" * 60)
    print("🔍 RENDER DEBUG MODE ENABLED")
    print("=" * 60)
    
    # Current working directory
    cwd = os.getcwd()
    print(f"📁 Current working directory: {cwd}")
    
    # DRY_RUN and TRADING_MODE values
    print(f"🧪 DRY_RUN: {DRY_RUN}")
    trading_mode = os.getenv("TRADING_MODE", "Not set")
    print(f"📊 TRADING_MODE: {trading_mode}")
    
    # Required env vars (check presence only, don't print values)
    print("\n🔑 Environment Variables Status:")
    required_vars = {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
    }
    for var_name, var_value in required_vars.items():
        status = "✅" if var_value else "❌"
        print(f"  {status} {var_name}")
    
    # Note: OANDA credentials are supplied dynamically at runtime from user dashboard
    print("\nℹ️  Note: OANDA_API_KEY and OANDA_ACCOUNT_ID are supplied dynamically at runtime")
    
    print("=" * 60 + "\n")


def main():
    # Extra clarity for Render logs
    now = datetime.now(timezone.utc).isoformat()
    print(f"[BOT] === AutoPip trading run started at {now} UTC ===")

    print("[BOT] Starting trading session...")
    autopip_client = None
    autopip_user_id = None
    oanda_api_key = None
    oanda_account_id = None
    
    user_id_env = os.getenv("AUTOPIP_USER_ID")
    if user_id_env:
        # If AUTOPIP_USER_ID is set, we MUST connect to the API to verify entitlements
        # and get broker credentials. This is a user's account - we can't trade without
        # dashboard connectivity.
        try:
            from autopip_client import AutopipClient
            autopip_user_id = int(user_id_env)
            print(f"[BOT] 🔗 Connecting to dashboard for user {autopip_user_id}...")
            autopip_client = AutopipClient()
            
            # Verify entitlements - user must be allowed to trade
            print("[BOT] 📋 Checking trading entitlements...")
            entitlements = autopip_client.get_entitlements(autopip_user_id)
            if not entitlements.get("canTrade"):
                print("[BOT] 🚫 Trading disabled by entitlement rules.")
                return
            
            # Get broker credentials - required to trade on user's account
            print("[BOT] 🔑 Fetching broker credentials...")
            broker_creds = autopip_client.get_broker(autopip_user_id)
            oanda_api_key = broker_creds.get("oandaApiKey")
            oanda_account_id = broker_creds.get("oandaAccountId")
            
            if not oanda_api_key or not oanda_account_id:
                print("[BOT] ❌ Broker credentials not available. Cannot trade without dashboard connection.")
                print("[BOT] Please ensure your broker account is connected in the dashboard.")
                return
            
            os.environ["OANDA_API_KEY"] = oanda_api_key
            os.environ["OANDA_ACCOUNT_ID"] = oanda_account_id
            print("[BOT] ✅ Dashboard connection successful. Trading enabled.")
            
        except ImportError as e:
            print(f"[BOT] ❌ Cannot import API client: {e}")
            print("[BOT] 🛑 Cannot trade without dashboard connection when AUTOPIP_USER_ID is set.")
            print("[BOT] This is a user account - dashboard must be accessible to verify entitlements and get credentials.")
            return
        except Exception as exc:
            print(f"[BOT] ❌ Failed to connect to dashboard: {exc}")
            print("[BOT] 🛑 Cannot trade without dashboard connection when AUTOPIP_USER_ID is set.")
            print("[BOT] Please ensure:")
            print("  - AUTOPIP_API_BASE_URL is set correctly")
            print("  - BOT_API_KEY is set correctly")
            print("  - Dashboard API is accessible")
            print("  - User has valid entitlements and broker credentials")
            return

    trade_ideas = get_trade_ideas()
    print(f"[BOT] Fetched {len(trade_ideas)} trade ideas.")

    # Normalize trade ideas
    normalized_ideas = []
    for idea in trade_ideas:
        if isinstance(idea, dict):
            normalized_ideas.append(idea)
        else:
            normalized_ideas.append({"description": idea})

    # Print raw ideas after normalization
    print("[BOT] Raw ideas before filtering:")
    for idx, idea in enumerate(normalized_ideas):
        print(f"  Idea #{idx+1}: {idea}")
        if "description" not in idea:
            print("  ⚠️ Missing 'description' key in idea!")

    filtered_ideas = [idea for idea in normalized_ideas if rule_based_filter(idea["description"])]

    # New: filter by freshness/duplicate ideas registry
    filtered_ideas = filter_fresh_ideas_by_registry(filtered_ideas)

    if not filtered_ideas:
        print("[BOT] No ideas passed rule-based and freshness filtering.")
        return

    top_ideas = filtered_ideas[:3]
    cache = load_cache()
    cache_key = get_hash(top_ideas)

    # Check cache
    if cache_key in cache:
        print("[BOT] Using cached GPT evaluation.")
        best = cache[cache_key]
    else:
        result = evaluate_top_ideas([idea["description"] for idea in top_ideas])
        if not result:
            print("[BOT] GPT error during top idea evaluation.")
            return
        print(f"[DEBUG] Raw GPT result: {result}")
        try:
            best = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError:
            print("[BOT] Failed to parse GPT response. Skipping.")
            return
        cache[cache_key] = best
        save_cache(cache)

    # Final validation
    if not isinstance(best, dict):
        print(f"[BOT] Invalid GPT response format: {type(best)} – {best}")
        return
    if "idea" not in best or "score" not in best:
        print("[BOT] Missing expected keys in GPT result. Skipping.")
        return

    selected_idea = best["idea"]
    score = best.get("score", 0)
    reason = best.get("reason", "No reason provided.")

    # Determine direction
    direction = extract_direction_from_text(selected_idea)
    if not direction:
        print("[BOT] ❌ Could not determine trade direction.")
        return
    print(f"[BOT] ✅ Trade direction: {direction}")

    api_key = oanda_api_key or os.getenv("OANDA_API_KEY")
    account_id = oanda_account_id or os.getenv("OANDA_ACCOUNT_ID")
    if not api_key:
        print("[BOT] ❌ OANDA_API_KEY not available")
        return
    client = API(access_token=api_key, environment="live")
    symbol = extract_instrument_from_text(selected_idea, client, account_id=account_id)

    print(f"[GPT] Selected Idea: {selected_idea}\nScore: {score}\nReason: {reason}")

    if not symbol:
        print("[BOT] No symbol provided. Skipping idea.")
        return
    if not isinstance(symbol, str) or not is_forex_pair(symbol):
        print(f"[BOT] Skipping non-Forex idea: {symbol}")
        return
    if is_trade_active(symbol, direction):
        print("[BOT] Trade already active on this pair and direction. Skipping.")
        return

    # ===== Risk Guards =====
    # 1) Daily loss cap
    try:
        max_daily_loss_usd = float(os.getenv("MAX_DAILY_LOSS_USD", "100"))
        today_trades = get_daily_performance(1)
        from datetime import datetime as dt_local
        today_str = dt_local.now().date().isoformat()
        todays_realized = 0.0
        for t in today_trades:
            try:
                if dt_local.fromisoformat(t.get("timestamp", "")).date().isoformat() == today_str:
                    todays_realized += float(t.get("profit_amount", 0) or 0)
            except Exception:
                continue
        if todays_realized <= -abs(max_daily_loss_usd):
            print(f"[BOT] 🚫 Daily loss cap reached ({todays_realized:.2f} ≤ -{abs(max_daily_loss_usd):.2f}). Halting new trades for today.")
            return
    except Exception as e:
        print(f"[BOT] ⚠️ Daily loss guard check failed: {e}")

    # 2) Consecutive losses halt
    try:
        max_consec_losses = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
        log = load_log()
        consec = 0
        for entry in reversed(log):
            pips = entry.get("pips_profit", 0)
            if pips is None:
                continue
            if pips < 0:
                consec += 1
            elif pips > 0:
                break
        if consec >= max_consec_losses:
            print(f"[BOT] 🚫 Max consecutive losses reached ({consec} ≥ {max_consec_losses}). Pausing entries.")
            return
    except Exception as e:
        print(f"[BOT] ⚠️ Consecutive loss guard check failed: {e}")

    # 3) Correlation/currency exposure guard
    try:
        max_same_currency_exposure = int(os.getenv("MAX_SAME_CURRENCY_EXPOSURE", "1"))
        base = symbol[:3]
        quote = symbol[3:6]
        new_exposures = {
            (base, 1 if direction == "buy" else -1),
            (quote, -1 if direction == "buy" else 1),
        }
        exposure_counts = {}
        for t in get_active_trades():
            sym2 = t.get("symbol", "")
            if len(sym2) < 6:
                continue
            side2 = (t.get("direction") or t.get("side") or "").lower()
            b2, q2 = sym2[:3], sym2[3:6]
            exp2 = [
                (b2, 1 if side2 == "buy" else -1),
                (q2, -1 if side2 == "buy" else 1),
            ]
            for key in exp2:
                exposure_counts[key] = exposure_counts.get(key, 0) + 1
        for key in new_exposures:
            if exposure_counts.get(key, 0) >= max_same_currency_exposure:
                print(f"[BOT] 🚫 Exposure limit: {key[0]} direction {key[1]} already at {exposure_counts.get(key,0)} ≥ {max_same_currency_exposure}. Skipping.")
                return
    except Exception as e:
        print(f"[BOT] ⚠️ Exposure guard check failed: {e}")

    # New: idea gate evaluation (cooldown/time & price, structure confirmation, stale repost)
    gate = evaluate_trade_gate(symbol, direction, selected_idea, api_key=api_key, account_id=account_id)
    if not gate.get("allow", False):
        print(f"[BOT] 🚫 Idea gated. Reasons: {gate.get('blocks')}")
        # Send admin notification for rejection
        try:
            send_admin_trade_notification(
                event_type="REJECTED",
                pair=symbol,
                direction=direction.upper(),
                rationale=reason,
                score=score,
                gate_blocks=gate.get("blocks", []),
                additional_context={
                    "trade_idea": selected_idea,
                    "gate_tags": gate.get("tags", []),
                },
            )
        except Exception as e:
            print(f"[BOT] ⚠️ Failed to send rejection notification: {e}")
        return
    if gate.get("tags"):
        print(f"[BOT] ℹ️ Idea tags (soft): {gate['tags']}")

    # Enhanced technical validation
    from validators import validate_entry_conditions
    if not validate_entry_conditions(symbol, direction):
        print(f"[BOT] ❌ Technical conditions not favorable for {direction} {symbol}")
        # Send admin notification for validation failure
        try:
            send_admin_trade_notification(
                event_type="VALIDATION_ERROR",
                pair=symbol,
                direction=direction.upper(),
                rationale=reason,
                score=score,
                validation_errors=[f"Technical conditions not favorable for {direction} {symbol}"],
                additional_context={
                    "trade_idea": selected_idea,
                },
            )
        except Exception as e:
            print(f"[BOT] ⚠️ Failed to send validation error notification: {e}")
        return

    broker_account_id = oanda_account_id or os.getenv("OANDA_ACCOUNT_ID")

    # === SMART LAYER INTEGRATION ===
    plan = plan_trade(symbol, direction, spread_pips=_default_spread_pips(symbol, api_key=api_key, account_id=account_id))
    if not plan:
        print("[BOT] ❌ Smart plan could not be built. Skipping.")
        return

    quality_score = plan["quality_score"]
    risk_pct = plan["risk_pct"]
    exits = plan["exits"]           # {'sl','tp1','tp2','trail_start_r','trail_step_pips'}

    print(f"[BOT] 🧠 Smart plan → Quality={quality_score:.1f}, Risk={risk_pct*100:.2f}%")
    print(f"[BOT] 🎯 Exits → SL={exits['sl']:.6f}, TP1={exits['tp1']:.6f}, TP2={exits['tp2']:.6f}")

    # Execute trade
    if score >= TP_THRESHOLD:
        print("[BOT] 👍 Idea meets TP threshold. Proceeding...")
        if not DRY_RUN:
            try:
                # Assumes trader.place_trade can accept overrides; if not, see Option B in earlier note.
                trade_details = place_trade(
                    selected_idea,
                    direction,
                    risk_pct=risk_pct,
                    sl_price=exits["sl"],
                    tp_price=exits["tp1"],  # keep TP1 as broker TP; TP2 is for runner via monitor
                    meta={
                        "quality_score": quality_score,
                        "smart_exits": True,
                        "trail_start_r": exits["trail_start_r"],
                        "trail_step_pips": exits["trail_step_pips"],
                        "plan_tp2": exits["tp2"],
                        "timeframe": "H4",
                    },
                    client=client,
                    account_id=account_id
                )

                opened_at_iso = datetime.now(timezone.utc).isoformat()
                external_trade_id = trade_details.get("trade_id") or f"{symbol}-{int(time.time())}"

                position_size = int(trade_details.get("position_size") or 0)
                entry_price = trade_details.get("entry_price")
                tp_price = trade_details.get("tp_price")
                sl_price = trade_details.get("sl_price")

                if autopip_client and autopip_user_id is not None and broker_account_id:
                    try:
                        autopip_client.post_trade(
                            {
                                "userId": autopip_user_id,
                                "externalTradeId": external_trade_id,
                                "symbol": symbol,
                                "side": direction.upper(),
                                "size": position_size,
                                "entry": entry_price,
                                "tp": tp_price,
                                "sl": sl_price,
                                "status": "OPEN",
                                "pnl": None,
                                "openedAt": opened_at_iso,
                                "closedAt": None,
                                "timeframe": trade_details.get("meta", {}).get("timeframe"),
                                "oandaAccountId": broker_account_id,
                            }
                        )
                    except Exception as sync_err:
                        print(f"[BOT] ⚠️ Failed to sync trade (open): {sync_err}")

                    balance = trade_details.get("account_balance")
                    if balance is not None:
                        try:
                            autopip_client.post_equity_snapshot(
                                autopip_user_id,
                                broker_account_id,
                                balance=balance,
                                equity=balance,
                                margin_used=0.0,
                                timestamp=datetime.now(timezone.utc),
                            )
                        except Exception as equity_err:
                            print(f"[BOT] ⚠️ Failed to sync equity snapshot: {equity_err}")

                add_trade(symbol, direction, trade_details["entry_price"], trade_details.get("trade_id", "manual"))
                # Record executed idea in registry
                record_executed_idea(symbol, direction, selected_idea, trade_details["entry_price"])

                # Broadcast signal (sends admin diagnostic + user clean signal for OPEN)
                # Note: send_signal handles both admin notification and user signal for OPEN trades
                try:
                    send_signal({
                        "signal_id": f"{trade_details.get('trade_id', 'manual')}:OPEN",
                        "type": "OPEN",
                        "pair": symbol,
                        "direction": direction.upper(),
                        "entry": trade_details.get("entry_price"),
                        "sl": trade_details.get("sl_price"),
                        "tp": trade_details.get("tp_price"),
                        "rationale": reason,
                        "score": score,
                        "quality_score": quality_score,
                        "trade_details": trade_details,
                        "additional_context": {
                            "trade_idea": selected_idea,
                        },
                    })
                except Exception as e:
                    print(f"[BOT] ⚠️ Failed to send signal: {e}")

                result = monitor_trade(trade_details, api_key=api_key, account_id=account_id)  # monitor will read meta to do BE/trailing
                # Note: send_signal handles admin notification for CLOSE (no user emails for CLOSE)
                if autopip_client and autopip_user_id is not None and broker_account_id:
                    try:
                        autopip_client.post_trade(
                            {
                                "userId": autopip_user_id,
                                "externalTradeId": external_trade_id,
                                "symbol": symbol,
                                "side": direction.upper(),
                                "size": position_size,
                                "entry": entry_price,
                                "tp": tp_price,
                                "sl": sl_price,
                                "status": result.get("status", "CLOSED"),
                                "pnl": result.get("pnl"),
                                "openedAt": opened_at_iso,
                                "closedAt": datetime.now(timezone.utc).isoformat(),
                                "timeframe": trade_details.get("meta", {}).get("timeframe"),
                                "oandaAccountId": broker_account_id,
                            }
                        )
                    except Exception as sync_err:
                        print(f"[BOT] ⚠️ Failed to sync trade (close): {sync_err}")
                # Broadcast CLOSE signal once (idempotent by trade_id:CLOSE)
                # Note: send_signal sends admin notification only (no user emails for CLOSE)
                try:
                    send_signal({
                        "signal_id": f"{trade_details.get('trade_id', 'manual')}:CLOSE",
                        "type": "CLOSE",
                        "pair": symbol,
                        "direction": direction.upper(),
                        "entry": trade_details.get("entry_price"),
                        "sl": trade_details.get("sl_price"),
                        "tp": trade_details.get("tp_price"),
                        "rationale": result.get("status"),
                        "trade_details": {
                            "status": result.get("status"),
                            "pnl": result.get("pnl"),
                            "message": result.get("message"),
                        },
                        "additional_context": {
                            "trade_idea": selected_idea,
                            "score": score,
                            "quality_score": quality_score,
                        },
                    })
                except Exception as e:
                    print(f"[BOT] ⚠️ Failed to send close signal: {e}")
                remove_trade(trade_details.get("trade_id", "manual"))
                add_log_entry({"symbol": symbol, "result": result, "score": score, "quality_score": quality_score})

            except Exception as e:
                print("[BOT] Error placing or monitoring trade:", e)
                # Send admin notification for execution error
                try:
                    send_admin_trade_notification(
                        event_type="EXECUTION_ERROR",
                        pair=symbol,
                        direction=direction.upper(),
                        rationale=reason,
                        score=score,
                        error_message=str(e),
                        additional_context={
                            "trade_idea": selected_idea,
                        },
                    )
                except Exception as notify_err:
                    print(f"[BOT] ⚠️ Failed to send error notification: {notify_err}")
        else:
            print("[DRY RUN] Trade simulated. No real execution.")
    elif score <= SL_THRESHOLD:
        print("[BOT] ❌ Idea rejected. Score below SL threshold.")
        # Send admin notification for score rejection
        try:
            send_admin_trade_notification(
                event_type="REJECTED",
                pair=symbol,
                direction=direction.upper(),
                rationale=reason,
                score=score,
                validation_errors=[f"Score {score:.1f} below SL threshold {SL_THRESHOLD}"],
                additional_context={
                    "trade_idea": selected_idea,
                },
            )
        except Exception as e:
            print(f"[BOT] ⚠️ Failed to send rejection notification: {e}")
    else:
        print("[BOT] 🤔 Idea in neutral zone. No action taken.")


if __name__ == "__main__":
    print("[BOT] Script initializing...")
    print("[BOT] Environment loaded.")
    
    # Render debug helper (only if RENDER_DEBUG env var is set to "true")
    if os.getenv("RENDER_DEBUG", "").lower() == "true":
        log_render_debug_info()
    
    main()
