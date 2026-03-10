"""
Lightweight performance analytics layer (reporting only).
Tracks per-trade metrics and rejection reasons; provides daily and weekly aggregate reports.
Does not alter strategy logic, entries, exits, or risk rules.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

ANALYTICS_FILE = "performance_analytics.json"
_lock = threading.Lock()


def _load() -> Dict[str, Any]:
    """Load analytics data from file."""
    with _lock:
        if not os.path.exists(ANALYTICS_FILE):
            return {"completed_trades": [], "rejections": []}
        try:
            with open(ANALYTICS_FILE, "r") as f:
                data = json.load(f)
                return {
                    "completed_trades": data.get("completed_trades", []),
                    "rejections": data.get("rejections", []),
                }
        except Exception:
            return {"completed_trades": [], "rejections": []}


def _save(data: Dict[str, Any]) -> None:
    """Save analytics data to file."""
    with _lock:
        try:
            with open(ANALYTICS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[ANALYTICS] ⚠️ Failed to save: {e}")


def record_completed_trade(
    trade_id: str,
    instrument: str,
    direction: str,
    strategy: str,
    entry_time: str,
    exit_time: str,
    score_at_entry: Optional[float],
    score_band_60_64: bool,
    initial_units: int,
    realized_pips: float,
    realized_r_multiple: Optional[float],
    hit_breakeven: bool,
    partial_taken: bool,
    trailing_closed: bool,
    had_pyramid_adds: bool,
    pyramid_units_added: int,
    realized_pnl: Optional[float],
    reason_exit: str,
) -> None:
    """Record one completed trade for analytics (reporting only)."""
    record = {
        "trade_id": str(trade_id),
        "instrument": instrument,
        "direction": direction,
        "strategy": strategy or "4H_MAIN",
        "entry_time": entry_time,
        "exit_time": exit_time,
        "score_at_entry": score_at_entry,
        "score_band_60_64": score_band_60_64,
        "initial_units": initial_units,
        "realized_pips": round(realized_pips, 2),
        "realized_r_multiple": round(realized_r_multiple, 2) if realized_r_multiple is not None else None,
        "hit_breakeven": hit_breakeven,
        "partial_taken": partial_taken,
        "trailing_closed": trailing_closed,
        "had_pyramid_adds": had_pyramid_adds,
        "pyramid_units_added": pyramid_units_added,
        "realized_pnl": round(realized_pnl, 2) if realized_pnl is not None else None,
        "reason_exit": reason_exit,
    }
    data = _load()
    data["completed_trades"].append(record)
    _save(data)


def record_rejection(symbol: str, direction: str, reason: str, detail: Optional[str] = None) -> None:
    """Record a rejected signal for analytics (reporting only)."""
    data = _load()
    data["rejections"].append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "direction": direction,
        "reason": reason,
        "detail": detail or "",
    })
    _save(data)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def get_aggregates(days: int = 1) -> Dict[str, Any]:
    """Compute aggregate stats for the last `days` days (from exit_time of completed trades and rejection ts)."""
    data = _load()
    trades = data.get("completed_trades", [])
    rejections = data.get("rejections", [])

    now = datetime.now(timezone.utc)
    period_start = now - timedelta(days=days)

    def in_period(exit_ts: Optional[str]) -> bool:
        t = _parse_iso(exit_ts)
        return t is not None and t >= period_start

    def rejection_in_period(r: Dict) -> bool:
        t = _parse_iso(r.get("ts"))
        return t is not None and t >= period_start

    period_trades = [t for t in trades if in_period(t.get("exit_time"))]
    period_rejections = [r for r in rejections if rejection_in_period(r)]

    # Rejection counts by reason
    rejection_counts: Dict[str, int] = {}
    for r in period_rejections:
        reason = r.get("reason", "unknown")
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    # Trades in score band 60-64
    band_trades = [t for t in period_trades if t.get("score_band_60_64") is True]
    n_band = len(band_trades)

    # Wins/losses by PnL
    wins = [t for t in period_trades if (t.get("realized_pnl") or 0) > 0]
    losses = [t for t in period_trades if (t.get("realized_pnl") or 0) < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    n_total = len(period_trades)

    avg_win = (sum(t.get("realized_pnl") or 0 for t in wins) / n_wins) if n_wins else 0.0
    avg_loss = (sum(t.get("realized_pnl") or 0 for t in losses) / n_losses) if n_losses else 0.0
    win_rate = (n_wins / n_total * 100.0) if n_total else 0.0

    r_multiples = [t.get("realized_r_multiple") for t in period_trades if t.get("realized_r_multiple") is not None]
    avg_r = (sum(r_multiples) / len(r_multiples)) if r_multiples else None

    # Expectancy: (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss) when using PnL
    expectancy = (win_rate / 100.0 * avg_win) + ((1 - win_rate / 100.0) * avg_loss) if n_total else 0.0

    # Band 60-64 win rate and expectancy
    band_wins = [t for t in band_trades if (t.get("realized_pnl") or 0) > 0]
    band_win_rate = (len(band_wins) / n_band * 100.0) if n_band else None
    band_avg_win = (sum(t.get("realized_pnl") or 0 for t in band_wins) / len(band_wins)) if band_wins else 0.0
    band_losses = [t for t in band_trades if (t.get("realized_pnl") or 0) < 0]
    band_avg_loss = (sum(t.get("realized_pnl") or 0 for t in band_losses) / len(band_losses)) if band_losses else 0.0
    band_expectancy = (band_win_rate / 100.0 * band_avg_win) + ((1 - band_win_rate / 100.0) * band_avg_loss) if n_band and band_win_rate is not None else None

    # Concurrent open trades: from entry_time/exit_time of period_trades
    def concurrent_at(t: datetime) -> int:
        count = 0
        for tr in period_trades:
            en = _parse_iso(tr.get("entry_time"))
            ex = _parse_iso(tr.get("exit_time"))
            if en and ex and en <= t < ex:
                count += 1
        return count

    concurrents: List[int] = []
    for tr in period_trades:
        ex = _parse_iso(tr.get("exit_time"))
        if ex:
            concurrents.append(concurrent_at(ex))
    avg_concurrent = (sum(concurrents) / len(concurrents)) if concurrents else 0.0
    max_concurrent = max(concurrents) if concurrents else 0

    # By instrument
    by_instrument: Dict[str, int] = {}
    for t in period_trades:
        inst = t.get("instrument") or "UNKNOWN"
        by_instrument[inst] = by_instrument.get(inst, 0) + 1

    # By strategy
    by_strategy: Dict[str, int] = {}
    for t in period_trades:
        st = t.get("strategy") or "4H_MAIN"
        by_strategy[st] = by_strategy.get(st, 0) + 1

    return {
        "period_days": days,
        "period_start": period_start.isoformat(),
        "total_trades": n_total,
        "win_rate": round(win_rate, 2),
        "average_win": round(avg_win, 2),
        "average_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "average_r_multiple": round(avg_r, 2) if avg_r is not None else None,
        "trades_score_band_60_64": n_band,
        "win_rate_band_60_64": round(band_win_rate, 2) if band_win_rate is not None else None,
        "expectancy_band_60_64": round(band_expectancy, 2) if band_expectancy is not None else None,
        "rejection_counts": rejection_counts,
        "average_concurrent_open_trades": round(avg_concurrent, 2),
        "max_concurrent_open_trades": max_concurrent,
        "total_trades_by_instrument": by_instrument,
        "total_trades_by_strategy": by_strategy,
    }


def print_daily_report() -> None:
    """Print aggregate report for the last 1 day."""
    ag = get_aggregates(days=1)
    _print_report(ag, "DAILY")


def print_weekly_report() -> None:
    """Print aggregate report for the last 7 days."""
    ag = get_aggregates(days=7)
    _print_report(ag, "WEEKLY")


def _print_report(ag: Dict[str, Any], label: str) -> None:
    """Format and print an aggregate report."""
    print(f"\n[ANALYTICS] ========== {label} PERFORMANCE REPORT ==========")
    print(f"  Period: last {ag['period_days']} day(s) (from {ag['period_start'][:19]})")
    print(f"  Total trades: {ag['total_trades']}")
    print(f"  Win rate: {ag['win_rate']}%")
    print(f"  Average win: {ag['average_win']} | Average loss: {ag['average_loss']}")
    print(f"  Expectancy: {ag['expectancy']}")
    print(f"  Average R multiple: {ag['average_r_multiple']}")
    print(f"  --- Score band 60-64 ---")
    print(f"  Trades in band: {ag['trades_score_band_60_64']}")
    print(f"  Win rate (band): {ag['win_rate_band_60_64']}%")
    print(f"  Expectancy (band): {ag['expectancy_band_60_64']}")
    print(f"  --- Rejections ---")
    for reason, count in sorted(ag["rejection_counts"].items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")
    print(f"  --- Concurrency ---")
    print(f"  Avg concurrent open: {ag['average_concurrent_open_trades']}")
    print(f"  Max concurrent open: {ag['max_concurrent_open_trades']}")
    print(f"  --- By instrument ---")
    for inst, n in sorted(ag["total_trades_by_instrument"].items(), key=lambda x: -x[1]):
        print(f"  {inst}: {n}")
    print(f"  --- By strategy ---")
    for st, n in sorted(ag["total_trades_by_strategy"].items(), key=lambda x: -x[1]):
        print(f"  {st}: {n}")
    print("[ANALYTICS] ==========================================\n")
