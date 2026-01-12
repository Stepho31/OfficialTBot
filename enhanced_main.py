"""
Enhanced Main Trading Logic with Market Scanner
Uses comprehensive market analysis instead of external trade ideas
"""

import os
import json
import time
from datetime import datetime
from typing import List, Dict, Optional

from market_scanner import get_market_opportunities, MarketOpportunity
from trader import place_trade
from monitor import monitor_trade
from email_utils import send_email
from signal_broadcast import send_signal
from trade_email_helpers import send_admin_trade_notification
from trade_cache import is_trade_active, add_trade, remove_trade, get_active_trades
from trading_log import add_log_entry
from trading_config import get_config
from dotenv import load_dotenv
from idea_guard import evaluate_trade_gate, record_executed_idea
from validators import validate_entry_conditions, passes_h4_hard_filters
from smart_layer import plan_trade
from circuit_breaker import get_circuit_breaker_status
from oandapyV20 import API as OandaAPI
import oandapyV20.endpoints.pricing as pricing
from user_helpers import get_tier2_users_for_automation, Tier2User
from oanda_helpers import create_oanda_client, get_user_open_positions, has_user_position_on_pair, get_user_active_pairs
from autopip_client import AutopipClient
from validators import get_oanda_data

load_dotenv()

# Import centralized DRY_RUN configuration
from trading_config import get_dry_run

# Import score constants from trading_config for consistency
from trading_config import BASE_MIN_SCORE, FREQUENCY_MIN_SCORE

class EnhancedTradingSession:
    """Enhanced trading session with market scanning"""
    
    def __init__(self):
        self.config = get_config()
        # Get DRY_RUN with production override
        self.dry_run = get_dry_run()
        
        # Force DRY_RUN off in production
        if os.getenv("ENVIRONMENT", "production").lower() == "production":
            self.dry_run = False
        
        # Prevent bot startup if DRY_RUN is still True
        if self.dry_run:
            raise RuntimeError(
                "❌ Bot startup aborted: DRY_RUN is enabled. Disable DRY_RUN to execute real trades."
            )
        
        # Add startup logging
        import logging
        logger = logging.getLogger(__name__)
        mode = "LIVE TRADING"
        logger.warning(f"[STARTUP MODE] Bot running in: {mode}")
        self.max_concurrent_trades = int(os.getenv("MAX_CONCURRENT_TRADES", "3"))
        # Use BASE_MIN_SCORE as the default minimum opportunity score for consistency
        self.min_opportunity_score = BASE_MIN_SCORE  # Always use BASE_MIN_SCORE constant
        self.max_trades_per_session = int(os.getenv("MAX_TRADES_PER_SESSION", "3"))  # Cap at 3 for frequency-first mode
        # Track trades executed this session for frequency-first mode
        self.trades_executed_this_session = 0
        # Track pairs traded this session to prevent duplicate trades per pair
        self.pairs_traded_this_session = set()
        self.session_stats = {
            "opportunities_found": 0,
            "trades_executed": 0,
            "trades_skipped": 0,
            "start_time": datetime.now()
        }
        # Initialize API client for fetching user settings
        try:
            self.api_client = AutopipClient()
        except Exception as e:
            print(f"[ENHANCED] ⚠️ Warning: Could not initialize AutopipClient: {e}")
            self.api_client = None
    
    def _confirm_h4_candle_state(self, symbol: str, oanda_client) -> bool:
        """
        Confirm H4 candle is >50% complete or M15 confirms structure.
        Fix #2: Ensures entry aligns with H4 structure, not mid-candle noise.
        
        Returns:
            True if entry should proceed, False if should wait for H4 candle to mature
        """
        try:
            # Get current H4 candle
            h4_candles = get_oanda_data(symbol, "H4", 2, oanda_client=oanda_client)
            if not h4_candles or len(h4_candles) < 1:
                print(f"[ENHANCED] ⚠️ H4 candle confirmation: No data available, allowing entry")
                return True  # Default allow if data unavailable
            
            current_candle = h4_candles[-1]
            candle_time_str = current_candle.get("time", "")
            if not candle_time_str:
                print(f"[ENHANCED] ⚠️ H4 candle confirmation: No time in candle, allowing entry")
                return True
            
            # Parse candle time (OANDA format: "2024-01-01T00:00:00.000000000Z")
            try:
                # Remove nanoseconds and Z, then parse
                clean_time = candle_time_str.replace("Z", "+00:00")
                if "." in clean_time:
                    # Remove fractional seconds
                    clean_time = clean_time.split(".")[0] + "+00:00"
                candle_time = datetime.fromisoformat(clean_time)
            except:
                # Fallback: try simpler format
                try:
                    candle_time = datetime.strptime(candle_time_str.split(".")[0], "%Y-%m-%dT%H:%M:%S")
                except:
                    print(f"[ENHANCED] ⚠️ Could not parse candle time: {candle_time_str}")
                    return True  # Allow entry on parse error
            
            # Use UTC for comparison
            now = datetime.utcnow()
            if candle_time.tzinfo:
                # Convert to UTC if timezone-aware
                from datetime import timezone
                candle_time_utc = candle_time.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                candle_time_utc = candle_time
            
            # H4 candles: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC
            hours_into_candle = (now - candle_time).total_seconds() / 3600.0
            
            # If >2 hours into 4-hour candle (>50%), allow entry
            if hours_into_candle >= 2.0:
                print(f"[ENHANCED] ✅ H4 candle confirmation: {hours_into_candle:.1f} hours into candle (>50%) - entry allowed")
                return True
            
            # If <2 hours, require STRONGER M15 confirmation for 65-70% win rate
            # Need H4 candle >60% complete OR very strong multi-timeframe confirmation
            if hours_into_candle < 2.4:  # Require >60% into H4 candle (was 50%)
                print(f"[ENHANCED] ⚠️ H4 candle confirmation: Only {hours_into_candle:.1f} hours into candle (<60%) - requiring strong M15 confirmation")
                m15_candles = get_oanda_data(symbol, "M15", 12, oanda_client=oanda_client)
                if m15_candles and len(m15_candles) >= 8:  # Need more candles for structure
                    # Get H4 direction from current candle
                    h4_current = float(h4_candles[-1]["mid"]["c"])
                    h4_prev = float(h4_candles[-2]["mid"]["c"]) if len(h4_candles) >= 2 else h4_current
                    h4_direction = "up" if h4_current > h4_prev else "down"
                    
                    # Check M15 structure alignment with H4
                    closes = [float(c["mid"]["c"]) for c in m15_candles[-8:]]
                    highs = [float(c["mid"]["h"]) for c in m15_candles[-8:]]
                    lows = [float(c["mid"]["l"]) for c in m15_candles[-8:]]
                    
                    # Require: (1) Clear trend AND (2) Structure alignment with H4 direction
                    m15_trend_up = all(closes[i] >= closes[i-1] for i in range(1, len(closes)))
                    m15_trend_down = all(closes[i] <= closes[i-1] for i in range(1, len(closes)))
                    
                    # Require: M15 trend matches H4 direction AND no major wicks against trend
                    if h4_direction == "up" and m15_trend_up:
                        # Check for wicks against trend (highs should be increasing)
                        recent_highs = highs[-4:]
                        if all(recent_highs[i] >= recent_highs[i-1] for i in range(1, len(recent_highs))):
                            print(f"[ENHANCED] ✅ H4 candle confirmation: Strong M15 structure confirms H4 direction ({hours_into_candle:.1f}h into H4 candle) - entry allowed")
                            return True
                    elif h4_direction == "down" and m15_trend_down:
                        # Check for wicks against trend (lows should be decreasing)
                        recent_lows = lows[-4:]
                        if all(recent_lows[i] <= recent_lows[i-1] for i in range(1, len(recent_lows))):
                            print(f"[ENHANCED] ✅ H4 candle confirmation: Strong M15 structure confirms H4 direction ({hours_into_candle:.1f}h into H4 candle) - entry allowed")
                            return True
                
                # Default: wait for H4 candle to mature (>60%)
                print(f"[ENHANCED] ⚠️ H4 candle confirmation: Only {hours_into_candle:.1f} hours into candle (<60%) and M15 not strongly confirming - delaying entry")
                return False
            
            # Default: wait for H4 candle to mature
            print(f"[ENHANCED] ⚠️ H4 candle confirmation: Only {hours_into_candle:.1f} hours into candle (<50%) and M15 not confirming - delaying entry")
            return False
        except Exception as e:
            print(f"[ENHANCED] ⚠️ H4 candle confirmation error: {e} - allowing entry as fallback")
            return True  # Default allow on error
        
    def execute_trading_session(self) -> Dict:
        """
        Execute a complete trading session with per-user automation.
        
        Flow:
        1. Fetch all Tier-2 users eligible for automation
        2. Compute trade ideas once (shared across all users)
        3. For each user:
           - Create OANDA client with their credentials
           - Fetch their open positions
           - Filter opportunities against their positions
           - Apply validation per user
           - Place orders per user
           - Send simplified signal emails to user, full details to admin
        """
        print("[ENHANCED] 🚀 Starting Enhanced 4H Trading Session (Per-User Mode)...")
        print(f"[ENHANCED] 📊 Max concurrent trades: {self.max_concurrent_trades}")
        print(f"[ENHANCED] 🎯 Min opportunity score: {self.min_opportunity_score}")
        # DRY_RUN should always be False at this point due to startup abort check
        mode = "LIVE TRADING"
        print(f"[ENHANCED] [STARTUP MODE] Bot running in: {mode}")
        
        # Check circuit breaker status
        cb_status = get_circuit_breaker_status()
        if cb_status["active"]:
            print(f"[ENHANCED] ⚠️ Circuit breaker ACTIVE: {cb_status['reason']}")
            print(f"[ENHANCED] ⚠️ Risk multiplier: {cb_status['risk_multiplier']:.2f}x, Frequency: {cb_status['frequency_multiplier']:.2f}x")
        
        # Step 1: Fetch Tier-2 users eligible for automation
        tier2_users = get_tier2_users_for_automation()
        if not tier2_users:
            print("[ENHANCED] ⚠️ No Tier-2 users found eligible for automation")
            return self._get_session_summary("no_users")
        
        print(f"[ENHANCED] 👥 Found {len(tier2_users)} Tier-2 users for automation")
        
        # Step 2: Compute trade ideas once (shared across all users)
        # Get a reasonable number of opportunities (enough for all users)
        # Use first user's credentials for market scanning (market data is the same for all users)
        max_opportunities = self.max_concurrent_trades * len(tier2_users) + 5
        first_user = tier2_users[0]
        opportunities = get_market_opportunities(
            max_opportunities, 
            api_key=first_user.oanda_api_key, 
            account_id=first_user.oanda_account_id
        )
        self.session_stats["opportunities_found"] = len(opportunities)
        
        if not opportunities:
            print("[ENHANCED] 📭 No trading opportunities found meeting criteria")
            print("[ENHANCED] 💡 Diagnostic: Scanner completed but no opportunities ≥48 score after sentiment/correlation adjustments")
            return self._get_session_summary("no_opportunities")
        
        # Filter opportunities by general criteria (score, confidence, correlation, session timing)
        # This filtering is independent of user positions
        print(f"[ENHANCED] 🔍 Filtering {len(opportunities)} opportunities by general criteria (min_score={self.min_opportunity_score:.1f})...")
        filtered_opportunities = self._filter_opportunities_general(opportunities)
        
        if not filtered_opportunities:
            print("[ENHANCED] 🚫 All opportunities filtered out by general criteria")
            print(f"[ENHANCED] 💡 Diagnostic: {len(opportunities)} opportunities found but none passed filters (score/confidence/correlation/session)")
            return self._get_session_summary("all_filtered")
        
        print(f"[ENHANCED] ✅ {len(filtered_opportunities)} opportunities passed general filters (out of {len(opportunities)} scanned)")
        
        # Step 3: Loop through each user and execute trades per account
        all_executed_trades = []
        
        for user in tier2_users:
            print(f"\n[ENHANCED] 👤 Processing user {user.user_id} ({user.email})")
            
            try:
                # Create OANDA client for this user
                user_client = create_oanda_client(user.oanda_api_key)
                
                # Fetch user's open positions
                user_positions = get_user_open_positions(user_client, user.oanda_account_id)
                user_active_pairs = get_user_active_pairs(user_client, user.oanda_account_id)
                
                print(f"[ENHANCED] 📊 User {user.user_id}: {len(user_positions)} open positions, {len(user_active_pairs)} active pairs")
                
                # Check if user has capacity for new trades
                if len(user_positions) >= self.max_concurrent_trades:
                    print(f"[ENHANCED] ⚠️ User {user.user_id}: SKIPPED - At max concurrent trades limit ({len(user_positions)}/{self.max_concurrent_trades})")
                    continue
                
                # Filter opportunities for this user (check against their positions)
                user_filtered_opps = self._filter_opportunities_for_user(
                    filtered_opportunities, 
                    user_positions, 
                    user_active_pairs,
                    user_client,
                    user.oanda_account_id
                )
                
                if not user_filtered_opps:
                    print(f"[ENHANCED] 📭 User {user.user_id}: No opportunities after position filtering ({len(filtered_opportunities)} available but all conflict with existing positions)")
                    continue
                
                # Initialize max_new_for_user safely before any print statements or execution logic
                max_new_for_user = max(0, self.max_concurrent_trades - len(user_positions))
                
                print(f"[ENHANCED] 🎯 User {user.user_id}: {len(user_filtered_opps)} opportunities available (user has {len(user_positions)}/{self.max_concurrent_trades} positions, capacity for {max_new_for_user} new trades)")
                
                # Execute trades for this user
                user_trades_executed = 0
                
                # Check circuit breaker status for frequency control
                cb_status = get_circuit_breaker_status()
                skip_count = 0
                
                for i, opportunity in enumerate(user_filtered_opps[:max_new_for_user]):
                    if user_trades_executed >= self.max_trades_per_session:
                        print(f"[ENHANCED] ⚠️ User {user.user_id}: SKIPPED - Reached per-session trade cap ({user_trades_executed}/{self.max_trades_per_session})")
                        break
                    
                    # Circuit breaker frequency control: skip trades if active
                    if cb_status["active"] and cb_status["frequency_multiplier"] < 1.0:
                        import random
                        if random.random() > cb_status["frequency_multiplier"]:
                            skip_count += 1
                            print(f"[ENHANCED] ⚠️ User {user.user_id}: {opportunity.symbol} {opportunity.direction}: REJECTED - Circuit breaker active (frequency multiplier: {cb_status['frequency_multiplier']:.2f}x, reason: {cb_status.get('reason', 'N/A')})")
                            continue
                    
                    print(f"\n[ENHANCED] 🎯 User {user.user_id}: Processing opportunity {i+1}/{len(user_filtered_opps)}")
                    
                    # Mandatory guardrails - apply in all modes including frequency-first
                    guardrail_result = self._check_mandatory_guardrails(opportunity, user_client)
                    if not guardrail_result["allowed"]:
                        print(f"[ENHANCED] 🚫 User {user.user_id}: {opportunity.symbol} {opportunity.direction}: REJECTED - Guardrail: {guardrail_result['reason']}")
                        self.session_stats["trades_skipped"] += 1
                        continue
                    
                    # Frequency-first mode: check if we need to use lower threshold for first trade
                    frequency_first_mode = (self.trades_executed_this_session == 0)
                    effective_min_score = FREQUENCY_MIN_SCORE if frequency_first_mode else BASE_MIN_SCORE
                    
                    # Check score threshold (using effective min score)
                    if opportunity.score < effective_min_score:
                        mode_str = "frequency-first" if frequency_first_mode else "normal"
                        print(f"[ENHANCED] 🚫 User {user.user_id}: {opportunity.symbol} {opportunity.direction}: REJECTED - Score {opportunity.score:.1f} < {effective_min_score} (mode: {mode_str})")
                        self.session_stats["trades_skipped"] += 1
                        continue
                    
                    # Frequency-first mode: ignore sentiment for first trade only
                    if frequency_first_mode:
                        print(f"[ENHANCED] 🔄 Frequency-first mode: Using FREQUENCY_MIN_SCORE ({FREQUENCY_MIN_SCORE}) for first trade, ignoring sentiment adjustments")
                        # Note: Sentiment is already applied in scanner, but for first trade we proceed anyway
                        # The lower threshold (FREQUENCY_MIN_SCORE) compensates for any negative sentiment
                    else:
                        # After first trade, use normal threshold and sentiment is already applied (clamped to max 3 points)
                        print(f"[ENHANCED] ✅ Normal mode: Using BASE_MIN_SCORE ({BASE_MIN_SCORE}), sentiment already applied (max ±3 pts)")
                    
                    # Check if we've already traded this pair this session (mandatory guardrail)
                    symbol_clean = opportunity.symbol.replace("_", "")
                    if symbol_clean in self.pairs_traded_this_session:
                        print(f"[ENHANCED] 🚫 User {user.user_id}: {opportunity.symbol} {opportunity.direction}: REJECTED - Already traded this pair this session")
                        self.session_stats["trades_skipped"] += 1
                        continue
                    
                    # Consolidated pre-entry validation (avoids redundant checks)
                    rechecks = int(os.getenv("PRE_ENTRY_RECHECKS", "2"))
                    recheck_sleep = int(os.getenv("PRE_ENTRY_RECHECK_SLEEP", "20"))
                    proceed = True
                    
                    for j in range(rechecks):
                        # Step 1: Gate check (cooldown/freshness - non-technical, fast)
                        gate = evaluate_trade_gate(opportunity.symbol.replace("_", ""), opportunity.direction,
                                                   f"Auto-opportunity score={opportunity.score}",
                                                   api_key=user.oanda_api_key, account_id=user.oanda_account_id)
                        if not gate.get("allow", False):
                            blocks = gate.get('blocks', [])
                            blocks_str = ', '.join(blocks) if blocks else 'unknown reason'
                            print(f"[ENHANCED] 🚫 User {user.user_id}: {opportunity.symbol} {opportunity.direction}: REJECTED - Gate blocked on recheck {j+1} (blocks: {blocks_str})")
                            self._send_admin_rejection_notification(opportunity, f"Gate blocked: {blocks_str} (recheck {j+1})", user)
                            proceed = False
                            break
                        
                        # Step 2: H4 hard filters FIRST (fastest technical check, includes trend/ADX/ATR%)
                        # Use relax=True to honor ALLOW_TREND_RELAX env var
                        # SAFETY LOG: Track validation order
                        if not passes_h4_hard_filters(opportunity.symbol.replace("_",""), opportunity.direction, relax=True, oanda_client=user_client):
                            print(f"[ENHANCED] 🚫 User {user.user_id}: {opportunity.symbol} {opportunity.direction}: REJECTED - H4 regime/hard filters blocked on recheck {j+1}")
                            self._send_admin_validation_error(opportunity, f"Regime gate blocked (recheck {j+1})", user)
                            proceed = False
                            break
                        
                        # Step 3: Detailed multi-timeframe validation (exclude H4 to avoid redundancy)
                        # Validate H1 and M15 only, since H4 was already checked above
                        # SAFETY LOG: Confirm we're not re-checking H4
                        if not validate_entry_conditions(opportunity.symbol.replace("_",""), opportunity.direction, timeframes=["H1","M15"], oanda_client=user_client):
                            print(f"[ENHANCED] 🚫 User {user.user_id}: {opportunity.symbol} {opportunity.direction}: REJECTED - Entry validation failed on recheck {j+1} (H1/M15 conditions not met)")
                            self._send_admin_validation_error(opportunity, f"Validation failed (recheck {j+1})", user)
                            proceed = False
                            break
                            
                        if j < rechecks - 1:
                            time.sleep(recheck_sleep)
                    
                    if not proceed:
                        self.session_stats["trades_skipped"] += 1
                        continue
                    
                    # Fix #2: Confirm H4 candle state before execution
                    symbol_clean = opportunity.symbol.replace("_", "")
                    if not self._confirm_h4_candle_state(symbol_clean, user_client):
                        print(f"[ENHANCED] ⚠️ User {user.user_id}: {opportunity.symbol} {opportunity.direction}: DELAYED - H4 candle not mature, will re-evaluate next cycle")
                        self.session_stats["trades_skipped"] += 1
                        continue
                    
                    # Execute trade for this user
                    mode_used = "frequency-first" if frequency_first_mode else "normal"
                    print(f"[ENHANCED] ✅ User {user.user_id}: {opportunity.symbol} {opportunity.direction}: APPROVED - Score {opportunity.score:.1f} >= {effective_min_score} (mode: {mode_used})")
                    trade_result = self._execute_opportunity_for_user(opportunity, user, user_client)
                    if trade_result:
                        all_executed_trades.append(trade_result)
                        self.session_stats["trades_executed"] += 1
                        self.trades_executed_this_session += 1
                        user_trades_executed += 1
                        # Track pair to prevent duplicate trades per session
                        self.pairs_traded_this_session.add(symbol_clean)
                        
                        print(f"[ENHANCED] ✅ Trade executed (session total: {self.trades_executed_this_session}/3, mode: {mode_used})")
                        
                        # Hard cap: never exceed 3 trades per session
                        if self.trades_executed_this_session >= 3:
                            print(f"[ENHANCED] ⚠️ Session trade cap reached (3 trades) - stopping execution")
                            break
                        
                        # Add delay between trades
                        if i < len(user_filtered_opps) - 1:
                            time.sleep(2)
                    else:
                        self.session_stats["trades_skipped"] += 1
                
                print(f"[ENHANCED] ✅ User {user.user_id}: Executed {user_trades_executed} trades")
                
                # Run reconciliation for this user to catch any missing trades
                try:
                    from db_persistence import reconcile_trades_from_oanda
                    print(f"[ENHANCED] 🔄 Running reconciliation for user {user.user_id}...")
                    reconcile_result = reconcile_trades_from_oanda(
                        oanda_account_id=user.oanda_account_id,
                        oanda_client=user_client,
                        user_id=user.user_id,
                    )
                    if reconcile_result["trades_inserted"] > 0:
                        print(f"[ENHANCED] ✅ Reconciliation: Inserted {reconcile_result['trades_inserted']} missing trades for user {user.user_id}")
                    if reconcile_result["trades_updated"] > 0:
                        print(f"[ENHANCED] ✅ Reconciliation: Updated {reconcile_result['trades_updated']} trades for user {user.user_id}")
                    if reconcile_result["errors"]:
                        print(f"[ENHANCED] ⚠️ Reconciliation errors for user {user.user_id}: {reconcile_result['errors']}")
                except Exception as reconcile_err:
                    print(f"[ENHANCED] ⚠️ Reconciliation failed for user {user.user_id}: {reconcile_err}")
                    # Don't fail the session if reconciliation fails
                
            except Exception as e:
                print(f"[ENHANCED] ❌ Error processing user {user.user_id}: {e}")
                import traceback
                traceback.print_exc()
                continue
        
        return self._get_session_summary("completed", all_executed_trades)
    
    def _check_mandatory_guardrails(self, opportunity: MarketOpportunity, user_client) -> Dict:
        """Check mandatory guardrails that apply in all modes including frequency-first.
        Returns dict with 'allowed' (bool) and 'reason' (str)."""
        symbol_clean = opportunity.symbol.replace("_", "")
        
        # Guardrail 1: Risk-reward ratio must be >= 1.3
        if opportunity.direction == 'buy':
            risk = opportunity.entry_price - opportunity.suggested_sl
            reward = opportunity.suggested_tp - opportunity.entry_price
        else:  # sell
            risk = opportunity.suggested_sl - opportunity.entry_price
            reward = opportunity.entry_price - opportunity.suggested_tp
        
        if risk <= 0:
            return {"allowed": False, "reason": "Invalid risk calculation (risk <= 0)"}
        
        rr_ratio = reward / risk if risk > 0 else 0
        if rr_ratio < 1.3:
            return {"allowed": False, "reason": f"Risk-reward ratio {rr_ratio:.2f} < 1.3"}
        
        # Guardrail 2: RSI must not be between 45 and 55 (neutral zone)
        if 45 <= opportunity.rsi <= 55:
            return {"allowed": False, "reason": f"RSI {opportunity.rsi:.1f} in neutral zone (45-55)"}
        
        # Guardrail 3: Require at least one strong confirmation
        strong_confirmations = []
        
        # RSI extremes
        if opportunity.rsi < 35 or opportunity.rsi > 65:
            strong_confirmations.append(f"RSI extreme ({opportunity.rsi:.1f})")
        
        # Strong trend alignment
        if (opportunity.direction == 'buy' and opportunity.trend == 'bullish') or \
           (opportunity.direction == 'sell' and opportunity.trend == 'bearish'):
            strong_confirmations.append("Strong trend alignment")
        
        # Favorable range position
        if opportunity.direction == 'buy' and opportunity.range_position < 0.30:
            strong_confirmations.append(f"Range position {opportunity.range_position:.2f} < 0.30")
        elif opportunity.direction == 'sell' and opportunity.range_position > 0.70:
            strong_confirmations.append(f"Range position {opportunity.range_position:.2f} > 0.70")
        
        if not strong_confirmations:
            return {"allowed": False, "reason": "No strong confirmation (need RSI extreme, trend alignment, or favorable range position)"}
        
        # Guardrail 4: Skip trades when volatility is abnormally low
        # Check ATR% to determine if volatility is too low
        try:
            from validators import get_h4_trend_adx_atr_percent
            trend, adx, atr_percent = get_h4_trend_adx_atr_percent(symbol_clean, oanda_client=user_client)
            if atr_percent is not None and atr_percent < 0.15:  # Abnormally low volatility
                return {"allowed": False, "reason": f"Volatility too low (ATR% {atr_percent:.2f} < 0.15)"}
        except Exception as e:
            # If we can't check volatility, log but don't block (defensive)
            print(f"[ENHANCED] ⚠️ Could not check volatility guardrail: {e}")
        
        return {"allowed": True, "reason": f"Guardrails passed (RR: {rr_ratio:.2f}, Confirmations: {', '.join(strong_confirmations)})"}
    
    def _filter_opportunities_general(self, opportunities: List[MarketOpportunity]) -> List[MarketOpportunity]:
        """Filter opportunities by general criteria (score, confidence, correlation, session timing).
        This filtering is independent of user positions.
        """
        filtered = []
        
        for opp in opportunities:
            # Scalp Mode: Check if score is between 38 and min_opportunity_score
            is_scalp_candidate = 38.0 <= opp.score < self.min_opportunity_score
            
            if is_scalp_candidate:
                # Mark as scalp mode and check relaxed criteria
                opp.scalp_mode = True
                
                # Scalp mode requirements: session_strength >= 0.25, correlation_risk <= 0.85
                if opp.session_strength < 0.25:
                    print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: Scalp candidate rejected - session strength too low ({opp.session_strength:.2f} < 0.25)")
                    continue
                
                if opp.correlation_risk > 0.85:
                    print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: Scalp candidate rejected - correlation risk too high ({opp.correlation_risk:.2f} > 0.85)")
                    continue
                
                print(f"[ENHANCED] ✅ {opp.symbol} {opp.direction}: Scalp mode candidate passed (Score: {opp.score:.1f}, Session: {opp.session_strength:.2f}, Correlation: {opp.correlation_risk:.2f})")
                filtered.append(opp)
                continue
            
            # Regular mode: Score threshold
            if opp.score < self.min_opportunity_score:
                print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: REJECTED - Score {opp.score:.1f} below minimum threshold {self.min_opportunity_score:.1f}")
                continue
            
            # Check confidence level
            if opp.confidence == "low":
                required_score = self.min_opportunity_score + 5
                if opp.score < required_score:
                    print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: REJECTED - Low confidence requires score ≥{required_score:.1f}, got {opp.score:.1f}")
                    continue
                print(f"[ENHANCED] ⚠️ {opp.symbol} {opp.direction}: Low confidence but score sufficient ({opp.score:.1f} ≥ {required_score:.1f})")
            
            # Correlation no longer reduces scores - it's handled as exposure-based blocking in user filtering
            # Session timing check (soft gate on 4H)
            if opp.session_strength < 0.4:
                penalty = 3.0  # small soft penalty instead of hard skip
                effective_score = opp.score - penalty
                if effective_score < self.min_opportunity_score:
                    print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: REJECTED - Poor session timing ({opp.session_strength:.2f}) reduces score to {effective_score:.1f} (below {self.min_opportunity_score:.1f})")
                    continue  # only skip if still below floor after penalty cushion
                print(f"[ENHANCED] ⚠️ {opp.symbol} {opp.direction}: Poor session timing ({opp.session_strength:.2f}) but score sufficient after penalty")
            
            print(f"[ENHANCED] ✅ {opp.symbol} {opp.direction}: Passed general filters (Score: {opp.score:.1f})")
            filtered.append(opp)
        
        return filtered
    
    def _filter_opportunities_for_user(self, opportunities: List[MarketOpportunity],
                                      user_positions: List[Dict],
                                      user_active_pairs: List[str],
                                      user_client,
                                      user_account_id: str) -> List[MarketOpportunity]:
        """Filter opportunities for a specific user based on their open positions.
        Includes exposure-based correlation blocking (only when user has open positions)."""
        filtered = []
        
        # Skip correlation checks entirely when user has zero open positions
        has_open_positions = len(user_positions) > 0
        
        for opp in opportunities:
            symbol_clean = opp.symbol.replace("_", "")
            
            # Check if user already has a position on this pair
            if has_user_position_on_pair(user_client, user_account_id, opp.symbol, opp.direction):
                print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: REJECTED - User already has open {opp.direction} position on this pair")
                continue
            
            # Check if user has this pair active (any direction)
            if symbol_clean in user_active_pairs:
                print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: REJECTED - User already has active position on {symbol_clean} (any direction)")
                continue
            
            # Exposure-based correlation blocking (only when user has open positions)
            if has_open_positions:
                base_currency = opp.symbol[:3]
                quote_currency = opp.symbol[4:7] if len(opp.symbol) > 6 else opp.symbol[3:6]
                
                # Check if this opportunity shares base or quote currency with existing positions
                has_correlation_exposure = False
                for position in user_positions:
                    pos_instrument = position.get("instrument", "").replace("_", "")
                    if len(pos_instrument) >= 6:
                        pos_base = pos_instrument[:3]
                        pos_quote = pos_instrument[3:6]
                        
                        # Check for currency overlap
                        if (base_currency == pos_base or quote_currency == pos_quote or
                            base_currency == pos_quote or quote_currency == pos_base):
                            has_correlation_exposure = True
                            break
                
                # Block only if correlation risk >= 0.7 and there's exposure
                if has_correlation_exposure and opp.correlation_risk >= 0.7:
                    print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: REJECTED - Correlation exposure (risk {opp.correlation_risk:.2f} >= 0.7) with existing positions")
                    continue
            
            filtered.append(opp)
        
        return filtered
    
    def _filter_opportunities(self, opportunities: List[MarketOpportunity], 
                            active_trades: List[Dict]) -> List[MarketOpportunity]:
        """Apply additional filtering to opportunities"""
        filtered = []
        
        for opp in opportunities:
            # Score threshold
            if opp.score < self.min_opportunity_score:
                print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: Score too low ({opp.score:.1f})")
                continue
            
            # Check if we already have a position on this pair
            if self._has_existing_position(opp.symbol, opp.direction, active_trades):
                print(f"[ENHANCED] ❌ {opp.symbol} {opp.direction}: Already have position")
                continue
            
            # Check confidence level
            if opp.confidence == "low":
                print(f"[ENHANCED] ⚠️ {opp.symbol} {opp.direction}: Low confidence, requiring higher score")
                if opp.score < self.min_opportunity_score + 5:
                    continue
            
            # Correlation risk check (enhanced)
            if opp.correlation_risk > 0.7:
                print(f"[ENHANCED] ⚠️ {opp.symbol} {opp.direction}: High correlation risk ({opp.correlation_risk:.2f})")
                if opp.score < self.min_opportunity_score + 15:  # Need higher score for high correlation
                    continue
            
            # Session timing check
          # Session timing check (soft gate on 4H)
            if opp.session_strength < 0.4:
                print(f"[ENHANCED] ⚠️ {opp.symbol} {opp.direction}: Poor session timing ({opp.session_strength:.2f})")
                penalty = 3.0  # small soft penalty instead of hard skip
                if opp.score + penalty < self.min_opportunity_score:
                    continue  # only skip if still below floor after penalty cushion

            
            print(f"[ENHANCED] ✅ {opp.symbol} {opp.direction}: Passed all filters (Score: {opp.score:.1f})")
            filtered.append(opp)
        
        return filtered
    
    def _has_existing_position(self, symbol: str, direction: str, 
                             active_trades: List[Dict]) -> bool:
        """Check if we already have a position on this pair/direction"""
        # Convert symbol format if needed
        clean_symbol = symbol.replace("_", "")
        
        for trade in active_trades:
            trade_symbol = trade.get("symbol", "").replace("_", "")
            trade_direction = trade.get("direction", "")
            
            if trade_symbol == clean_symbol and trade_direction == direction:
                return True
                
        return False
    
    def _execute_opportunity(self, opportunity: MarketOpportunity) -> Optional[Dict]:
        """Execute a trading opportunity"""
        try:
            symbol = opportunity.symbol.replace("_", "")  # Convert to expected format
            direction = opportunity.direction
            
            print(f"[ENHANCED] 🎯 Executing {direction.upper()} {symbol}")
            print(f"[ENHANCED] 📊 Opportunity Score: {opportunity.score:.1f} ({opportunity.confidence} confidence)")
            print(f"[ENHANCED] 💰 Entry: {opportunity.entry_price:.5f}")
            print(f"[ENHANCED] 🎯 Reasons: {', '.join(opportunity.reasons)}")
            
            # Create trade idea text for compatibility with existing system
            trade_idea = self._create_trade_idea_text(opportunity)

            # Idea gate (cooldown/time & price + structure confirmation + stale repost)
            gate = evaluate_trade_gate(symbol, direction, trade_idea, api_key=user.oanda_api_key, account_id=user.oanda_account_id)
            if not gate.get("allow", False):
                print(f"[ENHANCED] 🚫 Idea gated. Reasons: {gate.get('blocks')}")
                # Send admin notification for rejection
                try:
                    send_admin_trade_notification(
                        event_type="REJECTED",
                        pair=symbol,
                        direction=direction.upper(),
                        rationale=f"Gate blocked: {gate.get('blocks')}",
                        score=opportunity.score,
                        gate_blocks=gate.get("blocks", []),
                        additional_context={
                            "opportunity": {
                                "symbol": opportunity.symbol,
                                "direction": opportunity.direction,
                                "score": opportunity.score,
                                "confidence": opportunity.confidence,
                                "reasons": opportunity.reasons,
                            },
                        },
                    )
                except Exception as e:
                    print(f"[ENHANCED] ⚠️ Failed to send rejection notification: {e}")
                # Legacy broadcast (now only sends to admin via send_signal)
                self._maybe_broadcast_reject(
                    opportunity,
                    rationale=f"Gate blocked: {gate.get('blocks')}"
                )
                return None
            
            if not self.dry_run:
                # Build smart plan with live spread for consistent exits/sizing
                # NOTE: This function appears to be legacy - _execute_opportunity_for_user is used instead
                # If this path is used, user_client is not available, so plan_trade will use env vars (legacy mode)
                spread_pips = self._get_live_spread_pips(opportunity.symbol, api_key=user.oanda_api_key, account_id=user.oanda_account_id)
                plan = plan_trade(symbol, direction, spread_pips=spread_pips or 0.8, oanda_client=None)
                if not plan:
                    print("[ENHANCED] ❌ Smart plan could not be built. Skipping.")
                    return None
                exits = plan["exits"]
                risk_pct = plan["risk_pct"]
                # Place the actual trade
                trade_details = place_trade(
                    trade_idea,
                    direction,
                    risk_pct=risk_pct,
                    sl_price=exits["sl"],
                    tp_price=exits["tp1"],
                    meta={
                        "quality_score": plan.get("quality_score"),
                        "smart_exits": True,
                        "trail_start_r": exits.get("trail_start_r"),
                        "trail_step_pips": exits.get("trail_step_pips"),
                        "plan_tp2": exits.get("tp2"),
                        "reasons": opportunity.reasons,
                    }
                )
                trade_id_ok = str(trade_details.get("trade_id", "")).isdigit()
                
                if not trade_id_ok:
                    print("[ENHANCED] ⚠️ No valid trade ID; skipping monitor/cache add.")
                    self._send_trade_notification(opportunity, trade_details, "executed")  # still notify
                    return { ... }  # keep existing return but skip add_trade/record

                # Add to trade cache
                add_trade(
                    symbol, 
                    direction, 
                    trade_details["entry_price"], 
                    trade_details.get("trade_id", "manual")
                )
                # Record executed idea in registry
                record_executed_idea(symbol, direction, trade_idea, trade_details["entry_price"])
                
                # Send notification email
                self._send_trade_notification(opportunity, trade_details, "executed")
                
                # Start monitoring in background (for automated systems)
                # Note: For manual systems, monitoring should be done separately
                
                print(f"[ENHANCED] ✅ Trade executed: {symbol} {direction.upper()}")
                
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "opportunity_score": opportunity.score,
                    "trade_details": trade_details,
                    "execution_time": datetime.now().isoformat()
                }
            else:
                print(f"[ENHANCED] 🧪 DRY RUN: Would execute {symbol} {direction.upper()}")
                
                # Send dry run notification
                self._send_trade_notification(opportunity, None, "dry_run")
                
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "opportunity_score": opportunity.score,
                    "trade_details": "dry_run",
                    "execution_time": datetime.now().isoformat()
                }
                
        except Exception as e:
            print(f"[ENHANCED] ❌ Error executing opportunity {opportunity.symbol}: {e}")
            
            # Send error notification
            self._send_error_notification(opportunity, str(e))
            
            return None
    
    def _execute_opportunity_for_user(self, opportunity: MarketOpportunity, user: Tier2User, user_client) -> Optional[Dict]:
        """Execute a trading opportunity for a specific user."""
        try:
            symbol = opportunity.symbol.replace("_", "")  # Convert to expected format
            direction = opportunity.direction
            
            print(f"[ENHANCED] 🎯 User {user.user_id}: Executing {direction.upper()} {symbol}")
            print(f"[ENHANCED] 📊 Opportunity Score: {opportunity.score:.1f} ({opportunity.confidence} confidence)")
            print(f"[ENHANCED] 💰 Entry: {opportunity.entry_price:.5f}")
            print(f"[ENHANCED] 🎯 Reasons: {', '.join(opportunity.reasons)}")
            
            # Create trade idea text for compatibility with existing system
            trade_idea = self._create_trade_idea_text(opportunity)

            # Idea gate (cooldown/time & price + structure confirmation + stale repost)
            gate = evaluate_trade_gate(symbol, direction, trade_idea, api_key=user.oanda_api_key, account_id=user.oanda_account_id)
            if not gate.get("allow", False):
                print(f"[ENHANCED] 🚫 User {user.user_id}: Idea gated. Reasons: {gate.get('blocks')}")
                self._send_admin_rejection_notification(opportunity, f"Gate blocked: {gate.get('blocks')}", user)
                return None
            
            # DIAGNOSTIC LOGGING: Check dry-run mode
            print(f"[ENHANCED][DIAGNOSTIC] Dry-run mode check: self.dry_run = {self.dry_run}")
            print(f"[ENHANCED][DIAGNOSTIC] DRY_RUN env var: {os.getenv('DRY_RUN', 'not set')}")
            
            if not self.dry_run:
                print(f"[ENHANCED][DIAGNOSTIC] ✅ Dry-run mode is OFF - proceeding with real trade execution")
                
                # DIAGNOSTIC LOGGING: Validate client and account_id before proceeding
                print(f"[ENHANCED][DIAGNOSTIC] Validating OANDA client and account_id...")
                print(f"[ENHANCED][DIAGNOSTIC] user_client is None: {user_client is None}")
                print(f"[ENHANCED][DIAGNOSTIC] user.oanda_account_id: {user.oanda_account_id}")
                print(f"[ENHANCED][DIAGNOSTIC] user.oanda_api_key present: {bool(user.oanda_api_key)}")
                
                if user_client is None:
                    print(f"[ENHANCED][ERROR] ❌ user_client is None - cannot proceed with trade execution")
                    raise ValueError(f"OANDA client is None for user {user.user_id}")
                
                if not user.oanda_account_id:
                    print(f"[ENHANCED][ERROR] ❌ user.oanda_account_id is empty - cannot proceed with trade execution")
                    raise ValueError(f"OANDA account_id is empty for user {user.user_id}")
                
                # Build smart plan with live spread for consistent exits/sizing
                # Pass user_client to plan_trade to use per-user credentials instead of env vars
                spread_pips = self._get_live_spread_pips(opportunity.symbol, api_key=user.oanda_api_key, account_id=user.oanda_account_id)
                plan = plan_trade(symbol, direction, spread_pips=spread_pips or 0.8, oanda_client=user_client)
                if not plan:
                    print(f"[ENHANCED] ❌ User {user.user_id}: Smart plan could not be built. Skipping.")
                    return None
                exits = plan["exits"]
                risk_pct = plan["risk_pct"]
                
                # Scalp Mode: Overwrite exits with tighter TP/SL if this is a scalp trade
                if opportunity.scalp_mode:
                    print(f"[ENHANCED] ⚡ User {user.user_id}: Scalp mode trade - applying tighter exits")
                    # Get actual entry price from live market (will be set when trade is placed)
                    # For now, use opportunity entry price as estimate
                    entry_price = opportunity.entry_price
                    pip_factor = self._get_pip_factor(symbol)
                    
                    # TP1: 5-12 pips (use 10 pips for better R:R)
                    tp_pips = 10.0
                    # SL: 6-10 pips (use 8 pips)
                    sl_pips = 8.0
                    
                    if direction.lower() == "buy":
                        exits["tp1"] = entry_price + (tp_pips * pip_factor)
                        exits["sl"] = entry_price - (sl_pips * pip_factor)
                    else:  # sell
                        exits["tp1"] = entry_price - (tp_pips * pip_factor)
                        exits["sl"] = entry_price + (sl_pips * pip_factor)
                    
                    print(f"[ENHANCED] ⚡ Scalp exits: TP1={exits['tp1']:.5f} ({tp_pips} pips), SL={exits['sl']:.5f} ({sl_pips} pips)")
                
                # Fetch user's trade_allocation setting
                trade_allocation = None
                if self.api_client:
                    try:
                        settings = self.api_client.get_user_settings(user.user_id)
                        # Handle both camelCase and snake_case field names for robustness
                        trade_allocation = settings.get("tradeAllocation")
                        if trade_allocation is None:
                            trade_allocation = settings.get("trade_allocation")
                        if trade_allocation is not None:
                            trade_allocation = float(trade_allocation)
                            print(f"[ENHANCED] 📊 User {user.user_id}: Using trade_allocation={trade_allocation}%")
                        else:
                            print(f"[ENHANCED] ⚠️ User {user.user_id}: trade_allocation is None in API response, falling back to default sizing")
                    except Exception as e:
                        print(f"[ENHANCED] ⚠️ Could not fetch user settings for user {user.user_id}: {e}")
                        print(f"[ENHANCED] ⚠️ Falling back to default trade sizing logic")
                
                # Build meta dict with scalp_mode flag
                meta_dict = {
                    "quality_score": plan.get("quality_score"),
                    "smart_exits": True,
                    "trail_start_r": exits.get("trail_start_r"),
                    "trail_step_pips": exits.get("trail_step_pips"),
                    "plan_tp2": exits.get("tp2"),
                    "reasons": opportunity.reasons,
                }
                if opportunity.scalp_mode:
                    meta_dict["scalp_mode"] = True
                
                # DIAGNOSTIC LOGGING: Before calling place_trade
                print(f"[ENHANCED][DIAGNOSTIC] About to call place_trade() with:")
                print(f"[ENHANCED][DIAGNOSTIC]   - client: {type(user_client).__name__} (not None: {user_client is not None})")
                print(f"[ENHANCED][DIAGNOSTIC]   - account_id: {user.oanda_account_id}")
                print(f"[ENHANCED][DIAGNOSTIC]   - user_id: {user.user_id}")
                print(f"[ENHANCED][DIAGNOSTIC]   - direction: {direction}")
                print(f"[ENHANCED][DIAGNOSTIC]   - sl_price: {exits['sl']}")
                print(f"[ENHANCED][DIAGNOSTIC]   - tp_price: {exits['tp1']}")
                print(f"[ENHANCED][DIAGNOSTIC]   - trade_allocation: {trade_allocation} (None means will use fallback sizing)")
                
                # Place the actual trade using user's client and account
                trade_details = place_trade(
                    trade_idea,
                    direction,
                    risk_pct=risk_pct,
                    sl_price=exits["sl"],
                    tp_price=exits["tp1"],
                    meta=meta_dict,
                    client=user_client,
                    account_id=user.oanda_account_id,
                    user_id=user.user_id,
                    trade_allocation=trade_allocation
                )
                
                print(f"[ENHANCED][DIAGNOSTIC] place_trade() returned: trade_id={trade_details.get('trade_id', 'N/A')}")
                
                # SAFETY ASSERTION: Validate trade ID is present and valid
                trade_id = trade_details.get("trade_id")
                if not trade_id or trade_id == "unknown" or not str(trade_id).isdigit():
                    error_msg = f"Invalid trade ID after execution: {trade_id}"
                    print(f"[ENHANCED] ❌ User {user.user_id}: {error_msg}")
                    raise ValueError(error_msg)
                
                print(f"[ENHANCED] ✅ Trade ID validated: {trade_id}")
                trade_id_ok = True  # Validated above
                
                if not trade_id_ok:
                    print(f"[ENHANCED] ⚠️ User {user.user_id}: No valid trade ID; skipping monitor/cache add.")
                    self._send_trade_notification_for_user(opportunity, trade_details, "executed", user)
                    return {
                        "symbol": symbol,
                        "direction": direction,
                        "opportunity_score": opportunity.score,
                        "trade_details": trade_details,
                        "execution_time": datetime.now().isoformat(),
                        "user_id": user.user_id,
                    }

                # Add to trade cache (with user_id for tracking)
                add_trade(
                    symbol, 
                    direction, 
                    trade_details["entry_price"], 
                    trade_details.get("trade_id", "manual"),
                    user_id=user.user_id,
                    account_id=user.oanda_account_id
                )
                # Record executed idea in registry
                record_executed_idea(symbol, direction, trade_idea, trade_details["entry_price"])
                
                # Send notification emails (simplified to user, full to admin)
                self._send_trade_notification_for_user(opportunity, trade_details, "executed", user)
                
                print(f"[ENHANCED] ✅ User {user.user_id}: Trade executed: {symbol} {direction.upper()}")
                
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "opportunity_score": opportunity.score,
                    "trade_details": trade_details,
                    "execution_time": datetime.now().isoformat(),
                    "user_id": user.user_id,
                }
            else:
                # NOTE: This else block should never execute due to startup abort check in __init__
                # It's kept for defensive programming but will be unreachable in normal operation
                print(f"[ENHANCED] 🧪 DRY RUN: User {user.user_id}: Would execute {symbol} {direction.upper()}")
                self._send_trade_notification_for_user(opportunity, None, "dry_run", user)
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "opportunity_score": opportunity.score,
                    "trade_details": "dry_run",
                    "execution_time": datetime.now().isoformat(),
                    "user_id": user.user_id,
                }
                
        except Exception as e:
            print(f"[ENHANCED] ❌ User {user.user_id}: Error executing opportunity {opportunity.symbol}: {e}")
            self._send_error_notification_for_user(opportunity, str(e), user)
            return None
    
    def _send_admin_rejection_notification(self, opportunity: MarketOpportunity, rationale: str, user: Tier2User) -> None:
        """Send admin notification for trade rejection."""
        try:
            send_admin_trade_notification(
                event_type="REJECTED",
                pair=opportunity.symbol.replace("_", ""),
                direction=opportunity.direction.upper(),
                rationale=f"User {user.user_id} ({user.email}): {rationale}",
                score=opportunity.score,
                gate_blocks=[],
                additional_context={
                    "user_id": user.user_id,
                    "user_email": user.email,
                    "opportunity": {
                        "symbol": opportunity.symbol,
                        "direction": opportunity.direction,
                        "score": opportunity.score,
                    },
                },
            )
        except Exception as e:
            print(f"[ENHANCED] ⚠️ Failed to send rejection notification: {e}")
    
    def _send_admin_validation_error(self, opportunity: MarketOpportunity, rationale: str, user: Tier2User) -> None:
        """Send admin notification for validation error."""
        try:
            send_admin_trade_notification(
                event_type="VALIDATION_ERROR",
                pair=opportunity.symbol.replace("_", ""),
                direction=opportunity.direction.upper(),
                rationale=f"User {user.user_id} ({user.email}): {rationale}",
                score=opportunity.score,
                validation_errors=[rationale],
                additional_context={
                    "user_id": user.user_id,
                    "user_email": user.email,
                    "opportunity": {
                        "symbol": opportunity.symbol,
                        "direction": opportunity.direction,
                        "score": opportunity.score,
                    },
                },
            )
        except Exception as e:
            print(f"[ENHANCED] ⚠️ Failed to send validation error notification: {e}")
    
    def _send_trade_notification_for_user(self, opportunity: MarketOpportunity, 
                                         trade_details: Optional[Dict], 
                                         notification_type: str,
                                         user: Tier2User) -> None:
        """Send trade notification: simplified signal to user, full details to admin."""
        try:
            symbol = opportunity.symbol.replace("_", "")
            direction = opportunity.direction.upper()
            
            if notification_type == "executed" and trade_details:
                # Send simplified signal to user (via send_signal which handles user emails)
                try:
                    send_signal({
                        "signal_id": f"{trade_details.get('trade_id', 'manual')}:OPEN:USER{user.user_id}",
                        "type": "OPEN",
                        "pair": symbol,
                        "direction": direction,
                        "entry": trade_details.get("entry_price"),
                        "sl": trade_details.get("sl_price"),
                        "tp": trade_details.get("tp_price"),
                        "rationale": f"Auto scan score {opportunity.score:.1f}. " + (opportunity.reasons[0] if opportunity.reasons else ""),
                        "user_id": user.user_id,  # For per-user email routing
                    })
                except Exception as e:
                    print(f"[ENHANCED] ⚠️ Failed to send user signal: {e}")
                
                # Send full admin notification
                try:
                    send_admin_trade_notification(
                        event_type="ACCEPTED",
                        pair=symbol,
                        direction=direction,
                        entry=trade_details.get("entry_price"),
                        sl=trade_details.get("sl_price"),
                        tp=trade_details.get("tp_price"),
                        rationale=f"User {user.user_id} ({user.email}): Auto scan score {opportunity.score:.1f}. " + (opportunity.reasons[0] if opportunity.reasons else ""),
                        score=opportunity.score,
                        additional_context={
                            "user_id": user.user_id,
                            "user_email": user.email,
                            "trade_details": trade_details,
                            "opportunity": {
                                "symbol": opportunity.symbol,
                                "direction": opportunity.direction,
                                "score": opportunity.score,
                                "confidence": opportunity.confidence,
                                "reasons": opportunity.reasons,
                            },
                        },
                    )
                except Exception as e:
                    print(f"[ENHANCED] ⚠️ Failed to send admin notification: {e}")
            elif notification_type == "dry_run":
                # Send admin notification for dry run
                try:
                    send_admin_trade_notification(
                        event_type="ACCEPTED",
                        pair=symbol,
                        direction=direction,
                        entry=opportunity.entry_price,
                        sl=opportunity.suggested_sl,
                        tp=opportunity.suggested_tp,
                        rationale=f"DRY RUN - User {user.user_id} ({user.email}): Auto scan score {opportunity.score:.1f}. " + (opportunity.reasons[0] if opportunity.reasons else ""),
                        score=opportunity.score,
                        additional_context={
                            "dry_run": True,
                            "user_id": user.user_id,
                            "user_email": user.email,
                            "opportunity": {
                                "symbol": opportunity.symbol,
                                "direction": opportunity.direction,
                                "score": opportunity.score,
                                "confidence": opportunity.confidence,
                                "reasons": opportunity.reasons,
                            },
                        },
                    )
                except Exception as e:
                    print(f"[ENHANCED] ⚠️ Failed to send dry run notification: {e}")
            
        except Exception as e:
            print(f"[ENHANCED] ⚠️ Failed to send notification: {e}")
    
    def _send_error_notification_for_user(self, opportunity: MarketOpportunity, error_msg: str, user: Tier2User) -> None:
        """Send error notification for user trade execution failure."""
        try:
            send_admin_trade_notification(
                event_type="EXECUTION_ERROR",
                pair=opportunity.symbol.replace("_", ""),
                direction=opportunity.direction.upper(),
                rationale=f"User {user.user_id} ({user.email}): Failed to execute trade opportunity",
                score=opportunity.score,
                error_message=error_msg,
                additional_context={
                    "user_id": user.user_id,
                    "user_email": user.email,
                    "opportunity": {
                        "symbol": opportunity.symbol,
                        "direction": opportunity.direction,
                        "score": opportunity.score,
                        "confidence": opportunity.confidence,
                        "rsi": opportunity.rsi,
                        "trend": opportunity.trend,
                        "range_position": opportunity.range_position,
                        "session_strength": opportunity.session_strength,
                        "reasons": opportunity.reasons,
                    },
                },
            )
        except Exception as e:
            print(f"[ENHANCED] ⚠️ Failed to send error notification: {e}")
    
    def _maybe_broadcast_reject(self, opportunity: MarketOpportunity, rationale: str) -> None:
        """Optionally broadcast a rejection reason to admins + active users.
        Controlled by BROADCAST_REJECTIONS env var (default: true)."""
        if os.getenv("BROADCAST_REJECTIONS", "true").lower() != "true":
            return
        try:
            send_signal({
                "signal_id": f"{opportunity.symbol.replace('_','')}:{opportunity.direction.upper()}:REJECT:{int(time.time())}",
                "type": "REJECT",
                "pair": opportunity.symbol.replace("_", ""),
                "direction": opportunity.direction.upper(),
                "entry": getattr(opportunity, "entry_price", None),
                "rationale": rationale,
            })
        except Exception:
            pass
    
    def _create_trade_idea_text(self, opportunity: MarketOpportunity) -> str:
        """Create trade idea text for compatibility with existing system"""
        direction_text = "buy" if opportunity.direction == "buy" else "sell"
        
        idea_text = (
            f"{direction_text.upper()} {opportunity.symbol} - "
            f"4H Analysis Score: {opportunity.score:.1f}. "
            f"RSI: {opportunity.rsi:.1f}, Trend: {opportunity.trend}, "
            f"Range Position: {opportunity.range_position:.2f}. "
            f"Reasons: {', '.join(opportunity.reasons[:3])}. "
            f"Session strength: {opportunity.session_strength:.2f}, "
            f"Volatility: {opportunity.volatility:.2f}%"
        )
        
        return idea_text
    
    def _send_trade_notification(self, opportunity: MarketOpportunity, 
                               trade_details: Optional[Dict], notification_type: str):
        """Send email notification for trade execution"""
        try:
            symbol = opportunity.symbol.replace("_", "")
            direction = opportunity.direction.upper()
            
            if notification_type == "executed" and trade_details:
                # Broadcast signal (sends admin diagnostic + user clean signal for OPEN)
                # Note: send_signal handles both admin notification and user signal for OPEN trades
                try:
                    send_signal({
                        "signal_id": f"{trade_details.get('trade_id', 'manual')}:OPEN",
                        "type": "OPEN",
                        "pair": symbol,
                        "direction": direction,
                        "entry": trade_details.get("entry_price"),
                        "sl": trade_details.get("sl_price"),
                        "tp": trade_details.get("tp_price"),
                        "rationale": f"Auto scan score {opportunity.score:.1f}. " + (opportunity.reasons[0] if opportunity.reasons else ""),
                        "score": opportunity.score,
                        "quality_score": trade_details.get("meta", {}).get("quality_score"),
                        "trade_details": trade_details,
                        "additional_context": {
                            "opportunity": {
                                "symbol": opportunity.symbol,
                                "direction": opportunity.direction,
                                "score": opportunity.score,
                                "confidence": opportunity.confidence,
                                "reasons": opportunity.reasons,
                            },
                        },
                    })
                except Exception as e:
                    print(f"[ENHANCED] ⚠️ Failed to send signal: {e}")
            elif notification_type == "dry_run":
                # Send admin notification for dry run
                try:
                    send_admin_trade_notification(
                        event_type="ACCEPTED",
                        pair=symbol,
                        direction=direction,
                        entry=opportunity.entry_price,
                        sl=opportunity.suggested_sl,
                        tp=opportunity.suggested_tp,
                        rationale=f"DRY RUN - Auto scan score {opportunity.score:.1f}. " + (opportunity.reasons[0] if opportunity.reasons else ""),
                        score=opportunity.score,
                        additional_context={
                            "dry_run": True,
                            "opportunity": {
                                "symbol": opportunity.symbol,
                                "direction": opportunity.direction,
                                "score": opportunity.score,
                                "confidence": opportunity.confidence,
                                "reasons": opportunity.reasons,
                            },
                        },
                    )
                except Exception as e:
                    print(f"[ENHANCED] ⚠️ Failed to send dry run notification: {e}")
            
        except Exception as e:
            print(f"[ENHANCED] ⚠️ Failed to send notification: {e}")

    def _get_pip_factor(self, symbol: str) -> float:
        """Get pip factor for a symbol (price units per pip)."""
        s = symbol.upper().replace("_", "").replace("/", "")
        if s.endswith("JPY"):  # USDJPY etc.
            return 0.01
        if s == "XAUUSD":
            return 0.1
        if s == "XAGUSD":
            return 0.01
        return 0.0001
    
    def _get_live_spread_pips(self, pair: str, api_key=None, account_id=None) -> float:
        """Get live spread in pips. Requires api_key and account_id to be provided explicitly or set in env (legacy mode)."""
        try:
            api_key = api_key or os.getenv("OANDA_API_KEY")
            account_id = account_id or os.getenv("OANDA_ACCOUNT_ID")
            if not api_key or not account_id:
                # Return default if credentials not available
                return 0.8
            client = OandaAPI(access_token=api_key, environment="live")
            r = pricing.PricingInfo(accountID=account_id, params={"instruments": pair})
            client.request(r)
            prices = r.response["prices"][0]
            bid = float(prices["bids"][0]["price"])
            ask = float(prices["asks"][0]["price"])
            spread = max(0.0, ask - bid)
            pip = self._get_pip_factor(pair)
            return spread / pip if pip else 0.8
        except Exception:
            return 0.8
    
    def _send_error_notification(self, opportunity: MarketOpportunity, error_msg: str):
        """Send error notification"""
        try:
            send_admin_trade_notification(
                event_type="EXECUTION_ERROR",
                pair=opportunity.symbol.replace("_", ""),
                direction=opportunity.direction.upper(),
                rationale="Failed to execute trade opportunity",
                score=opportunity.score,
                error_message=error_msg,
                additional_context={
                    "opportunity": {
                        "symbol": opportunity.symbol,
                        "direction": opportunity.direction,
                        "score": opportunity.score,
                        "confidence": opportunity.confidence,
                        "rsi": opportunity.rsi,
                        "trend": opportunity.trend,
                        "range_position": opportunity.range_position,
                        "session_strength": opportunity.session_strength,
                        "reasons": opportunity.reasons,
                    },
                },
            )
        except Exception as e:
            print(f"[ENHANCED] ⚠️ Failed to send error notification: {e}")
    
    def _format_execution_email(self, opportunity: MarketOpportunity, 
                              trade_details: Dict) -> str:
        """Format execution email"""
        rr_ratio = trade_details.get("risk_reward_ratio", 0)
        plain = self._build_plain_summary(opportunity, trade_details)
        
        body = (
            f"Trade executed successfully!\n\n"
            f"In simple terms: {plain}\n\n"
            f"📊 OPPORTUNITY ANALYSIS:\n"
            f"Symbol: {opportunity.symbol}\n"
            f"Direction: {opportunity.direction.upper()}\n"
            f"Score: {opportunity.score:.1f}/100 ({opportunity.confidence} confidence)\n"
            f"Correlation Risk: {opportunity.correlation_risk:.2f}\n\n"
            f"💰 TRADE DETAILS:\n"
            f"Entry Price: {trade_details.get('entry_price', 'N/A'):.5f}\n"
            f"Stop Loss: {trade_details.get('sl_price', 'N/A'):.5f}\n"
            f"Take Profit: {trade_details.get('tp_price', 'N/A'):.5f}\n"
            f"Position Size: {trade_details.get('position_size', 'N/A')}\n"
            f"Risk:Reward: 1:{rr_ratio:.2f}\n\n"
            f"📈 TECHNICAL ANALYSIS:\n"
            f"RSI: {opportunity.rsi:.1f}\n"
            f"Trend: {opportunity.trend}\n"
            f"Range Position: {opportunity.range_position:.2f}\n"
            f"Volatility: {opportunity.volatility:.2f}%\n"
            f"Session Strength: {opportunity.session_strength:.2f}\n\n"
            f"🎯 REASONS:\n"
            + "\n".join(f"• {reason}" for reason in opportunity.reasons)
        )
        
        return body
    
    def _format_dry_run_email(self, opportunity: MarketOpportunity) -> str:
        """Format dry run email"""
        plain = self._build_plain_summary(opportunity, None, is_dry_run=True)
        body = (
            f"Dry run trade simulation:\n\n"
            f"In simple terms: {plain}\n\n"
            f"📊 OPPORTUNITY ANALYSIS:\n"
            f"Symbol: {opportunity.symbol}\n"
            f"Direction: {opportunity.direction.upper()}\n"
            f"Score: {opportunity.score:.1f}/100 ({opportunity.confidence} confidence)\n\n"
            f"💰 SUGGESTED LEVELS:\n"
            f"Entry Price: {opportunity.entry_price:.5f}\n"
            f"Stop Loss: {opportunity.suggested_sl:.5f}\n"
            f"Take Profit: {opportunity.suggested_tp:.5f}\n\n"
            f"📈 TECHNICAL ANALYSIS:\n"
            f"RSI: {opportunity.rsi:.1f}\n"
            f"Trend: {opportunity.trend}\n"
            f"Range Position: {opportunity.range_position:.2f}\n"
            f"Session Strength: {opportunity.session_strength:.2f}\n\n"
            f"🎯 REASONS:\n"
            + "\n".join(f"• {reason}" for reason in opportunity.reasons)
        )
        
        return body
    
    def _build_plain_summary(self, opportunity: MarketOpportunity, 
                              trade_details: Optional[Dict], is_dry_run: bool = False) -> str:
        """Produce a concise, non-technical summary to build trust.
        Explains what we're doing, why, and how risk is controlled.
        """
        direction_upper = str(opportunity.direction or "").upper()
        verb = "buying" if direction_upper == "BUY" else ("selling" if direction_upper == "SELL" else "trading")
        trend = (opportunity.trend or "").lower()
        aligns = (
            (direction_upper == "BUY" and trend == "bullish") or 
            (direction_upper == "SELL" and trend == "bearish")
        )
        if aligns:
            alignment_text = "aligns with the current trend"
        elif trend in ("bullish", "bearish"):
            alignment_text = "goes against the higher‑timeframe trend"
        else:
            alignment_text = "meets our quality rules"

        score_text = f"{opportunity.score:.1f}/100"

        def _fmt_price(val: Optional[float]) -> str:
            try:
                return f"{float(val):.5f}"
            except Exception:
                return "N/A"

        # Prices and RR
        if is_dry_run:
            entry = opportunity.entry_price
            sl = opportunity.suggested_sl
            tp = opportunity.suggested_tp
            try:
                risk = abs(float(entry) - float(sl))
                reward = abs(float(tp) - float(entry))
                rr = (reward / risk) if risk > 0 else None
            except Exception:
                rr = None
        else:
            entry = trade_details.get("entry_price") if trade_details else None
            sl = trade_details.get("sl_price") if trade_details else None
            tp = trade_details.get("tp_price") if trade_details else None
            rr = trade_details.get("risk_reward_ratio") if trade_details else None

        parts: List[str] = []
        parts.append(f"We're {verb} {opportunity.symbol} because the setup {alignment_text} and passes our safety checks.")
        parts.append(f"Confidence score: {score_text} (higher means stronger).")

        # Risk control explanation
        sl_txt = _fmt_price(sl)
        tp_txt = _fmt_price(tp)
        rr_txt = None
        try:
            if rr is not None:
                rr_txt = f"1:{float(rr):.2f}"
        except Exception:
            rr_txt = None

        if sl_txt != "N/A" and tp_txt != "N/A" and rr_txt:
            parts.append(f"If we're wrong, the stop loss limits risk near {sl_txt}. If we're right, we aim for {tp_txt} (~{rr_txt}).")
        elif sl_txt != "N/A" and tp_txt != "N/A":
            parts.append(f"Risk is limited near {sl_txt}; target is around {tp_txt}.")
        elif sl_txt != "N/A":
            parts.append(f"Risk is limited by a stop loss near {sl_txt}.")

        return " ".join(parts)
    
    def _get_session_summary(self, session_result: str, 
                           executed_trades: List[Dict] = None) -> Dict:
        """Get session summary"""
        end_time = datetime.now()
        duration = end_time - self.session_stats["start_time"]
        
        summary = {
            "session_result": session_result,
            "start_time": self.session_stats["start_time"].isoformat(),
            "end_time": end_time.isoformat(),
            "duration_minutes": duration.total_seconds() / 60,
            "opportunities_found": self.session_stats["opportunities_found"],
            "trades_executed": self.session_stats["trades_executed"],
            "trades_skipped": self.session_stats["trades_skipped"],
            "executed_trades": executed_trades or []
        }
        
        print(f"\n[ENHANCED] 📊 SESSION SUMMARY:")
        print(f"[ENHANCED] Result: {session_result}")
        print(f"[ENHANCED] Duration: {duration.total_seconds()/60:.1f} minutes")
        print(f"[ENHANCED] Opportunities Found: {self.session_stats['opportunities_found']}")
        print(f"[ENHANCED] Trades Executed: {self.session_stats['trades_executed']}")
        print(f"[ENHANCED] Trades Skipped: {self.session_stats['trades_skipped']}")
        
        return summary

def main():
    """Enhanced main function using market scanner"""
    try:
        # Add startup logging
        import logging
        logger = logging.getLogger(__name__)
        mode = "LIVE TRADING"
        logger.warning(f"[STARTUP MODE] Bot running in: {mode}")
        print(f"[STARTUP MODE] Bot running in: {mode}")
        
        session = EnhancedTradingSession()
        result = session.execute_trading_session()
        
        # Log session result
        add_log_entry({
            "type": "session_summary",
            "result": result,
            "timestamp": datetime.now().isoformat()
        })
        
        return result
        
    except Exception as e:
        print(f"[ENHANCED] ❌ Session failed: {e}")
        
        # Send error notification
        try:
            send_email(
                "❌ Trading Session Error",
                f"Enhanced trading session failed:\n\nError: {str(e)}\nTime: {datetime.now()}"
            )
        except:
            pass
        
        return {"session_result": "error", "error": str(e)}

if __name__ == "__main__":
    main()