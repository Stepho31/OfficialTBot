"""
Circuit Breaker Module for Drawdown and Loss-Streak Protection
Provides soft circuit breakers that reduce size/frequency during bad conditions
"""

import os
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from trading_log import get_daily_performance

CIRCUIT_BREAKER_STATE_FILE = "circuit_breaker_state.json"

# Configuration
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "8.0"))  # 8% max drawdown
LOSS_STREAK_THRESHOLD = int(os.getenv("LOSS_STREAK_THRESHOLD", "4"))  # 4 consecutive losses
CIRCUIT_BREAKER_REDUCTION = float(os.getenv("CIRCUIT_BREAKER_REDUCTION", "0.5"))  # 50% reduction when active
RECOVERY_THRESHOLD = float(os.getenv("RECOVERY_THRESHOLD", "2.0"))  # 2% recovery to reset


def load_circuit_breaker_state() -> Dict:
    """Load circuit breaker state from file"""
    if os.path.exists(CIRCUIT_BREAKER_STATE_FILE):
        try:
            with open(CIRCUIT_BREAKER_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {
        "active": False,
        "triggered_at": None,
        "trigger_reason": None,
        "recovery_check_count": 0,
    }


def save_circuit_breaker_state(state: Dict):
    """Save circuit breaker state to file"""
    try:
        with open(CIRCUIT_BREAKER_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"[CIRCUIT_BREAKER] Error saving state: {e}")


def calculate_drawdown(trades: list, initial_balance: float = None) -> Tuple[float, float]:
    """
    Calculate current drawdown percentage and peak equity.
    Returns (drawdown_pct, peak_equity)
    """
    if not trades:
        return 0.0, initial_balance or 10000.0
    
    # Calculate running equity
    equity = initial_balance or 10000.0
    peak_equity = equity
    max_drawdown = 0.0
    
    for trade in sorted(trades, key=lambda x: x.get("timestamp", "")):
        pnl = trade.get("profit_amount", 0) or trade.get("pips_profit", 0) * 10  # Rough conversion
        equity += pnl
        
        if equity > peak_equity:
            peak_equity = equity
        
        drawdown = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0.0
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    return max_drawdown, peak_equity


def check_loss_streak(trades: list, threshold: int = None) -> Tuple[bool, int]:
    """
    Check for consecutive losses.
    Returns (is_streak_active, streak_length)
    """
    if threshold is None:
        threshold = LOSS_STREAK_THRESHOLD
    
    if not trades:
        return False, 0
    
    # Get recent trades sorted by timestamp
    recent_trades = sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True)
    
    streak = 0
    for trade in recent_trades:
        pips = trade.get("pips_profit", 0)
        profit = trade.get("profit_amount", 0)
        
        # Consider it a loss if pips or profit is negative
        is_loss = (pips < 0) or (profit < 0)
        
        if is_loss:
            streak += 1
        else:
            break  # Streak broken
    
    return streak >= threshold, streak


def check_circuit_breaker_conditions() -> Tuple[bool, Optional[str]]:
    """
    Check if circuit breaker should be activated.
    Returns (should_activate, reason)
    """
    # Get recent trades (last 7 days)
    recent_trades = get_daily_performance(days_back=7)
    
    if not recent_trades:
        return False, None
    
    # Check drawdown
    drawdown_pct, peak_equity = calculate_drawdown(recent_trades)
    if drawdown_pct >= MAX_DRAWDOWN_PCT:
        return True, f"Drawdown threshold exceeded: {drawdown_pct:.2f}% >= {MAX_DRAWDOWN_PCT}%"
    
    # Check loss streak
    is_streak, streak_length = check_loss_streak(recent_trades)
    if is_streak:
        return True, f"Loss streak threshold exceeded: {streak_length} consecutive losses >= {LOSS_STREAK_THRESHOLD}"
    
    return False, None


def check_recovery() -> bool:
    """
    Check if system has recovered from circuit breaker conditions.
    Returns True if recovered.
    """
    recent_trades = get_daily_performance(days_back=3)  # Check last 3 days
    
    if not recent_trades:
        return False
    
    # Calculate recent performance
    recent_pnl = sum(t.get("profit_amount", 0) or t.get("pips_profit", 0) * 10 for t in recent_trades)
    recent_equity = 10000.0 + recent_pnl  # Rough estimate
    
    # Check if we've recovered by RECOVERY_THRESHOLD%
    state = load_circuit_breaker_state()
    if state.get("active") and state.get("triggered_at"):
        try:
            triggered_time = datetime.fromisoformat(state["triggered_at"])
            days_since_trigger = (datetime.now() - triggered_time).days
            
            # Require at least 1 day and positive recovery
            if days_since_trigger >= 1 and recent_pnl > 0:
                # Check if recovery threshold met
                recovery_pct = (recent_pnl / 10000.0) * 100  # Rough recovery calculation
                if recovery_pct >= RECOVERY_THRESHOLD:
                    return True
        except (ValueError, TypeError):
            pass
    
    return False


def get_circuit_breaker_status() -> Dict:
    """
    Get current circuit breaker status and risk adjustment factors.
    Returns dict with 'active', 'risk_multiplier', 'frequency_multiplier', 'reason'
    """
    state = load_circuit_breaker_state()
    
    # Check if circuit breaker should be activated
    should_activate, reason = check_circuit_breaker_conditions()
    
    if should_activate and not state.get("active"):
        # Activate circuit breaker
        state["active"] = True
        state["triggered_at"] = datetime.now().isoformat()
        state["trigger_reason"] = reason
        state["recovery_check_count"] = 0
        save_circuit_breaker_state(state)
        print(f"[CIRCUIT_BREAKER] 🚨 ACTIVATED: {reason}")
    
    # Check for recovery
    if state.get("active"):
        if check_recovery():
            state["active"] = False
            state["triggered_at"] = None
            state["trigger_reason"] = None
            state["recovery_check_count"] = 0
            save_circuit_breaker_state(state)
            print(f"[CIRCUIT_BREAKER] ✅ RECOVERED: Circuit breaker reset")
        else:
            state["recovery_check_count"] = state.get("recovery_check_count", 0) + 1
            save_circuit_breaker_state(state)
    
    # Return status
    if state.get("active"):
        return {
            "active": True,
            "risk_multiplier": CIRCUIT_BREAKER_REDUCTION,  # Reduce position size
            "frequency_multiplier": 0.5,  # Reduce trade frequency (skip 50% of opportunities)
            "reason": state.get("trigger_reason", "Unknown"),
        }
    else:
        return {
            "active": False,
            "risk_multiplier": 1.0,  # Normal size
            "frequency_multiplier": 1.0,  # Normal frequency
            "reason": None,
        }


