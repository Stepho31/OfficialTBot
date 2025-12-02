#!/usr/bin/env python3
"""
Fully Automated 4H Forex Trading System
Continuous operation with multi-trade capability and intelligent monitoring
"""

import schedule
import os
import time
import threading
import json
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional
import schedule

from main import main as execute_trading_logic
from monitor import monitor_trade, monitor_open_trades
from email_utils import send_email
from trade_cache import get_active_trades, is_trade_active, add_trade, remove_trade
from trading_log import add_log_entry, get_weekly_performance, generate_and_save_weekly_snapshot
from validators import validate_entry_conditions
import oandapyV20
from oandapyV20.endpoints.trades import TradesList
from oandapyV20.endpoints.accounts import AccountDetails

@dataclass
class TradingState:
    """Current state of the automated trading system"""
    is_running: bool = False
    last_scan_time: datetime = None
    active_pairs: List[str] = None
    weekly_stats: Dict = None
    total_trades_today: int = 0
    max_trades_per_day: int = 10
    max_concurrent_trades: int = 3
    
    def __post_init__(self):
        if self.active_pairs is None:
            self.active_pairs = []
        if self.weekly_stats is None:
            self.weekly_stats = {}

class AutomatedTrader:
    """Fully automated trading system with continuous operation"""
    
    def __init__(self):
        self.state = TradingState()
        self.client = None
        self.account_id = None  # Will be set per-user, not from env at startup
        self.monitoring_threads = {}  # Dict to track monitoring threads per trade
        self.last_health_check = datetime.now()
        
        # Initialize OANDA client (optional - only if env vars are set for legacy support)
        self._initialize_client()
        
        # Load existing state if available
        self._load_state()

        # Apply environment overrides for automation limits
        try:
            env_max_conc = os.getenv("MAX_CONCURRENT_TRADES")
            if env_max_conc is not None:
                self.state.max_concurrent_trades = int(env_max_conc)
            env_max_daily = os.getenv("MAX_TRADES_PER_DAY")
            if env_max_daily is not None:
                self.state.max_trades_per_day = int(env_max_daily)
        except Exception:
            pass
        
        print("[AUTOMATED] 🤖 Automated 4H Forex Trading System Initialized")
        print(f"[AUTOMATED] 📊 Max concurrent trades: {self.state.max_concurrent_trades}")
        print(f"[AUTOMATED] 📈 Max trades per day: {self.state.max_trades_per_day}")
    
    def _initialize_client(self):
        """Initialize OANDA API client (optional - only if env vars are set for legacy support).
        Per-user credentials should be used instead via enhanced_main."""
        try:
            token = os.getenv("OANDA_API_KEY")
            account_id = os.getenv("OANDA_ACCOUNT_ID")
            if not token or not account_id:
                print("[AUTOMATED] ℹ️ OANDA credentials not found in environment.")
                print("[AUTOMATED] ℹ️ This is expected - per-user credentials will be used via enhanced_main.")
                self.client = None
                self.account_id = None
                return
            
            self.client = oandapyV20.API(access_token=token, environment="live")
            self.account_id = account_id
            print("[AUTOMATED] ✅ OANDA client initialized (legacy mode - using env vars)")
        except Exception as e:
            print(f"[AUTOMATED] ⚠️ Failed to initialize OANDA client: {e}")
            self.client = None
            self.account_id = None
    
    def _load_state(self):
        """Load previous trading state"""
        try:
            if os.path.exists("automated_state.json"):
                with open("automated_state.json", "r") as f:
                    data = json.load(f)
                    
                self.state.active_pairs = data.get("active_pairs", [])
                self.state.total_trades_today = data.get("total_trades_today", 0)
                
                # Reset daily counter if it's a new day
                last_reset = data.get("last_reset_date")
                if last_reset != datetime.now().strftime("%Y-%m-%d"):
                    self.state.total_trades_today = 0
                    
                print(f"[AUTOMATED] 📥 State loaded: {len(self.state.active_pairs)} active pairs")
        except Exception as e:
            print(f"[AUTOMATED] ⚠️ Could not load previous state: {e}")
    
    def _save_state(self):
        """Save current trading state"""
        try:
            data = {
                "active_pairs": self.state.active_pairs,
                "total_trades_today": self.state.total_trades_today,
                "last_reset_date": datetime.now().strftime("%Y-%m-%d"),
                "last_update": datetime.now().isoformat()
            }
            
            with open("automated_state.json", "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[AUTOMATED] ⚠️ Could not save state: {e}")
    
    def detect_manual_trades(self):
        """Detect manually closed trades and update cache"""
        try:
            if not self.client or not self.account_id:
                return
            # Get current trades from OANDA
            r = TradesList(accountID=self.account_id)
            self.client.request(r)
            live_trades = {trade["id"]: trade for trade in r.response.get("trades", [])}
            
            # Get cached trades
            cached_trades = get_active_trades()
            
            # Find trades that exist in cache but not in live account
            for cached_trade in cached_trades[:]:  # Create copy to iterate over
                trade_id = cached_trade.get("trade_id")
                if trade_id and trade_id not in live_trades:
                    print(f"[AUTOMATED] 🔍 Detected manually closed trade: {trade_id}")
                    
                    # Log the manual closure
                    add_log_entry({
                        "symbol": cached_trade.get("instrument", "UNKNOWN"),
                        "result": {"status": "MANUALLY_CLOSED", "message": "Trade closed manually"},
                        "entry_price": cached_trade.get("entry_price", 0),
                        "exit_price": "Manual",
                        "side": cached_trade.get("side", "unknown"),
                        "trade_id": trade_id,
                        "manual_closure": True
                    })
                    
                    # Update database for manually closed trade (persistence layer)
                    try:
                        # Try to get exit price from live trades if available
                        exit_price = None
                        pnl_net = None
                        
                        # Try to get final trade details from OANDA
                        try:
                            trade_details_req = oandapyV20.endpoints.trades.TradeDetails(accountID=self.account_id, tradeID=trade_id)
                            self.client.request(trade_details_req)
                            trade_data = trade_details_req.response.get("trade", {})
                            
                            # Get realized P/L if available
                            realized_pl = trade_data.get("realizedPL")
                            if realized_pl:
                                pnl_net = float(realized_pl)
                            
                            # Get average close price if available
                            close_price = trade_data.get("averageClosePrice")
                            if close_price:
                                exit_price = float(close_price)
                        except Exception:
                            # If we can't get trade details, use current price
                            try:
                                instrument = cached_trade.get("instrument", "UNKNOWN")
                                from monitor import _safe_price_from_pricing
                                import oandapyV20.endpoints.pricing as pricing
                                price_req = pricing.PricingInfo(accountID=self.account_id, params={"instruments": instrument})
                                self.client.request(price_req)
                                exit_price = _safe_price_from_pricing(price_req.response, cached_trade.get("side", "buy"), instrument)
                            except Exception:
                                pass
                        
                        # Update database
                        update_trade_close_from_oanda_account(
                            oanda_account_id=self.account_id,
                            external_id=str(trade_id),
                            exit_price=exit_price,
                            pnl_net=pnl_net,
                            closed_at=datetime.now(timezone.utc),
                            reason_close="MANUALLY_CLOSED",
                        )
                        print(f"[DB] ✅ Manually closed trade {trade_id} saved to database")
                    except Exception as db_error:
                        # Log error but don't fail the detection
                        print(f"[DB] ❌ Error saving manually closed trade to database: {db_error}")
                    
                    # Remove from cache and active pairs
                    remove_trade(trade_id)
                    instrument = cached_trade.get("instrument", "").replace("_", "")
                    if instrument in self.state.active_pairs:
                        self.state.active_pairs.remove(instrument)
                        print(f"[AUTOMATED] 🧹 Removed {instrument} from active pairs")
            
            # Stop monitoring threads for manually closed trades
            for trade_id in list(self.monitoring_threads.keys()):
                if trade_id not in live_trades:
                    thread = self.monitoring_threads.pop(trade_id, None)
                    if thread and thread.is_alive():
                        # Note: Can't directly kill thread, but it will exit when it detects trade is gone
                        print(f"[AUTOMATED] 🛑 Monitoring thread for {trade_id} will be cleaned up")
                    
        except Exception as e:
            print(f"[AUTOMATED] ⚠️ Error detecting manual trades: {e}")
    
    def can_place_new_trade(self, instrument: str) -> bool:
        """Check if we can place a new trade"""
        
        # Check daily limit
        if self.state.total_trades_today >= self.state.max_trades_per_day:
            print(f"[AUTOMATED] 🚫 Daily trade limit reached: {self.state.total_trades_today}")
            return False
        
        # Check concurrent trades limit
        active_trades = get_active_trades()
        if len(active_trades) >= self.state.max_concurrent_trades:
            print(f"[AUTOMATED] 🚫 Max concurrent trades reached: {len(active_trades)}")
            return False
        
        # Check if we already have a trade on this pair
        clean_instrument = instrument.replace("_", "")
        if clean_instrument in self.state.active_pairs:
            print(f"[AUTOMATED] 🚫 Already trading {clean_instrument}")
            return False
        
        return True
    
    def start_trade_monitoring(self, trade_details: Dict):
        """Start monitoring a new trade in a separate thread"""
        trade_id = trade_details.get("trade_id")
        if not trade_id:
            print("[AUTOMATED] ⚠️ Cannot monitor trade without ID")
            return
        
        def monitor_wrapper():
            try:
                print(f"[AUTOMATED] 🔍 Starting monitoring for trade {trade_id}")
                result = monitor_trade(trade_details)
                
                # Clean up after trade completion
                instrument = trade_details.get("instrument", "").replace("_", "")
                if instrument in self.state.active_pairs:
                    self.state.active_pairs.remove(instrument)
                
                # Remove from monitoring threads
                self.monitoring_threads.pop(trade_id, None)
                
                print(f"[AUTOMATED] ✅ Trade {trade_id} monitoring completed: {result.get('status')}")
                
            except Exception as e:
                print(f"[AUTOMATED] ❌ Error monitoring trade {trade_id}: {e}")
                # Clean up on error
                self.monitoring_threads.pop(trade_id, None)
        
        # Start monitoring thread
        thread = threading.Thread(target=monitor_wrapper, daemon=True)
        thread.start()
        self.monitoring_threads[trade_id] = thread
    
    def execute_automated_trading_cycle(self):
        """Execute one complete trading cycle"""
        try:
            print(f"\n[AUTOMATED] 🔄 Starting trading cycle at {datetime.now().strftime('%H:%M:%S')}")
            
            # Detect any manually closed trades
            self.detect_manual_trades()
            
            # Check if we can place new trades
            active_trades = get_active_trades()
            available_slots = self.state.max_concurrent_trades - len(active_trades)
            
            if available_slots > 0:
                print(f"[AUTOMATED] 📊 {available_slots} trade slots available")
                
                # Try to place new trades
                try:
                    # Execute the enhanced scanner-based trading logic instead of GPT-idea flow
                    from enhanced_main import main as enhanced_main
                    
                    # Temporarily ensure live trading during automation
                    original_dry_run = os.environ.get("DRY_RUN", "false")
                    os.environ["DRY_RUN"] = "false"
                    
                    # Run enhanced trading session
                    enhanced_main()
                    
                    # Restore original dry run setting
                    os.environ["DRY_RUN"] = original_dry_run
                    
                    # Check if a new trade was placed
                    new_active_trades = get_active_trades()
                    if len(new_active_trades) > len(active_trades):
                        # New trade was placed
                        new_trade = None
                        for trade in new_active_trades:
                            if trade not in active_trades:
                                new_trade = trade
                                break
                        
                        if new_trade:
                            # Update state
                            instrument = new_trade.get("instrument", "").replace("_", "")
                            if instrument not in self.state.active_pairs:
                                self.state.active_pairs.append(instrument)
                            
                            self.state.total_trades_today += 1
                            
                            # Start monitoring the new trade
                            self.start_trade_monitoring(new_trade)
                            
                            print(f"[AUTOMATED] 🎯 New trade placed and monitoring started: {instrument}")
                    
                except Exception as e:
                    print(f"[AUTOMATED] ⚠️ Error in trading cycle: {e}")
            else:
                print("[AUTOMATED] 💤 All trade slots occupied, waiting...")
            
            # Update state
            self.state.last_scan_time = datetime.now()
            self._save_state()
            
        except Exception as e:
            print(f"[AUTOMATED] ❌ Critical error in trading cycle: {e}")
    
    def health_check(self):
        """Perform system health check"""
        try:
            if not self.client or not self.account_id:
                active_trades = len(get_active_trades())
                print(f"[AUTOMATED] 💚 Health Check - Active Trades: {active_trades} (OANDA client not available)")
                return
            # Check OANDA connection
            r = AccountDetails(self.account_id)
            self.client.request(r)
            balance = float(r.response['account']['balance'])
            
            # Check monitoring threads
            active_threads = sum(1 for t in self.monitoring_threads.values() if t.is_alive())
            active_trades = len(get_active_trades())
            
            print(f"[AUTOMATED] 💚 Health Check - Balance: ${balance:.2f}, Active Trades: {active_trades}, Monitoring Threads: {active_threads}")
            
            # Clean up dead threads
            dead_threads = [tid for tid, thread in self.monitoring_threads.items() if not thread.is_alive()]
            for tid in dead_threads:
                self.monitoring_threads.pop(tid, None)
            
            self.last_health_check = datetime.now()
            
        except Exception as e:
            print(f"[AUTOMATED] ❤️‍🩹 Health check failed: {e}")
    
    def _weekly_state_path(self) -> str:
        return "weekly_report_state.json"

    def _load_weekly_state(self) -> Dict:
        try:
            if os.path.exists(self._weekly_state_path()):
                with open(self._weekly_state_path(), "r") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return {"last_sent_week_end": None}

    def _save_weekly_state(self, state: Dict) -> None:
        try:
            with open(self._weekly_state_path(), "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _last_sunday(self, ref_dt: Optional[datetime] = None) -> datetime:
        ref = ref_dt or datetime.now()
        days_since_sunday = (ref.weekday() - 6) % 7
        week_end_date = (ref - timedelta(days=days_since_sunday)).date()
        return datetime.combine(week_end_date, datetime.max.time())

    def generate_weekly_report(self, end_dt: Optional[datetime] = None):
        """Generate weekly snapshot and conditionally email it.
        Always saves a local snapshot for audit reliability across restarts.
        If end_dt is provided, generate the report for the week ending at that Sunday.
        """
        try:
            print("[AUTOMATED] 📊 Generating weekly snapshot...")
            snapshot, json_path, csv_path = generate_and_save_weekly_snapshot(end_dt=end_dt)

            # If no trades in the snapshot, still keep the empty snapshot for audit trail
            total_trades = snapshot.get("summary", {}).get("total_trades", 0)
            if total_trades == 0:
                print(f"[AUTOMATED] ⚠️ Weekly snapshot has no trades. Saved: {json_path}")
            
            # Use snapshot to prepare email content (if enabled)
            enable_email = os.getenv("ENABLE_EMAIL", "true").lower() == "true"
            if not enable_email:
                print(f"[AUTOMATED] ✉️ Emails disabled (ENABLE_EMAIL=false). Snapshot saved at {json_path}")
                return

            period = snapshot.get("period", {})
            summary = snapshot.get("summary", {})
            by_pair = snapshot.get("by_pair", {})
            trades = snapshot.get("trades", [])

            email_subject = (
                f"📊 Weekly Trading Report - Week {period.get('start', '')[:10]} to {period.get('end', '')[:10]}"
            )

            email_body = f"""
🤖 AUTOMATED 4H FOREX TRADING SYSTEM
Weekly Performance Report (Snapshot-backed)

📅 PERIOD
━━━━━━━━━━━━━━━━━━━━━━━━━━
• Start: {period.get('start', '')}
• End:   {period.get('end', '')}

📈 PERFORMANCE SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━
• Total Trades: {summary.get('total_trades', 0)}
• Winning Trades: {summary.get('winning_trades', 0)}
• Losing Trades: {summary.get('losing_trades', 0)}
• Win Rate: {summary.get('win_rate', 0):.1f}%
• Total Pips: {summary.get('total_pips', 0):+.1f}
• Total Profit: ${summary.get('total_profit', 0):+.2f}

📊 BY PAIR (Pips, Trades)
━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
            for pair, data in by_pair.items():
                email_body += f"• {pair}: {data.get('pips', 0):+.1f} pips, {data.get('trades', 0)} trades\n"
            email_body += """

📊 DETAILED TRADE BREAKDOWN
━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

            for i, trade in enumerate(trades, 1):
                status = "✅" if trade.get("pips_profit", 0) > 0 else "❌"
                email_body += f"{i}. {status} {trade.get('symbol', 'N/A')} - {trade.get('side', 'N/A').upper()} - {trade.get('pips_profit', 0):+.1f} pips\n"


            send_email(email_subject, email_body)
            # Mark this week's report as sent
            state = self._load_weekly_state()
            state["last_sent_week_end"] = period.get("end")
            self._save_weekly_state(state)
            print(f"[AUTOMATED] 📧 Weekly report sent successfully (snapshot: {json_path})")

        except Exception as e:
            print(f"[AUTOMATED] ❌ Error generating weekly report: {e}")
    
    def start_automation(self):
        """Start the fully automated trading system"""
        print("[AUTOMATED] 🚀 Starting fully automated trading system...")
        
        self.state.is_running = True
        
        # Schedule weekly reports (every Sunday at 23:00)
        schedule.every().sunday.at("23:00").do(self.generate_weekly_report)
        # Daily catch-up to ensure last week's report is sent even if the bot was off
        schedule.every().day.at("09:00").do(self.ensure_weekly_report_sent)
        
        # Schedule health checks (every hour)
        schedule.every().hour.do(self.health_check)
        
        # Main automation loop
        try:
            while self.state.is_running:
                # Run scheduled tasks
                schedule.run_pending()
                
                # Execute trading cycle every 30 minutes during favorable hours
                current_time = datetime.now()
                
                # Check if it's a favorable time for any major pairs
                favorable_time = any([
                    6 <= current_time.hour <= 18,   # European session
                    12 <= current_time.hour <= 22,  # American session
                    current_time.hour >= 22 or current_time.hour <= 10  # Asian session
                ])
                
                if favorable_time:
                    self.execute_automated_trading_cycle()
                    sleep_duration = 1800  # 30 minutes
                else:
                    print("[AUTOMATED] 😴 Outside favorable trading hours, extended sleep...")
                    sleep_duration = 3600  # 1 hour
                
                # Sleep with interruption check every minute
                for _ in range(sleep_duration // 60):
                    if not self.state.is_running:
                        break
                    time.sleep(60)
                    
                    # Quick health check every 10 minutes
                    if datetime.now().minute % 10 == 0:
                        print(f"[AUTOMATED] 💓 Quick status: {len(get_active_trades())} active trades")
                
        except KeyboardInterrupt:
            print("\n[AUTOMATED] 🛑 Shutdown signal received...")
            self.stop_automation()
        except Exception as e:
            print(f"[AUTOMATED] 💥 Critical system error: {e}")
            self.emergency_shutdown()
    
    def stop_automation(self):
        """Gracefully stop the automated system"""
        print("[AUTOMATED] 🛑 Stopping automated trading system...")
        
        self.state.is_running = False
        
        # Wait for monitoring threads to complete
        for trade_id, thread in self.monitoring_threads.items():
            if thread.is_alive():
                print(f"[AUTOMATED] ⏳ Waiting for trade {trade_id} monitoring to complete...")
                thread.join(timeout=30)  # Wait max 30 seconds per thread
        
        # Save final state
        self._save_state()
        
        # Send shutdown notification
        try:
            send_email(
                "🛑 Automated Trading System Shutdown",
                f"Automated trading system stopped at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Final Status:\n"
                f"• Active Trades: {len(get_active_trades())}\n"
                f"• Trades Today: {self.state.total_trades_today}\n"
                f"• Active Pairs: {', '.join(self.state.active_pairs) if self.state.active_pairs else 'None'}"
            )
        except:
            pass
        
        print("[AUTOMATED] ✅ Automated system stopped gracefully")

    def ensure_weekly_report_sent(self):
        """Ensure the latest completed week's report is sent even if the bot was off on Sunday.
        If multiple weeks were missed, send catch-up reports for each missing week.
        """
        try:
            state = self._load_weekly_state()
            last_sent_end_iso = state.get("last_sent_week_end")
            pending_weeks: List[datetime] = []

            # Compute last completed Sunday's end
            current_last_sunday = self._last_sunday()

            # Determine the next week to send
            if last_sent_end_iso:
                try:
                    last_sent_end = datetime.fromisoformat(last_sent_end_iso)
                except Exception:
                    last_sent_end = None
            else:
                last_sent_end = None

            # Start from the week after last_sent_end (or just current_last_sunday if none)
            if last_sent_end is None:
                candidate = current_last_sunday
            else:
                # Move to next Sunday after last_sent_end
                candidate = last_sent_end + timedelta(days=7)
                candidate = self._last_sunday(candidate)

            # Collect all missed Sundays up to current_last_sunday
            while candidate <= current_last_sunday:
                pending_weeks.append(candidate)
                candidate = candidate + timedelta(days=7)

            if not pending_weeks:
                print("[AUTOMATED] 📨 Weekly report up to date.")
                return

            # Send reports for each pending week in chronological order
            for week_end in sorted(pending_weeks):
                print(f"[AUTOMATED] 📨 Sending catch-up weekly report for week ending {week_end.date()}")
                self.generate_weekly_report(end_dt=week_end)
        except Exception as e:
            print(f"[AUTOMATED] ⚠️ ensure_weekly_report_sent failed: {e}")
    
    def emergency_shutdown(self):
        """Emergency shutdown with notifications"""
        print("[AUTOMATED] 🚨 EMERGENCY SHUTDOWN")
        
        self.state.is_running = False
        self._save_state()
        
        try:
            send_email(
                "🚨 URGENT: Trading System Emergency Shutdown",
                f"EMERGENCY: Automated trading system crashed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Please check system logs and restart manually.\n\n"
                f"Active trades may still be running - check your broker platform."
            )
        except:
            pass

def main():
    """Main entry point for automated trading"""
    print("🤖 Initializing Fully Automated 4H Forex Trading System")
    
    trader = AutomatedTrader()
    
    try:
        # Initial health check
        trader.health_check()
        
        # Start automation
        trader.start_automation()
        
    except Exception as e:
        print(f"❌ Failed to start automated trading: {e}")
        trader.emergency_shutdown()

if __name__ == "__main__":
    main()