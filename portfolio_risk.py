# portfolio_risk.py
# Volatility-targeted portfolio risk budgeting: cap total exposure, correlation reduction,
# volatility regime and equity curve adjustments. Does not replace per-trade sizing; adjusts
# risk_pct before execution.

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple, Any

from trade_cache import get_active_trades
from trading_config import get_config
from trading_log import get_recent_equity_metrics

# Correlation groups for portfolio risk (include CADJPY with yen crosses)
CORRELATION_GROUPS = [
    {"name": "USD_MAJORS", "members": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]},
    {"name": "YEN_CROSSES", "members": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY"]},
]

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("_", "").replace("/", "")


def _pip_value_per_unit(instrument: str) -> float:
    """Pip value in price units (e.g. 0.0001 for EURUSD, 0.01 for USDJPY)."""
    s = _normalize_symbol(instrument)
    if s.endswith("JPY"):
        return 0.01
    if s == "XAUUSD":
        return 0.1
    if s == "XAGUSD":
        return 0.01
    return 0.0001


def _risk_per_trade_dollars(units: int, entry_price: float, sl_price: float, side: str, instrument: str) -> float:
    """Approximate risk in account currency (USD) for one trade.
    risk = units * |entry - sl| * pip_value_per_unit * (units per standard lot factor).
    For standard lots: 1 pip move = pip_value_per_unit * 100000 for non-JPY, 100000 for JPY (1 unit = 1 unit of base).
    Simplified: dollar risk ≈ |units| * |entry - sl| for quote=USD pairs; for JPY quote we need contract size.
    OANDA: 1 unit = 1 unit of base currency. So for EUR_USD, 1 unit = 1 EUR, risk in USD = units * (entry-sl) in USD per unit = units * (entry - sl) for buy.
    So risk_dollars = abs(units) * abs(entry_price - sl_price) when quote is USD. When base is USD (e.g. USD_JPY), 1 unit = 1 USD, so risk in USD = units * abs(sl - entry) in JPY, but we want USD: move in JPY * units / exchange_rate. Actually for USDJPY, price is in JPY per USD, so move in price * units = move in JPY for the position; in USD that's (move in JPY) / price. So risk_usd = abs(units) * abs(entry - sl) / entry for USD_JPY (approx). Keep it simple: for most pairs risk ≈ abs(units) * abs(entry - sl) when quote is USD; for USD_JPY risk ≈ abs(units) * abs(entry - sl) / entry (price in JPY per 1 USD). We don't have account currency here; assume USD. So:
    - EUR_USD: risk = units * (entry - sl) in USD.
    - USD_JPY: risk = units * |entry - sl| but that's in JPY; in USD = units * |entry - sl| / entry.
    """
    try:
        units = abs(int(units))
        if units == 0:
            return 0.0
        dist = abs(float(entry_price) - float(sl_price))
        if dist <= 0:
            return 0.0
        s = _normalize_symbol(instrument)
        # Quote is USD (e.g. EUR_USD, GBP_USD): risk in USD = units * distance (price is in USD per unit of base)
        if s.endswith("USD") and not s.startswith("USD"):
            return units * dist
        # USD_JPY: price is JPY per USD, so distance in JPY; risk in USD = units * distance / current_price
        if s.startswith("USD") and "JPY" in s:
            return units * dist / max(entry_price, 1e-9)
        # Crosses like EUR_JPY: units in base (EUR), distance in JPY; approximate as units * distance / entry (JPY per EUR)
        if "JPY" in s:
            return units * dist / max(entry_price, 1e-9)
        # Default: treat as USD-quoted
        return units * dist
    except Exception:
        return 0.0


def get_open_trades_for_account(account_id: Optional[str]) -> List[Dict]:
    """Return active trades from cache, optionally filtered by account_id."""
    active = get_active_trades()
    if not account_id:
        return active
    return [t for t in active if str(t.get("account_id", "")) == str(account_id)]


def calculate_portfolio_risk(
    balance: float,
    account_id: Optional[str] = None,
) -> Tuple[float, List[Dict]]:
    """
    Compute total portfolio risk as a percentage of account balance.
    Uses cached open trades with position_size and sl_price when available.
    Returns (portfolio_risk_pct, list of per-trade risk info dicts).
    """
    if balance is None or balance <= 0:
        return 0.0, []
    open_trades = get_open_trades_for_account(account_id)
    total_risk_usd = 0.0
    details = []
    for t in open_trades:
        units = t.get("position_size") or t.get("units")
        sl_price = t.get("sl_price")
        entry_price = t.get("entry_price")
        sym_raw = t.get("instrument") or t.get("symbol") or ""
        instrument = sym_raw
        if sym_raw and "_" not in str(sym_raw) and len(str(sym_raw)) == 6:
            instrument = f"{str(sym_raw)[:3]}_{str(sym_raw)[3:]}"
        if not instrument or units is None or sl_price is None or entry_price is None:
            continue
        try:
            u = int(units) if not isinstance(units, int) else units
        except (TypeError, ValueError):
            continue
        side = (t.get("side") or t.get("direction") or "buy").lower()
        risk_usd = _risk_per_trade_dollars(u, float(entry_price), float(sl_price), side, instrument)
        total_risk_usd += risk_usd
        details.append({
            "symbol": t.get("symbol"),
            "instrument": instrument,
            "risk_usd": risk_usd,
            "units": u,
        })
    if balance <= 0:
        return 0.0, details
    portfolio_risk_pct = (total_risk_usd / float(balance)) * 100.0
    return portfolio_risk_pct, details


def get_correlation_group(symbol: str) -> Optional[str]:
    """Return correlation group name if symbol belongs to one."""
    sym = _normalize_symbol(symbol)
    for g in CORRELATION_GROUPS:
        if sym in g["members"]:
            return g["name"]
    return None


def get_correlated_risk_pct(balance: float, open_risk_details: List[Dict], new_symbol: str) -> float:
    """Return portfolio risk % that comes from open trades in the same correlation group as new_symbol."""
    if not balance or balance <= 0 or not open_risk_details:
        return 0.0
    group = get_correlation_group(new_symbol)
    if not group:
        return 0.0
    members = set()
    for g in CORRELATION_GROUPS:
        if g["name"] == group:
            members = set(g["members"])
            break
    correlated_risk_usd = sum(
        d["risk_usd"] for d in open_risk_details
        if _normalize_symbol((d.get("symbol") or d.get("instrument") or "")) in members
    )
    return (correlated_risk_usd / float(balance)) * 100.0


def adjust_risk_for_portfolio(
    risk_pct: float,
    symbol: str,
    balance: float,
    account_id: Optional[str] = None,
    atr_pct: Optional[float] = None,
    sl_price: Optional[float] = None,
    entry_price: Optional[float] = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """
    Adjust risk_pct for portfolio cap, correlation, volatility regime, and equity curve.
    Config values are in percent (e.g. 0.25, 1.2, 3.0); we use fractions internally (0.0025, 0.012, 0.03).
    Returns (adjusted_risk_pct, adjustments_dict). If trade should be skipped, returns (None, dict).
    """
    cfg = get_config().portfolio_risk
    min_frac = cfg.min_risk_per_trade / 100.0
    max_frac = cfg.max_risk_per_trade / 100.0
    cap_frac = cfg.max_portfolio_risk / 100.0
    adjustments = {
        "original_risk_pct": risk_pct * 100.0,
        "portfolio_risk_before_pct": 0.0,
        "portfolio_risk_after_pct": 0.0,
        "portfolio_cap_reduction": 0.0,
        "correlation_reduction": 0.0,
        "volatility_adjustment": 0.0,
        "equity_adjustment": 0.0,
        "skipped_reason": None,
    }
    if balance is None or balance <= 0:
        adjustments["skipped_reason"] = "invalid_balance"
        return None, adjustments

    # Current portfolio risk
    portfolio_risk_pct, open_details = calculate_portfolio_risk(balance, account_id)
    adjustments["portfolio_risk_before_pct"] = portfolio_risk_pct

    # 1) Portfolio cap: new trade would add risk_pct (as % of balance). Total must not exceed cap.
    headroom_pct = max(0.0, (cfg.max_portfolio_risk - portfolio_risk_pct))
    risk_pct_as_pct = risk_pct * 100.0
    if risk_pct_as_pct > headroom_pct:
        # Reduce to fit under cap
        reduction = risk_pct_as_pct - headroom_pct
        adjustments["portfolio_cap_reduction"] = reduction
        risk_pct_as_pct = headroom_pct
    risk_pct = risk_pct_as_pct / 100.0

    if risk_pct < min_frac:
        adjustments["skipped_reason"] = "portfolio_cap_insufficient_headroom"
        adjustments["adjusted_risk_pct"] = risk_pct * 100.0
        return None, adjustments

    # 2) Correlation: if correlated exposure already >= 50% of portfolio risk, skip
    correlated_pct = get_correlated_risk_pct(balance, open_details, symbol)
    total_open_pct = portfolio_risk_pct
    if total_open_pct > 0 and (correlated_pct / total_open_pct) >= cfg.correlation_exposure_skip_threshold:
        adjustments["skipped_reason"] = "correlation_exposure_above_threshold"
        adjustments["correlated_risk_pct"] = correlated_pct
        return None, adjustments
    # If we have correlated open trades, reduce new trade risk by 30-50%
    if correlated_pct > 0 and get_correlation_group(symbol):
        mult = 1.0 - cfg.correlation_risk_reduction
        risk_pct *= mult
        adjustments["correlation_reduction"] = (1.0 - mult) * 100.0
        adjustments["correlated_risk_pct"] = correlated_pct

    if risk_pct < min_frac:
        adjustments["skipped_reason"] = "after_correlation_below_min"
        adjustments["adjusted_risk_pct"] = risk_pct * 100.0
        return None, adjustments

    # 3) Volatility regime (ATR%)
    if atr_pct is not None:
        if atr_pct >= 2.5:  # Unusually high
            risk_pct *= (1.0 - cfg.volatility_high_reduction)
            adjustments["volatility_adjustment"] = -cfg.volatility_high_reduction * 100.0
        elif atr_pct <= 0.4:  # Unusually low
            risk_pct *= (1.0 + cfg.volatility_low_increase)
            adjustments["volatility_adjustment"] = cfg.volatility_low_increase * 100.0

    if risk_pct < min_frac:
        adjustments["skipped_reason"] = "after_volatility_below_min"
        adjustments["adjusted_risk_pct"] = risk_pct * 100.0
        return None, adjustments

    # 4) Equity curve
    n = cfg.last_n_trades_equity
    equity = get_recent_equity_metrics(n)
    if equity["n_trades"] >= max(1, n // 2):
        if equity["recent_drawdown_pct"] >= cfg.drawdown_threshold_pct:
            risk_pct *= (1.0 - cfg.drawdown_reduction)
            adjustments["equity_adjustment"] = -cfg.drawdown_reduction * 100.0
            adjustments["recent_drawdown_pct"] = equity["recent_drawdown_pct"]
        elif equity["win_rate_pct"] >= cfg.winrate_threshold:
            risk_pct *= (1.0 + cfg.winrate_increase)
            adjustments["equity_adjustment"] = cfg.winrate_increase * 100.0
            adjustments["last_10_win_rate_pct"] = equity["win_rate_pct"]

    # Clamp to floor/ceiling
    risk_pct = max(min_frac, min(max_frac, risk_pct))
    adjustments["portfolio_risk_after_pct"] = portfolio_risk_pct + (risk_pct * 100.0)
    adjustments["adjusted_risk_pct"] = risk_pct * 100.0
    return risk_pct, adjustments
