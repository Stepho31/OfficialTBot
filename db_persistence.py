"""
Database persistence module for saving bot trades to Postgres database.

This module provides a thin persistence layer that saves trade data
to the same Postgres database used by OfficialTBot-api, without
modifying any trading logic or strategy.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError

# Import models from the API (optional - will gracefully degrade if unavailable)
# We need to add the API directory to the Python path
import sys
from pathlib import Path

# Get the workspace root (parent of both OfficialTBot and OfficialTBot-api)
workspace_root = Path(__file__).parent.parent
api_path = workspace_root / "OfficialTBot-api"

# Add API path to sys.path if not already there
if str(api_path) not in sys.path:
    sys.path.insert(0, str(api_path))

# Try to import models, but don't crash if unavailable
_models_available = False
try:
    from app.models import Trade, Account, BrokerCredential, User
    _models_available = True
except ImportError as e:
    # Log but don't raise - this module will gracefully degrade
    logging.warning(f"Database persistence models not available: {e}")
    logging.warning("Database persistence features will be disabled. Continuing without syncing trades to database.")
    # Create dummy classes to prevent NameError
    Trade = None
    Account = None
    BrokerCredential = None
    User = None

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection
_engine = None
_SessionLocal = None


def get_db_connection():
    """Initialize database connection using DATABASE_URL from environment."""
    global _engine, _SessionLocal
    
    if not _models_available:
        logger.warning("Database models not available, cannot initialize connection")
        return None
    
    if _engine is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL environment variable is not set")
        
        try:
            _engine = create_engine(database_url, pool_pre_ping=True)
            _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
            logger.info("Database connection initialized")
        except Exception as e:
            logger.error(f"Failed to initialize database connection: {e}")
            raise
    
    return _SessionLocal


def get_db_session() -> Optional[Session]:
    """Get a database session."""
    SessionLocal = get_db_connection()
    if SessionLocal is None:
        return None
    return SessionLocal()


def lookup_user_and_account(oanda_account_id: str) -> Optional[Tuple[int, int]]:
    """
    Lookup user_id and account_id from OANDA account_id.
    
    Args:
        oanda_account_id: The OANDA account ID (e.g., "101-001-12345678-001")
        
    Returns:
        Tuple of (user_id, account_id) if found, None otherwise
    """
    if not _models_available:
        logger.warning("Database models not available, cannot lookup user and account")
        return None
    
    db = get_db_session()
    if db is None:
        return None
    
    try:
        # First, find the BrokerCredential by OANDA account_id
        broker_cred = db.execute(
            select(BrokerCredential).where(BrokerCredential.oanda_account_id == oanda_account_id)
        ).scalar_one_or_none()
        
        if not broker_cred:
            logger.warning(f"BrokerCredential not found for OANDA account_id: {oanda_account_id}")
            return None
        
        user_id = broker_cred.user_id
        
        # Now find the Account record for this user and OANDA account_id
        account = db.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.account_id == oanda_account_id,
                Account.broker == "OANDA"
            )
        ).scalar_one_or_none()
        
        if not account:
            # Account doesn't exist - create it automatically from BrokerCredential
            # This ensures trades can be saved even if Account wasn't created via /accounts endpoint
            logger.info(f"Account not found for user_id {user_id} and OANDA account_id {oanda_account_id}, creating...")
            try:
                account = Account(
                    user_id=user_id,
                    account_id=oanda_account_id,
                    broker="OANDA",
                    is_primary=True,  # Assume primary if it's the only one
                )
                db.add(account)
                db.commit()
                db.refresh(account)
                logger.info(f"Created Account record for user_id {user_id} and OANDA account_id {oanda_account_id}")
            except Exception as e:
                logger.error(f"Failed to create Account record: {e}")
                db.rollback()
                return None
        
        return (user_id, account.id)
        
    except SQLAlchemyError as e:
        logger.error(f"Database error looking up user and account: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error looking up user and account: {e}")
        return None
    finally:
        db.close()


def save_trade_open(
    user_id: int,
    account_id: int,
    external_id: str,
    instrument: str,
    side: str,
    units: int,
    entry_price: float,
    opened_at: Optional[datetime] = None,
    reason_open: Optional[str] = None,
    commission: Optional[float] = None,
    spread_cost: Optional[float] = None,
    slippage_cost: Optional[float] = None,
) -> bool:
    """
    Save a trade when it opens.
    
    Args:
        user_id: User ID from the database
        account_id: Account ID from the database
        external_id: OANDA trade ID (external_id in Trade model)
        instrument: Trading instrument (e.g., "EUR_USD")
        side: Trade side ("buy" or "sell")
        units: Position size in units
        entry_price: Entry price
        opened_at: Timestamp when trade was opened (defaults to now)
        reason_open: Reason for opening the trade (optional)
        commission: Commission cost (optional)
        spread_cost: Spread cost (optional)
        slippage_cost: Slippage cost (optional)
        
    Returns:
        True if saved successfully, False otherwise
    """
    if not _models_available:
        logger.warning("Database models not available, cannot save trade")
        return False
    
    db = get_db_session()
    if db is None:
        return False
    
    try:
        # Check if trade already exists
        existing_trade = db.execute(
            select(Trade).where(
                Trade.user_id == user_id,
                Trade.external_id == external_id
            )
        ).scalar_one_or_none()
        
        if existing_trade:
            logger.info(f"Trade {external_id} already exists, updating...")
            trade = existing_trade
        else:
            trade = Trade(
                user_id=user_id,
                account_id=account_id,
                external_id=external_id,
            )
            db.add(trade)
        
        # Update trade fields
        trade.instrument = instrument
        trade.side = side.upper() if side else None
        trade.units = units
        trade.entry_price = Decimal(str(entry_price)) if entry_price is not None else None
        trade.opened_at = opened_at or datetime.now(timezone.utc)
        trade.reason_open = reason_open
        trade.commission = Decimal(str(commission)) if commission is not None else None
        trade.spread_cost = Decimal(str(spread_cost)) if spread_cost is not None else None
        trade.slippage_cost = Decimal(str(slippage_cost)) if slippage_cost is not None else None
        
        db.commit()
        logger.info(f"Trade {external_id} saved successfully (opened)")
        return True
        
    except SQLAlchemyError as e:
        logger.error(f"Database error saving trade open: {e}")
        db.rollback()
        return False
    except Exception as e:
        logger.error(f"Unexpected error saving trade open: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def save_trade_close(
    user_id: int,
    external_id: str,
    exit_price: Optional[float] = None,
    pnl_net: Optional[float] = None,
    closed_at: Optional[datetime] = None,
    reason_close: Optional[str] = None,
) -> bool:
    """
    Update a trade when it closes.
    
    Args:
        user_id: User ID from the database
        external_id: OANDA trade ID (external_id in Trade model)
        exit_price: Exit price (optional)
        pnl_net: Net profit/loss (optional)
        closed_at: Timestamp when trade was closed (defaults to now)
        reason_close: Reason for closing the trade (optional)
        
    Returns:
        True if updated successfully, False otherwise
    """
    if not _models_available:
        logger.warning("Database models not available, cannot save trade close")
        return False
    
    db = get_db_session()
    if db is None:
        return False
    
    try:
        # Find the trade
        trade = db.execute(
            select(Trade).where(
                Trade.user_id == user_id,
                Trade.external_id == external_id
            )
        ).scalar_one_or_none()
        
        if not trade:
            logger.warning(f"Trade {external_id} not found for user {user_id}, cannot update")
            return False
        
        # Update trade fields
        if exit_price is not None:
            trade.exit_price = Decimal(str(exit_price))
        if pnl_net is not None:
            trade.pnl_net = Decimal(str(pnl_net))
        trade.closed_at = closed_at or datetime.now(timezone.utc)
        if reason_close:
            trade.reason_close = reason_close
        
        db.commit()
        logger.info(f"Trade {external_id} updated successfully (closed)")
        return True
        
    except SQLAlchemyError as e:
        logger.error(f"Database error saving trade close: {e}")
        db.rollback()
        return False
    except Exception as e:
        logger.error(f"Unexpected error saving trade close: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def save_trade_from_oanda_account(
    oanda_account_id: str,
    external_id: str,
    instrument: str,
    side: str,
    units: int,
    entry_price: float,
    opened_at: Optional[datetime] = None,
    reason_open: Optional[str] = None,
    commission: Optional[float] = None,
    spread_cost: Optional[float] = None,
    slippage_cost: Optional[float] = None,
) -> bool:
    """
    Convenience function to save a trade open using OANDA account_id.
    This function looks up the user_id and account_id automatically.
    
    Args:
        oanda_account_id: OANDA account ID
        external_id: OANDA trade ID
        instrument: Trading instrument
        side: Trade side
        units: Position size
        entry_price: Entry price
        opened_at: Timestamp when trade was opened
        reason_open: Reason for opening
        commission: Commission cost
        spread_cost: Spread cost
        slippage_cost: Slippage cost
        
    Returns:
        True if saved successfully, False otherwise
    """
    lookup_result = lookup_user_and_account(oanda_account_id)
    if not lookup_result:
        logger.error(f"Cannot save trade - user/account not found for OANDA account {oanda_account_id}")
        return False
    
    user_id, account_id = lookup_result
    return save_trade_open(
        user_id=user_id,
        account_id=account_id,
        external_id=external_id,
        instrument=instrument,
        side=side,
        units=units,
        entry_price=entry_price,
        opened_at=opened_at,
        reason_open=reason_open,
        commission=commission,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
    )


def update_trade_close_from_oanda_account(
    oanda_account_id: str,
    external_id: str,
    exit_price: Optional[float] = None,
    pnl_net: Optional[float] = None,
    closed_at: Optional[datetime] = None,
    reason_close: Optional[str] = None,
) -> bool:
    """
    Convenience function to update a trade close using OANDA account_id.
    This function looks up the user_id automatically.
    
    Args:
        oanda_account_id: OANDA account ID
        external_id: OANDA trade ID
        exit_price: Exit price
        pnl_net: Net profit/loss
        closed_at: Timestamp when trade was closed
        reason_close: Reason for closing
        
    Returns:
        True if updated successfully, False otherwise
    """
    lookup_result = lookup_user_and_account(oanda_account_id)
    if not lookup_result:
        logger.error(f"Cannot update trade - user/account not found for OANDA account {oanda_account_id}")
        return False
    
    user_id, _ = lookup_result
    return save_trade_close(
        user_id=user_id,
        external_id=external_id,
        exit_price=exit_price,
        pnl_net=pnl_net,
        closed_at=closed_at,
        reason_close=reason_close,
    )


def reconcile_trades_from_oanda(
    oanda_account_id: str,
    oanda_client,
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
) -> dict:
    """
    Reconcile trades from OANDA with database.
    Fetches all open trades from OANDA and ensures they exist in the database.
    
    Args:
        oanda_account_id: OANDA account ID
        oanda_client: OANDA API client instance
        user_id: Optional user_id (if known, avoids lookup)
        account_id: Optional account_id (if known, avoids lookup)
    
    Returns:
        dict with reconciliation results:
        {
            "trades_found": int,
            "trades_inserted": int,
            "trades_updated": int,
            "errors": list
        }
    """
    if not _models_available:
        logger.warning("Database models not available, cannot reconcile trades")
        return {"trades_found": 0, "trades_inserted": 0, "trades_updated": 0, "errors": ["Models not available"]}
    
    try:
        import oandapyV20
        from oandapyV20.endpoints.trades import TradesList
    except ImportError:
        logger.error("oandapyV20 not available for reconciliation")
        return {"trades_found": 0, "trades_inserted": 0, "trades_updated": 0, "errors": ["oandapyV20 not available"]}
    
    result = {
        "trades_found": 0,
        "trades_inserted": 0,
        "trades_updated": 0,
        "errors": []
    }
    
    # Lookup user_id and account_id if not provided
    if user_id is None or account_id is None:
        lookup_result = lookup_user_and_account(oanda_account_id)
        if not lookup_result:
            error_msg = f"Cannot reconcile - user/account not found for OANDA account {oanda_account_id}"
            logger.error(error_msg)
            result["errors"].append(error_msg)
            return result
        user_id, account_id = lookup_result
    
    db = get_db_session()
    if db is None:
        error_msg = "Cannot get database session for reconciliation"
        logger.error(error_msg)
        result["errors"].append(error_msg)
        return result
    
    try:
        # Fetch all open trades from OANDA
        r = TradesList(accountID=oanda_account_id)
        oanda_client.request(r)
        oanda_trades = r.response.get("trades", [])
        result["trades_found"] = len(oanda_trades)
        
        logger.info(f"[RECONCILE] Found {len(oanda_trades)} open trades on OANDA account {oanda_account_id}")
        
        for oanda_trade in oanda_trades:
            trade_id = str(oanda_trade.get("id"))
            if not trade_id:
                continue
            
            try:
                # Check if trade exists in database
                existing_trade = db.execute(
                    select(Trade).where(
                        Trade.user_id == user_id,
                        Trade.external_id == trade_id
                    )
                ).scalar_one_or_none()
                
                if existing_trade:
                    # Trade exists - update status if needed
                    if existing_trade.status != "OPEN" and not existing_trade.closed_at:
                        existing_trade.status = "OPEN"
                        existing_trade.closed_at = None
                        db.commit()
                        result["trades_updated"] += 1
                        logger.info(f"[RECONCILE] Updated status for existing trade {trade_id}")
                else:
                    # Trade missing - insert it
                    instrument = oanda_trade.get("instrument", "")
                    side_units = oanda_trade.get("currentUnits", "0")
                    try:
                        units = abs(int(side_units)) if side_units else 0
                        side_int = int(side_units) if side_units else 0
                        side_str = "BUY" if side_int > 0 else "SELL" if side_int < 0 else None
                    except (ValueError, TypeError):
                        logger.warning(f"[RECONCILE] Invalid currentUnits for trade {trade_id}: {side_units}")
                        units = 0
                        side_str = None
                    
                    try:
                        entry_price = float(oanda_trade.get("price", 0))
                    except (ValueError, TypeError):
                        logger.warning(f"[RECONCILE] Invalid price for trade {trade_id}: {oanda_trade.get('price')}")
                        entry_price = 0.0
                    opened_at_str = oanda_trade.get("openTime", "")
                    
                    # Parse opened_at timestamp
                    opened_at = None
                    if opened_at_str:
                        try:
                            # OANDA timestamps are in RFC3339 format (e.g., "2023-01-01T12:00:00.000000000Z")
                            # Try parsing with datetime.fromisoformat first (Python 3.7+)
                            if opened_at_str.endswith('Z'):
                                opened_at_str = opened_at_str[:-1] + '+00:00'
                            opened_at = datetime.fromisoformat(opened_at_str.replace('Z', '+00:00'))
                            if opened_at.tzinfo is None:
                                opened_at = opened_at.replace(tzinfo=timezone.utc)
                        except Exception:
                            # Fallback to current time if parsing fails
                            opened_at = datetime.now(timezone.utc)
                    else:
                        opened_at = datetime.now(timezone.utc)
                    
                    # Get unrealized P/L
                    unrealized_pl = oanda_trade.get("unrealizedPL")
                    pnl_net = float(unrealized_pl) if unrealized_pl is not None else None
                    
                    # Create trade record
                    new_trade = Trade(
                        user_id=user_id,
                        account_id=account_id,
                        external_id=trade_id,
                        instrument=instrument,
                        side=side_str,
                        units=units,
                        entry_price=Decimal(str(entry_price)) if entry_price else None,
                        opened_at=opened_at,
                        status="OPEN",
                        pnl_net=Decimal(str(pnl_net)) if pnl_net is not None else None,
                        reason_open="Reconciled from OANDA",
                    )
                    db.add(new_trade)
                    db.commit()
                    result["trades_inserted"] += 1
                    logger.info(f"[RECONCILE] Inserted missing trade {trade_id} ({instrument} {side_str} {units})")
                    
            except Exception as e:
                error_msg = f"Error reconciling trade {trade_id}: {e}"
                logger.error(error_msg)
                result["errors"].append(error_msg)
                db.rollback()
                continue
        
        logger.info(
            f"[RECONCILE] Reconciliation complete for account {oanda_account_id}: "
            f"{result['trades_inserted']} inserted, {result['trades_updated']} updated"
        )
        
    except Exception as e:
        error_msg = f"Error during reconciliation: {e}"
        logger.error(error_msg)
        result["errors"].append(error_msg)
    finally:
        db.close()
    
    return result

