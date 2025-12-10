import json
import os
from datetime import datetime
from typing import List, Dict, Optional

CACHE_FILE = "active_trades.json"

def load_trades():
    """Load active trades from cache file"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                trades = json.load(f)
                # Ensure trades is a list
                return trades if isinstance(trades, list) else []
        except (json.JSONDecodeError, FileNotFoundError):
            print("[CACHE] Warning: Could not load trades cache, starting fresh")
            return []
    return []

def save_trades(trades):
    try:
        if not isinstance(trades, list):
            trades = []
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp, CACHE_FILE)  # atomic on posix/nt
    except Exception as e:
        print(f"[CACHE] Error saving trades: {e}")


# Legacy function names for backward compatibility
def load_cache():
    return load_trades()

def save_cache(trades):
    save_trades(trades)

def add_trade(symbol, direction, entry_price, trade_id, **additional_data):
    trades = load_trades()
    clean_symbol = symbol.replace("_", "")

    # If we already have this trade_id, skip
    if any(str(t.get("trade_id")) == str(trade_id) for t in trades if t.get("trade_id")):
        print(f"[CACHE] ⚠️ Duplicate trade_id {trade_id}; not adding.")
        return False

    # Optional: prevent multiple positions same symbol+direction
    if any(t.get("symbol") == clean_symbol and t.get("direction") == direction.lower() for t in trades):
        print(f"[CACHE] ⚠️ Existing {clean_symbol} {direction.upper()} already active; not adding.")
        return False

    trade = {
        "symbol": clean_symbol,
        "instrument": symbol,
        "direction": direction.lower(),
        "side": direction.lower(),
        "entry_price": float(entry_price),
        "trade_id": str(trade_id),
        "timestamp": datetime.now().isoformat(),
        **additional_data
    }
    trades.append(trade)
    save_trades(trades)
    print(f"[CACHE] ✅ Added trade: {clean_symbol} {direction.upper()} (ID: {trade_id})")
    return True

def remove_trade(trade_id):
    """Remove a trade from the cache"""
    trades = load_trades()
    original_count = len(trades)
    
    # Remove trade with matching ID
    trades = [t for t in trades if str(t.get("trade_id")) != str(trade_id)]
    
    if len(trades) < original_count:
        save_trades(trades)
        print(f"[CACHE] 🗑️ Removed trade ID: {trade_id}")
        return True
    else:
        print(f"[CACHE] ⚠️ Trade ID {trade_id} not found in cache")
        return False

def get_active_trades() -> List[Dict]:
    """Get all active trades"""
    return load_trades()

def get_trade_by_id(trade_id: str) -> Optional[Dict]:
    """Get a specific trade by ID"""
    trades = load_trades()
    for trade in trades:
        if str(trade.get("trade_id")) == str(trade_id):
            return trade
    return None

def get_trades_by_symbol(symbol: str) -> List[Dict]:
    """Get all trades for a specific symbol"""
    trades = load_trades()
    clean_symbol = symbol.replace("_", "")
    return [t for t in trades if t.get("symbol") == clean_symbol]

def is_trade_active(symbol, direction=None):
    """Check if there's an active trade for a symbol/direction"""
    trades = load_trades()
    clean_symbol = symbol.replace("_", "")
    
    if direction:
        return any(t.get("symbol") == clean_symbol and t.get("direction") == direction.lower() for t in trades)
    else:
        return any(t.get("symbol") == clean_symbol for t in trades)

def get_active_pairs() -> List[str]:
    """Get list of currently active trading pairs"""
    trades = load_trades()
    return list(set(t.get("symbol", "") for t in trades if t.get("symbol")))

def update_trade(trade_id: str, updates: Dict) -> bool:
    """Update an existing trade with new data"""
    trades = load_trades()
    
    for trade in trades:
        if str(trade.get("trade_id")) == str(trade_id):
            trade.update(updates)
            save_trades(trades)
            print(f"[CACHE] 🔄 Updated trade {trade_id}: {list(updates.keys())}")
            return True
    
    print(f"[CACHE] ⚠️ Cannot update - trade {trade_id} not found")
    return False

def cleanup_stale_trades(max_age_hours: int = 72):
    """Remove trades older than specified hours (safety cleanup)"""
    trades = load_trades()
    current_time = datetime.now()
    cleaned_trades = []
    
    for trade in trades:
        try:
            trade_time = datetime.fromisoformat(trade.get("timestamp", ""))
            age_hours = (current_time - trade_time).total_seconds() / 3600
            
            if age_hours <= max_age_hours:
                cleaned_trades.append(trade)
            else:
                print(f"[CACHE] 🧹 Removed stale trade: {trade.get('symbol')} (age: {age_hours:.1f}h)")
        except:
            # Keep trades with invalid timestamps (better safe than sorry)
            cleaned_trades.append(trade)
    
    if len(cleaned_trades) != len(trades):
        save_trades(cleaned_trades)
        return len(trades) - len(cleaned_trades)
    
    return 0

def get_cache_stats() -> Dict:
    """Get statistics about the trade cache"""
    trades = load_trades()
    
    stats = {
        "total_trades": len(trades),
        "active_pairs": len(get_active_pairs()),
        "buy_trades": len([t for t in trades if t.get("direction") == "buy"]),
        "sell_trades": len([t for t in trades if t.get("direction") == "sell"]),
        "oldest_trade": None,
        "newest_trade": None
    }
    
    if trades:
        timestamps = []
        for trade in trades:
            try:
                timestamps.append(datetime.fromisoformat(trade.get("timestamp", "")))
            except:
                continue
        
        if timestamps:
            stats["oldest_trade"] = min(timestamps).isoformat()
            stats["newest_trade"] = max(timestamps).isoformat()
    
    return stats

def sync_cache_with_broker(client, account_id) -> int:
    """
    Validate cache against live broker state and remove any closed trades.
    
    Args:
        client: OANDA API client instance
        account_id: OANDA account ID
        
    Returns:
        Number of trades removed from cache
    """
    try:
        import oandapyV20
        from oandapyV20.endpoints.trades import TradesList
        
        # Get current trades from OANDA
        r = TradesList(accountID=account_id)
        client.request(r)
        live_trades = {trade["id"]: trade for trade in r.response.get("trades", [])}
        
        # Get cached trades
        cached_trades = get_active_trades()
        removed_count = 0
        
        # Find trades that exist in cache but not in live account
        for cached_trade in cached_trades[:]:  # Create copy to iterate over
            trade_id = cached_trade.get("trade_id")
            if trade_id and trade_id not in live_trades:
                print(f"[CACHE] 🔍 Trade {trade_id} not found in broker - removing from cache")
                if remove_trade(trade_id):
                    removed_count += 1
        
        if removed_count > 0:
            print(f"[CACHE] ✅ Synced cache with broker: removed {removed_count} closed trade(s)")
        else:
            print(f"[CACHE] ✅ Cache sync complete: all cached trades are still active")
        
        return removed_count
        
    except Exception as e:
        print(f"[CACHE] ⚠️ Error syncing cache with broker: {e}")
        return 0

def validate_and_cleanup_cache(client=None, account_id=None):
    """
    Validate cache and remove any trades that are no longer open.
    This is a safety function that can be called periodically to ensure cache integrity.
    
    Args:
        client: Optional OANDA API client (if None, only age-based cleanup runs)
        account_id: Optional OANDA account ID (required if client is provided)
    
    Returns:
        Total number of trades removed
    """
    removed_count = 0
    
    # Always run age-based cleanup
    removed_count += cleanup_stale_trades()
    
    # If broker credentials provided, sync with live state
    if client and account_id:
        removed_count += sync_cache_with_broker(client, account_id)
    
    return removed_count