"""
ORM models for database persistence. Mirrors OfficialTBot-api app.models schema
so the trading bot can read/write the same Postgres database without depending on the API package.
Keep in sync with OfficialTBot-api/app/models.py for User, Account, Trade, BrokerCredential.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    LargeBinary,
    Numeric,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String, default="PENDING_PASSWORD")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(String(32), default="USER")
    has_tier1: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    password_reset_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    password_reset_expires: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    accounts: Mapped[list["Account"]] = relationship(back_populates="user")
    broker_credential: Mapped[Optional["BrokerCredential"]] = relationship(
        back_populates="user", uselist=False
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    broker: Mapped[str] = mapped_column(String, default="OANDA")
    account_id: Mapped[str] = mapped_column(String, nullable=False)

    token_encrypted: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    user: Mapped["User"] = relationship(back_populates="accounts")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("user_id", "external_id", name="uq_trades_user_external"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    external_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    instrument: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    side: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    units: Mapped[Optional[int]] = mapped_column(nullable=True)

    opened_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    entry_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    commission: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    spread_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    slippage_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    pnl_net: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)

    reason_open: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason_close: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    user: Mapped["User"] = relationship()
    account: Mapped["Account"] = relationship()


class BrokerCredential(Base):
    __tablename__ = "broker_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, unique=True)

    oanda_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enc_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    enc_iv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    enc_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="broker_credential")
