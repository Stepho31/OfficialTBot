import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional

LOG_FILE = "trading_log.json"
SNAPSHOT_DIR = "weekly_snapshots"

def load_log():
    """Load trading log from file"""
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                log_data = json.load(f)
                # Ensure log_data is a list
                return log_data if isinstance(log_data, list) else []
        except (json.JSONDecodeError, FileNotFoundError):
            print("[LOG] Warning: Could not load trading log, starting fresh")
            return []
    return []

def save_log(log_data):
    """Save trading log to file"""
    try:
        with open(LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=2, default=str)
    except Exception as e:
        print(f"[LOG] Error saving log: {e}")

def add_log_entry(entry):
    """Add a new entry to the trading log"""
    log_data = load_log()
    
    # Add timestamp if not present
    if "timestamp" not in entry:
        entry["timestamp"] = datetime.now().isoformat()
    
    # Ensure entry has required fields
    if "symbol" not in entry:
        entry["symbol"] = "UNKNOWN"
    
    log_data.append(entry)
    save_log(log_data)
    
    # Log the entry
    status = entry.get("result", {}).get("status", "UNKNOWN")
    symbol = entry.get("symbol", "UNKNOWN")
    pips = entry.get("pips_profit", 0)
    print(f"[LOG] 📝 Added entry: {symbol} - {status} ({pips:+.1f} pips)")

def get_weekly_performance(weeks_back: int = 1) -> List[Dict]:
    """Get trading performance for the last N weeks"""
    log_data = load_log()
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(weeks=weeks_back)
    
    weekly_trades = []
    
    for entry in log_data:
        try:
            entry_date = datetime.fromisoformat(entry.get("timestamp", ""))
            if start_date <= entry_date <= end_date:
                weekly_trades.append(entry)
        except (ValueError, TypeError):
            continue
    
    return weekly_trades

def get_daily_performance(days_back: int = 7) -> List[Dict]:
    """Get trading performance for the last N days"""
    log_data = load_log()
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    daily_trades = []
    
    for entry in log_data:
        try:
            entry_date = datetime.fromisoformat(entry.get("timestamp", ""))
            if start_date <= entry_date <= end_date:
                daily_trades.append(entry)
        except (ValueError, TypeError):
            continue
    
    return daily_trades

def get_pair_performance(symbol: str, days_back: int = 30) -> Dict:
    """Get performance statistics for a specific pair"""
    log_data = load_log()
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    pair_trades = []
    
    for entry in log_data:
        try:
            entry_date = datetime.fromisoformat(entry.get("timestamp", ""))
            entry_symbol = entry.get("symbol", "").replace("_", "")
            clean_symbol = symbol.replace("_", "")
            
            if (start_date <= entry_date <= end_date and 
                entry_symbol == clean_symbol):
                pair_trades.append(entry)
        except (ValueError, TypeError):
            continue
    
    if not pair_trades:
        return {
            "symbol": symbol,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0,
            "total_pips": 0,
            "average_pips": 0,
            "total_profit": 0
        }
    
    # Calculate statistics
    winning_trades = [t for t in pair_trades if t.get("pips_profit", 0) > 0]
    losing_trades = [t for t in pair_trades if t.get("pips_profit", 0) < 0]
    total_pips = sum(t.get("pips_profit", 0) for t in pair_trades)
    total_profit = sum(t.get("profit_amount", 0) for t in pair_trades)
    
    return {
        "symbol": symbol,
        "total_trades": len(pair_trades),
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "win_rate": (len(winning_trades) / len(pair_trades) * 100) if pair_trades else 0,
        "total_pips": total_pips,
        "average_pips": total_pips / len(pair_trades) if pair_trades else 0,
        "total_profit": total_profit,
        "best_trade": max(pair_trades, key=lambda x: x.get("pips_profit", 0)) if pair_trades else None,
        "worst_trade": min(pair_trades, key=lambda x: x.get("pips_profit", 0)) if pair_trades else None
    }

def get_overall_statistics(days_back: int = 30) -> Dict:
    """Get overall trading statistics"""
    trades = get_daily_performance(days_back)
    
    if not trades:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0,
            "total_pips": 0,
            "total_profit": 0,
            "average_pips_per_trade": 0,
            "best_day": None,
            "worst_day": None,
            "most_traded_pair": None
        }
    
    # Basic statistics
    winning_trades = [t for t in trades if t.get("pips_profit", 0) > 0]
    losing_trades = [t for t in trades if t.get("pips_profit", 0) < 0]
    total_pips = sum(t.get("pips_profit", 0) for t in trades)
    total_profit = sum(t.get("profit_amount", 0) for t in trades)
    
    # Daily performance
    daily_performance = {}
    for trade in trades:
        try:
            date = datetime.fromisoformat(trade.get("timestamp", "")).date()
            date_str = date.isoformat()
            
            if date_str not in daily_performance:
                daily_performance[date_str] = {"pips": 0, "profit": 0, "trades": 0}
            
            daily_performance[date_str]["pips"] += trade.get("pips_profit", 0)
            daily_performance[date_str]["profit"] += trade.get("profit_amount", 0)
            daily_performance[date_str]["trades"] += 1
        except:
            continue
    
    best_day = max(daily_performance.items(), key=lambda x: x[1]["pips"]) if daily_performance else None
    worst_day = min(daily_performance.items(), key=lambda x: x[1]["pips"]) if daily_performance else None
    
    # Most traded pair
    pair_counts = {}
    for trade in trades:
        symbol = trade.get("symbol", "UNKNOWN")
        pair_counts[symbol] = pair_counts.get(symbol, 0) + 1
    
    most_traded_pair = max(pair_counts.items(), key=lambda x: x[1]) if pair_counts else None
    
    return {
        "total_trades": len(trades),
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "win_rate": (len(winning_trades) / len(trades) * 100) if trades else 0,
        "total_pips": total_pips,
        "total_profit": total_profit,
        "average_pips_per_trade": total_pips / len(trades) if trades else 0,
        "best_day": best_day,
        "worst_day": worst_day,
        "most_traded_pair": most_traded_pair,
        "daily_performance": daily_performance
    }

def get_strongest_pairs(days_back: int = 7, top_n: int = 5) -> List[Dict]:
    """Get the strongest performing pairs over the specified period"""
    trades = get_daily_performance(days_back)
    
    # Group by pair
    pair_performance = {}
    
    for trade in trades:
        symbol = trade.get("symbol", "UNKNOWN")
        if symbol not in pair_performance:
            pair_performance[symbol] = {
                "symbol": symbol,
                "total_pips": 0,
                "total_profit": 0,
                "trade_count": 0,
                "winning_trades": 0
            }
        
        pips = trade.get("pips_profit", 0)
        pair_performance[symbol]["total_pips"] += pips
        pair_performance[symbol]["total_profit"] += trade.get("profit_amount", 0)
        pair_performance[symbol]["trade_count"] += 1
        
        if pips > 0:
            pair_performance[symbol]["winning_trades"] += 1
    
    # Calculate win rates and sort by total pips
    for pair_data in pair_performance.values():
        pair_data["win_rate"] = (pair_data["winning_trades"] / pair_data["trade_count"] * 100) if pair_data["trade_count"] > 0 else 0
        pair_data["average_pips"] = pair_data["total_pips"] / pair_data["trade_count"] if pair_data["trade_count"] > 0 else 0
    
    # Sort by total pips (descending) and return top N
    sorted_pairs = sorted(pair_performance.values(), key=lambda x: x["total_pips"], reverse=True)
    
    return sorted_pairs[:top_n]

def cleanup_old_entries(days_to_keep: int = 90):
    """Remove log entries older than specified days"""
    log_data = load_log()
    current_time = datetime.now()
    cutoff_date = current_time - timedelta(days=days_to_keep)
    
    cleaned_log = []
    removed_count = 0
    
    for entry in log_data:
        try:
            entry_date = datetime.fromisoformat(entry.get("timestamp", ""))
            if entry_date >= cutoff_date:
                cleaned_log.append(entry)
            else:
                removed_count += 1
        except:
            # Keep entries with invalid timestamps
            cleaned_log.append(entry)
    
    if removed_count > 0:
        save_log(cleaned_log)
        print(f"[LOG] 🧹 Cleaned up {removed_count} old entries (keeping last {days_to_keep} days)")
    
    return removed_count

def export_log_to_csv(filename: str = None, days_back: int = 30) -> str:
    """Export trading log to CSV format"""
    import csv
    
    if filename is None:
        filename = f"trading_log_{datetime.now().strftime('%Y%m%d')}.csv"
    
    trades = get_daily_performance(days_back)
    
    if not trades:
        print("[LOG] No trades to export")
        return ""
    
    try:
        with open(filename, 'w', newline='') as csvfile:
            fieldnames = ['timestamp', 'symbol', 'side', 'entry_price', 'exit_price', 
                         'pips_profit', 'profit_amount', 'status', 'position_size']
            
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            for trade in trades:
                row = {}
                for field in fieldnames:
                    if field == 'status':
                        row[field] = trade.get('result', {}).get('status', 'UNKNOWN')
                    elif field == 'side':
                        row[field] = trade.get('side', trade.get('direction', 'UNKNOWN'))
                    else:
                        row[field] = trade.get(field, '')
                
                writer.writerow(row)
        
        print(f"[LOG] 📊 Exported {len(trades)} trades to {filename}")
        return filename
        
    except Exception as e:
        print(f"[LOG] ❌ Error exporting to CSV: {e}")
        return ""

# ===== Weekly Snapshot Utilities =====
def _ensure_snapshot_dir():
    try:
        if not os.path.exists(SNAPSHOT_DIR):
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    except Exception as e:
        print(f"[LOG] ❌ Failed to ensure snapshot directory: {e}")

def _get_week_period(end_dt: Optional[datetime] = None) -> Dict[str, datetime]:
    """Compute the [start, end] datetimes (UTC) for the week ending on the last Sunday.
    If end_dt is None, use now().
    Week runs Monday 00:00:00 to Sunday 23:59:59 (7 days).
    """
    if end_dt is None:
        end_dt = datetime.now()
    # Python weekday: Monday=0 .. Sunday=6. Find the most recent Sunday.
    days_since_sunday = (end_dt.weekday() - 6) % 7
    week_end_date = (end_dt - timedelta(days=days_since_sunday)).date()
    week_start_date = week_end_date - timedelta(days=6)
    week_start = datetime.combine(week_start_date, datetime.min.time())
    week_end = datetime.combine(week_end_date, datetime.max.time())
    return {"start": week_start, "end": week_end}

def _get_trades_in_range(start_dt: datetime, end_dt: datetime) -> List[Dict]:
    log_data = load_log()
    results: List[Dict] = []
    for entry in log_data:
        try:
            entry_dt = datetime.fromisoformat(entry.get("timestamp", ""))
            if start_dt <= entry_dt <= end_dt:
                results.append(entry)
        except Exception:
            continue
    return results

def generate_and_save_weekly_snapshot(end_dt: Optional[datetime] = None, save_csv: bool = True):
    """Generate weekly snapshot for the last full week ending Sunday and save JSON (and CSV).
    Returns (snapshot_dict, json_path, csv_path_or_empty).
    Always saves locally to ensure an audit trail even if emails are disabled.
    """
    _ensure_snapshot_dir()
    period = _get_week_period(end_dt)
    start_dt = period["start"]
    end_dt = period["end"]
    trades = _get_trades_in_range(start_dt, end_dt)

    # Compute stats
    total_trades = len(trades)
    winning_trades = [t for t in trades if t.get("pips_profit", 0) > 0]
    losing_trades = [t for t in trades if t.get("pips_profit", 0) < 0]
    win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0.0
    total_pips = sum(t.get("pips_profit", 0) for t in trades)
    total_profit = sum(t.get("profit_amount", 0) for t in trades)

    # Pair breakdown
    pair_perf: Dict[str, Dict] = {}
    for t in trades:
        pair = t.get("symbol", "UNKNOWN")
        if pair not in pair_perf:
            pair_perf[pair] = {"pips": 0.0, "trades": 0}
        pair_perf[pair]["pips"] += t.get("pips_profit", 0)
        pair_perf[pair]["trades"] += 1

    strongest_pair = None
    if pair_perf:
        strongest_pair = max(pair_perf.items(), key=lambda x: x[1]["pips"])  # (pair, {pips, trades})

    snapshot = {
        "period": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat()
        },
        "summary": {
            "total_trades": total_trades,
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": win_rate,
            "total_pips": total_pips,
            "total_profit": total_profit,
        },
        "by_pair": {pair: {"pips": data["pips"], "trades": data["trades"]} for pair, data in pair_perf.items()},
        "trades": trades,
    }

    # File paths
    basename = f"weekly_snapshot_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
    json_path = os.path.join(SNAPSHOT_DIR, f"{basename}.json")
    csv_path = os.path.join(SNAPSHOT_DIR, f"{basename}.csv") if save_csv else ""

    # Save JSON
    try:
        with open(json_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        print(f"[LOG] 💾 Weekly snapshot saved: {json_path}")
    except Exception as e:
        print(f"[LOG] ❌ Failed to save weekly snapshot JSON: {e}")

    # Save CSV if requested
    if save_csv:
        try:
            import csv
            with open(csv_path, "w", newline="") as csvfile:
                fieldnames = [
                    'timestamp', 'symbol', 'side', 'entry_price', 'exit_price',
                    'pips_profit', 'profit_amount', 'status', 'position_size'
                ]
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for t in trades:
                    row = {}
                    for field in fieldnames:
                        if field == 'status':
                            row[field] = t.get('result', {}).get('status', 'UNKNOWN')
                        elif field == 'side':
                            row[field] = t.get('side', t.get('direction', 'UNKNOWN'))
                        else:
                            row[field] = t.get(field, '')
                    writer.writerow(row)
            print(f"[LOG] 📊 Weekly snapshot CSV saved: {csv_path}")
        except Exception as e:
            print(f"[LOG] ❌ Failed to save weekly snapshot CSV: {e}")
            csv_path = ""

    return snapshot, json_path, csv_path

def load_latest_weekly_snapshot():
    """Load the most recent weekly snapshot JSON if available."""
    _ensure_snapshot_dir()
    try:
        files = [f for f in os.listdir(SNAPSHOT_DIR) if f.startswith("weekly_snapshot_") and f.endswith('.json')]
        if not files:
            return None
        files.sort(reverse=True)
        with open(os.path.join(SNAPSHOT_DIR, files[0]), 'r') as f:
            return json.load(f)
    except Exception:
        return None