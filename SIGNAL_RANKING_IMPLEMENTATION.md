# Signal Ranking Engine – Implementation Summary

## 1. Ranking Algorithm Explanation

The **Signal Ranking Engine** assigns a composite **ranking_score** (0–100) to each opportunity so the system can **execute the highest-quality trades first** when multiple signals appear in one scan.

### Formula

```
ranking_score =
  0.40 × base_score           (existing opportunity.score 0–100)
+ 0.20 × trend_strength       (0–100: aligned=100, neutral=50, opposite=0)
+ 0.15 × momentum             (0–100 from opportunity.momentum.strength)
+ 0.10 × volatility_quality   (0–100: moderate ATR% best, extremes penalized)
+ 0.10 × confirmations        (0–100 from number of reasons, cap at 8)
+ 0.05 × session_score        (0–100 from session_strength, e.g. London/NY)
+ 0.10 × risk_reward_score    (0–100 from suggested R:R; 2R=100, 1.5R=75, 1R=50)
```

All inputs are normalized to 0–100 where applicable; the weighted sum is clamped to **0–100**.

### Risk allocation by ranking

After the smart planner and strategy scaling (SCALP/Tier-2), **risk_pct** is scaled by ranking:

- **ranking_score ≥ 85** → risk_pct × **1.2**
- **75 ≤ ranking_score < 85** → risk_pct × **1.0**
- **65 ≤ ranking_score < 75** → risk_pct × **0.8**
- **&lt; 65** → risk_pct × **0.8**

The result is still passed through the **Portfolio Risk Engine**, which enforces the portfolio cap and per-trade floor/ceiling (e.g. 0.25%–1.2%), so effective risk remains within existing limits.

### Correlation-aware selection

Opportunities are **sorted by ranking_score descending**. When several signals belong to the same correlation group (e.g. USD_MAJORS, YEN_CROSSES), the **highest-ranked** one is tried first. Execution continues in rank order until session/concurrent/portfolio limits are hit. The existing portfolio risk engine continues to skip or reduce when correlation exposure or portfolio cap is exceeded.

---

## 2. Files Modified

| File | Changes |
|------|--------|
| **signal_ranking.py** (new) | `compute_ranking_score()`, `rank_and_sort_opportunities()`, `get_risk_multiplier_by_ranking()`, `log_ranking_decision()`. Correlation groups and helpers. |
| **enhanced_main.py** | After `_filter_opportunities_for_user`, call `rank_and_sort_opportunities()`; iterate over **ranked** list (best first). Pass `ranking_score` and `ranking_components` into `_execute_opportunity_for_user`. Apply ranking risk multiplier after strategy scaling; add `ranking_score` and `ranking_components` to `meta_dict`; pass `ranking_score` and `strategy_id` into `add_trade()`. Call `log_ranking_decision()` when a trade is executed. Stop when `user_trades_executed >= max_new_for_user` or session/global caps. Log “not executed” when stopping due to limits. |
| **automated_trader.py** | When logging a closed trade via `add_log_entry()`, include `ranking_score` and `strategy_id` from the cached trade for performance tracking. |
| **trade_cache.py** | No code change; `add_trade(..., **additional_data)` already stores `ranking_score` and `strategy_id`. |

---

## 3. Example Ranking Output for a Scan Cycle

```
[ENHANCED] 📊 User abc-123: Ranked 4 opportunities by signal quality (top first)
[ENHANCED]   1. EURUSD BUY ranking_score=88.2 (base=72.0)
[ENHANCED]   2. GBPUSD BUY ranking_score=82.5 (base=68.0)
[ENHANCED]   3. USDJPY SELL ranking_score=75.1 (base=65.0)
[ENHANCED]   4. AUDUSD BUY ranking_score=71.0 (base=64.0)
[ENHANCED] 🎯 User abc-123: 4 opportunities available (user has 2/7 positions, capacity for 5 new trades)

[ENHANCED] 🎯 User abc-123: Processing opportunity 1/4 (ranking_score=88.2, base=72.0)
...
[ENHANCED] 📊 Ranking risk scaling: ranking_score=88.2 → risk_pct × 1.20
[RANKING] Selected EURUSD BUY (ranking_score=88.2, base_score=72.0). higher ranking score (trend, momentum, volatility, session, R:R).
[RANKING]   - Skipped GBPUSD BUY (ranking_score=82.5)
[RANKING]   - Skipped USDJPY SELL (ranking_score=75.1)
[RANKING]   - Skipped AUDUSD BUY (ranking_score=71.0)
[ENHANCED] ✅ Trade executed (session total: 3/7, strategy=4H_MAIN, tier2=False)
```

Execution order is **EURUSD → GBPUSD → USDJPY → AUDUSD** until limits are reached.

---

## 4. Portfolio Risk Logic Unchanged

- **Portfolio Risk Engine** still runs **after** the smart planner and **after** ranking-based risk scaling. It still:
  - Enforces the max portfolio risk cap (e.g. 3%).
  - Applies correlation exposure skip/reduction.
  - Applies volatility and equity-curve adjustments.
  - Clamps per-trade risk to the configured floor/ceiling (e.g. 0.25%–1.2%).
- Ranking only changes **order of execution** (best first) and **input risk_pct** (×1.2 / 1.0 / 0.8). The portfolio engine continues to **adjust or skip** using the same rules, so portfolio-level risk limits remain intact.

---

## 5. How Ranking Improves Trade Selection

1. **Best-first execution**  
   All opportunities from the scan are **collected**, then **ranked** by composite quality. The bot no longer executes in the arbitrary order returned by the scanner; it always tries the **highest-ranking** opportunity first, then the next, and so on until limits are hit. So when several signals appear in one cycle, the strongest one (by trend, momentum, volatility, confirmations, session, and R:R) is executed first.

2. **Better use of capacity**  
   With a cap on concurrent trades and session size, using rank order ensures that the first N filled slots are the **best N** among the current set, instead of whatever came first in the list.

3. **Correlation-aware priority**  
   Within a correlation group (e.g. EURUSD and GBPUSD), the **highest-ranked** symbol is tried first. The portfolio engine still blocks or reduces when correlated exposure or portfolio cap would be exceeded, so correlation and cap limits stay in place while improving which correlated trade is preferred.

4. **Risk scaled by quality**  
   Higher-ranked trades (≥85) get a 1.2× risk scaling; lower-ranked (65–74) get 0.8×. This slightly increases size on the best signals and slightly reduces it on weaker ones, within the existing portfolio and per-trade limits.

5. **Performance tracking**  
   Each executed trade stores **ranking_score** and **strategy_id** in the cache and in the trade log (and in meta for downstream use). When trades are closed, the log entry can include **ranking_score**, **strategy_id**, and (where available) **profit/loss** and **R-multiple**, so you can later analyze which ranking scores and strategies perform best.

---

## Execution Flow (Updated)

```
Scanner (get_market_opportunities)
  → General filter (_filter_opportunities_general)
  → Per-user filter (_filter_opportunities_for_user)
  → Opportunity collection (all opportunities for user available)
  → Signal Ranking Engine (rank_and_sort_opportunities)
  → Sorted list: execute in order (best first)
  → For each opportunity in rank order:
        Strategy classification (4H_MAIN / SCALP, tier2)
        Guardrails, rechecks, H4 candle confirm
        _execute_opportunity_for_user(..., ranking_score, ranking_components)
          → Smart Planner (plan_trade)
          → Strategy scaling (SCALP / tier2)
          → Ranking risk scaling (×1.2 / 1.0 / 0.8)
          → Portfolio Risk Engine (adjust_risk_for_portfolio)
          → Multi-entry planner (if high quality)
          → Execution (place_trade)
          → add_trade(..., ranking_score, strategy_id)
        log_ranking_decision(...)
  → Stop when session cap, user capacity, or concurrent limit reached
```

No existing strategy logic, risk management, or execution flow has been removed; the ranking layer is added between **opportunity collection** and **strategy/execution**.
