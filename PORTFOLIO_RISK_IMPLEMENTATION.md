# Portfolio Risk Management Implementation

## 1. Summary of New Risk Architecture

A **volatility-targeted portfolio risk budgeting** layer has been added **on top of** the existing per-trade sizing. It does not replace `trader.place_trade()` or `smart_layer.plan_trade()`; it adjusts `risk_pct` before execution and can skip trades when limits are breached.

- **Portfolio Risk Engine** runs after the Smart Planner and before the Multi-Entry Planner. It:
  - Enforces a **maximum total portfolio risk cap** (e.g. 3% of balance).
  - **Reduces or skips** new trades when adding them would exceed the cap or when correlation/drawdown rules are violated.
  - Applies **volatility regime** and **equity curve** adjustments (reduce risk in high vol or drawdown; increase when recent win rate is strong).
- **Existing sizing logic is unchanged**: `place_trade()` still uses balance, ATR, score-based `risk_pct`, and user `trade_allocation` as before. The engine only changes the `risk_pct` (and skip decision) passed into that flow.

---

## 2. Portfolio Risk Calculations

### 2.1 Total portfolio risk (`calculate_portfolio_risk`)

- **Inputs**: Account `balance`, optional `account_id` (to filter cached trades by account).
- **Data source**: Cached open trades in `trade_cache` (active_trades.json). Each trade should have `position_size`, `sl_price`, `entry_price`, and `instrument`/`symbol` (stored when the trade is added after execution).
- **Per-trade risk (USD)**:
  - For USD-quoted pairs (e.g. EUR_USD): `risk_usd = |units| × |entry − sl|`.
  - For JPY-quoted (e.g. USD_JPY, EUR_JPY): `risk_usd = |units| × |entry − sl| / entry` (convert JPY risk to USD).
- **Total portfolio risk %** = `(sum of risk_usd across open trades for the account) / balance × 100`.

### 2.2 Portfolio cap and headroom

- **Max portfolio risk** is set in config (e.g. 3%).
- **Headroom** = `max(0, max_portfolio_risk − current_portfolio_risk_pct)`.
- If the **new trade’s risk_pct** (as % of balance) would exceed headroom, it is reduced so that `portfolio_risk_after ≤ max_portfolio_risk`. If the reduced value would be below the **minimum risk per trade** (e.g. 0.25%), the trade is **skipped**.

### 2.3 Correlation risk reduction

- **Correlation groups** (e.g. USD_MAJORS, YEN_CROSSES including CADJPY) are defined in `portfolio_risk.CORRELATION_GROUPS`.
- **Correlated exposure** = sum of `risk_usd` of open trades in the same group as the new symbol.
- If **correlated exposure already ≥ 50% of total portfolio risk** (configurable threshold), the new trade is **skipped**.
- If there are correlated open trades but below that threshold, the **new trade’s risk_pct is reduced by 30–50%** (configurable `correlation_risk_reduction`).

### 2.4 Volatility regime

- Uses **ATR%** from the Smart Planner (symbol’s H4 ATR%).
- **Unusually high** (e.g. ATR% ≥ 2.5): reduce `risk_pct` by 30%.
- **Unusually low** (e.g. ATR% ≤ 0.4): increase `risk_pct` by 10–20% (configurable).

### 2.5 Equity curve protection

- **Last N trades** (e.g. 10) from the trading log are used.
- **Win rate** = % of those trades with positive pips/profit.
- **Drawdown** = from cumulative PnL over those trades: `(peak − current) / peak × 100` when below peak.
- If **drawdown > 5%**: reduce `risk_pct` by 40%.
- If **win rate over last 10 > 60%**: increase `risk_pct` by 15%.

### 2.6 Risk floor and ceiling

- **Minimum risk per trade** = 0.25% (configurable).
- **Maximum risk per trade** = 1.2% (configurable).
- **Portfolio risk cap** = 3% (configurable).
- After all adjustments, `risk_pct` is clamped to `[min_risk_per_trade, max_risk_per_trade]` (in fraction form internally).

---

## 3. Files Modified

| File | Changes |
|------|--------|
| **trading_config.py** | Added `PortfolioRisk` dataclass (min/max risk per trade, max portfolio risk, correlation/volatility/drawdown/win-rate parameters). Wired into `TradingConfig` and env overrides: `PORTFOLIO_MIN_RISK_PCT`, `PORTFOLIO_MAX_RISK_PCT`, `MAX_PORTFOLIO_RISK_PCT`. |
| **portfolio_risk.py** (new) | Implements `calculate_portfolio_risk()`, `get_correlated_risk_pct()`, `adjust_risk_for_portfolio()`. Correlation groups, risk-per-trade USD logic, and all adjustments (cap, correlation, volatility, equity) with skip and logging info. |
| **trading_log.py** | Added `get_last_n_trades(n)` and `get_recent_equity_metrics(n)` for win rate and drawdown over the last N closed trades. |
| **enhanced_main.py** | After strategy scaling (SCALP/Tier-2), fetches account balance; calls `adjust_risk_for_portfolio()`. If result is skip, returns without executing. Otherwise uses adjusted `risk_pct` for multi-entry/single leg. Logs original/adjusted risk_pct, portfolio risk before/after, and correlation/cap/volatility/equity adjustments. When adding trades to cache, passes `position_size` and `sl_price` (total units for multi-entry) so portfolio risk can be computed. Same for the legacy single-trade path. |
| **trade_cache.py** | No code change; `add_trade(..., **additional_data)` already accepts and stores `position_size` and `sl_price`. |

---

## 4. Example Scenario: Risk Adjustment

- **Account balance**: $10,000.  
- **Existing open trades**: 1 trade (USDJPY), total portfolio risk = 1.2%.  
- **New trade**: Smart Planner outputs `risk_pct = 1.0%`; symbol EURUSD; ATR% = 0.35 (low); last 10 trades win rate = 65%.  
- **Max portfolio risk** = 3%.

1. **Portfolio cap**: Headroom = 3 − 1.2 = 1.8%. New trade 1% fits; no cap reduction.
2. **Correlation**: No open trade in USD_MAJORS (EURUSD’s group); no correlation reduction.
3. **Volatility**: ATR% low → increase risk by 15% → 1.0 × 1.15 = 1.15%.
4. **Equity**: Win rate > 60% → increase by 15% → 1.15 × 1.15 ≈ 1.32%.
5. **Clamp**: 1.32% is above max 1.2% → **adjusted_risk_pct = 1.2%**.
6. **Portfolio after** = 1.2 + 1.2 = 2.4% (under 3%).

Logs would show: `original_risk_pct=1.0` → `adjusted_risk_pct=1.2`, `portfolio_before=1.2` → `portfolio_after=2.4`, `volatility_adjustment=+15%`, `equity_adjustment=+15%`.

**Skip example**: If the same account had 2.8% portfolio risk already, headroom = 0.2%. The engine would reduce the new trade to 0.2%; if min risk per trade is 0.25%, the trade would be **skipped** (`skipped_reason=portfolio_cap_insufficient_headroom`).

---

## 5. Confirmation: Existing Sizing Logic Unchanged

- **`smart_layer.plan_trade()`**: Still computes quality score and base `risk_pct` from volatility and score. Not modified.
- **`trader.place_trade()`**: Still uses `risk_pct` (or user `trade_allocation`), balance, ATR, and instrument to compute units; still applies min/max position size and existing validation. Not modified.
- **Multi-entry and strategy scaling**: Legs are still built from (possibly adjusted) `risk_pct`; SCALP and Tier-2 scaling still apply before the portfolio risk engine.
- **Monitoring and DB logging**: Unchanged; only the numeric `risk_pct` (and the decision to skip) is affected before execution. Cache is extended with `position_size` and `sl_price` for portfolio risk calculation only.

The system remains stable: monitoring, execution, and database logging behave as before; the portfolio risk layer only adjusts or blocks the trade in the execution flow and adds transparent logging.
