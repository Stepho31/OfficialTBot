"""
Helper functions for OANDA API operations, including per-user account operations.
"""

import oandapyV20
from oandapyV20.endpoints.trades import TradesList
from typing import List, Dict, Optional


def create_oanda_client(api_key: str, environment: str = "live") -> oandapyV20.API:
    """Create an OANDA API client with the provided API key."""
    return oandapyV20.API(access_token=api_key, environment=environment)


def get_user_open_positions(client: oandapyV20.API, account_id: str) -> List[Dict]:
    """
    Fetch open positions for a specific OANDA account.
    
    Args:
        client: OANDA API client
        account_id: OANDA account ID
        
    Returns:
        List of open trade dictionaries with keys: id, instrument, currentUnits, etc.
    """
    try:
        r = TradesList(accountID=account_id)
        client.request(r)
        trades = r.response.get("trades", [])
        # Filter to only open trades (currentUnits != 0)
        open_trades = [t for t in trades if float(t.get("currentUnits", 0)) != 0]
        return open_trades
    except Exception as e:
        print(f"[OANDA_HELPERS] ❌ Error fetching positions for account {account_id}: {e}")
        return []


def get_user_active_pairs(client: oandapyV20.API, account_id: str) -> List[str]:
    """
    Get list of active trading pairs for a user's account.
    
    Returns:
        List of normalized pair symbols (e.g., ["EURUSD", "GBPUSD"])
    """
    positions = get_user_open_positions(client, account_id)
    pairs = []
    for pos in positions:
        instrument = pos.get("instrument", "")
        if instrument:
            # Normalize to format without underscore
            clean_pair = instrument.replace("_", "")
            if clean_pair not in pairs:
                pairs.append(clean_pair)
    return pairs


def has_user_position_on_pair(client: oandapyV20.API, account_id: str, symbol: str, direction: Optional[str] = None) -> bool:
    """
    Check if user has an active position on a specific pair.
    
    Args:
        client: OANDA API client
        account_id: OANDA account ID
        symbol: Trading pair symbol (e.g., "EURUSD" or "EUR_USD")
        direction: Optional direction filter ("buy" or "sell")
        
    Returns:
        True if user has an active position matching the criteria
    """
    positions = get_user_open_positions(client, account_id)
    clean_symbol = symbol.replace("_", "").upper()
    
    for pos in positions:
        instrument = pos.get("instrument", "").replace("_", "").upper()
        if instrument == clean_symbol:
            if direction is None:
                return True
            # Check direction: positive units = buy, negative = sell
            current_units = float(pos.get("currentUnits", 0))
            if direction.lower() == "buy" and current_units > 0:
                return True
            if direction.lower() == "sell" and current_units < 0:
                return True
    return False

