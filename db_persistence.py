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

