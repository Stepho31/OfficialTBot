# Autonomous Trading Bot Overview

## Entry Points & Scheduling
- `start_automated_trading.py` loads environment variables, confirms dependencies, and then instantiates `automated_trader.AutomatedTrader`. This is the primary entry point for continuous operation and is responsible for switching `DRY_RUN` off before automation.
- `AutomatedTrader.start_automation()` (in `automated_trader.py`) owns the long-running loop. It wires three `schedule` timers: a weekly Sunday 23:00 report (`generate_weekly_report`), a daily 09:00 catch-up (`ensure_weekly_report_sent`), and an hourly `health_check`. Between `schedule.run_pending()` calls it sleeps in 30-minute blocks during “favorable” sessions and one-hour blocks otherwise, emitting a quick heartbeat every ten minutes.
- `run_loop.py` wraps the legacy `main.py` pipeline in an endless four-hour cadence suitable for cron-style hosting. `run_once.py` executes the same idea flow a single time for manual triggers.
- `monitor_loop.py` is an optional helper that repeatedly calls `monitor.monitor_open_trades()` once per minute when trade monitoring is run as a standalone worker.
- `keep_alive.py` provides a lightweight Flask server used by hosted environments (e.g., Replit/UptimeRobot) to prove liveness.

## Core Loops (Decision → Execution → Persistence)

### Automated 30-minute cycle — `AutomatedTrader.execute_automated_trading_cycle()`
- **Decision**: loads live brokerage state with `TradesList` (detects manually closed trades via `detect_manual_trades`) and reads `trade_cache.get_active_trades()` / `automated_state.json` to determine free slots.
- **Execution**: when capacity exists it runs `enhanced_main.main()` (see below) to scan markets and optionally place new positions. Newly opened trades are tracked in-memory (`state.active_pairs`) and have background monitoring threads launched via `start_trade_monitoring`.
- **Persistence**: writes `automated_state.json` after each cycle to store active pairs, trade counts, and last scan timestamp; monitoring threads update `active_trades.json` and `trading_log.json` as trades evolve.

### Opportunity evaluation — `EnhancedTradingSession.execute_trading_session()` (in `enhanced_main.py`)
- **Decision pipeline**:
  1. `market_scanner.get_market_opportunities()` queries OANDA candles (`InstrumentsCandles`), account instruments, and real-time spreads (`PricingInfo`) to build scored `MarketOpportunity` objects. Sentiment adjustments call TwelveData and cached metrics in `market_sentiment.py`.
  2. `_filter_opportunities()` removes low-score, duplicate, or high-correlation setups based on `trade_cache` and real-time spread limits derived from `trading_config`.
  3. For each candidate the bot re-validates via `idea_guard.evaluate_trade_gate()` (cooldowns & registry), `validators.validate_entry_conditions()` across H4/H1/M15 data, and `validators.passes_h4_hard_filters()`.
- **Execution**:
  - `smart_layer.plan_trade()` converts validation signals into a risk plan (dynamic risk %, ATR exits), and `_get_live_spread_pips()` fetches live quotes.
  - `trader.place_trade()` performs final risk checks (market hours, news blackout, correlation) and submits an OANDA market order (`orders.OrderCreate`) with bracket TP/SL. It logs pricing telemetry and sizing decisions.
- **Persistence / fan-out**:
  - `trade_cache.add_trade()` writes to `active_trades.json` (idempotent on trade_id + pair).
  - `idea_guard.record_executed_idea()` appends to `idea_registry.json` for future de-duplication.
  - Notifications are emitted through `email_utils.send_email()` and optionally `signal_broadcast.send_signal()` (Stripe-backed tier list & SMTP).
  - `monitor.start_trade_monitoring()` (when automation is active) spawns a watcher thread for downstream persistence (see next loop).

### Trade monitoring — `monitor.monitor_trade()`
- **Decision**: poll OANDA trade state using `TradeDetails` and live prices (`pricing.PricingInfo`). The loop maintains running profit in pips, checks ATR-based trail triggers, and enforces partial-profit milestones.
- **Execution**: trailing-stop updates call `TradeCRCDO`, partial closes use `TradeClose`, and full exits rely on broker TP/SL or external closure detected by zero units. Meta parameters from `smart_layer` drive break-even and trailing logic.
- **Persistence**: when a trade exits, `trade_cache.remove_trade()` updates `active_trades.json` and `trading_log.add_log_entry()` writes the result to `trading_log.json`. Weekly snapshots later consume this log for reporting.

### Legacy GPT idea loop — `main.py`
- **Decision**: `scraper.get_trade_ideas()` (Playwright) pulls TradingView content, `filters.rule_based_filter()` and `idea_guard.filter_fresh_ideas_by_registry()` curate candidates, and `gpt_utils.evaluate_top_ideas()` (OpenAI chat completions with `gpt_cache.json`) selects a top idea. Multiple risk gates (daily loss, consecutive losses, exposure limits) run on log + cache data.
- **Execution**: the chosen idea is validated via `validators.validate_entry_conditions()`, planned with `smart_layer.plan_trade()`, and executed with `trader.place_trade()` similar to the enhanced flow.
- **Persistence**: identical to the enhanced session—trade cache, idea registry, monitor loop, and trading log.

## OANDA Integration Points
- Authentication everywhere uses `oandapyV20.API(access_token=os.environ['OANDA_API_KEY'], environment='live')` plus a single account id (`OANDA_ACCOUNT_ID`). Multi-tenant support is not present; all functions read the same env variables.
- Market data: `market_scanner`, `validators`, and `idea_guard` call `instruments.InstrumentsCandles`, `pricing.PricingInfo`, and `AccountInstruments`. Spread checks and ATR/EMA calculations depend on these endpoints.
- Trade lifecycle: `trader.place_trade()` sends `orders.OrderCreate` requests with attached TP/SL, then reads `AccountDetails` for balance and monitors fills. `monitor.monitor_trade()` drives `TradeDetails`, `TradeCRCDO`, and `TradeClose` for lifecycle management. `AutomatedTrader.detect_manual_trades()` queries `TradesList` to reconcile local caches.

## External & Supporting Integrations
- **OpenAI** (`gpt_utils.py`): `OpenAI(api_key=OPENAI_API_KEY)` for chat completions used by the legacy idea flow.
- **TradingView scraping** (`scraper.py`): Playwright automated browser sessions to https://www.tradingview.com/ideas/forex/ with retry/backoff logic.
- **Twelve Data & sentiment** (`validators.py`, `market_sentiment.py`): HTTPS requests to indicator and time-series endpoints using `TWELVE_DATA_API_KEY`; falls back to heuristics when unavailable. Optional `FRED_API_KEY` is reserved for bond data.
- **Stripe** (`signal_broadcast.py`, `stripe_server.py`, `api/stripe/*`): reads/writes customer metadata (grant/revoke Tier 1 access) and handles checkout/webhook flows. The broadcaster uses `stripe.Customer.list` to resolve email recipients.
- **Email / notifications** (`email_utils.py`): SMTP over Gmail credentials (`EMAIL_USER`/`EMAIL_PASS`) with body-hash deduplication. `signal_broadcast.send_signal()` fans out structured notifications to Stripe+admin recipients.
- **News blackout** (`news_filter.py`): reads `news_events.json` to block trades near high-impact events when `ENABLE_NEWS_BLACKOUT=true`.

## Idempotency & Duplicate Guards
- `trade_cache.add_trade()` refuses duplicates by `trade_id` and symbol+direction, keeping `active_trades.json` consistent even if `place_trade` is retried.
- `idea_guard.evaluate_trade_gate()` and `record_executed_idea()` maintain `idea_registry.json`, enforcing cooldowns by time, ATR move, and textual similarity to avoid replaying ideas.
- `gpt_utils.gpt_cache` hashes idea sets to reuse GPT responses and prevents redundant billable calls.
- `signal_broadcast._SENT_IDS` (process-level) and `email_utils` (recipient+subject body hashes) suppress repeated notifications.
- Weekly reports persist snapshots to `weekly_snapshots/` so catch-up jobs (`ensure_weekly_report_sent`) can detect already-sent weeks.

## Error Handling, Retry, and Termination
- Outer loops catch exceptions: `AutomatedTrader.start_automation()` traps `KeyboardInterrupt` for graceful shutdown (`stop_automation`) and calls `emergency_shutdown()` on unhandled exceptions to persist state and send alerts.
- Network-heavy modules (`scraper.get_trade_ideas`, `market_scanner`, `market_sentiment`) wrap API calls in try/except, provide fallbacks, and use incremental backoff (e.g., Playwright `goto_with_retry`).
- `monitor.monitor_trade()` continues on transient API errors by sleeping 30 seconds and retrying; it detects externally closed trades via `TradeDetails` and ensures the cache is cleaned.
- `start_automated_trading.py` validates env vars & dependencies before launching, preventing half-configured runs.
- Persistence writes (`trade_cache`, `trading_log`, `automated_state`) use temp-file swaps (`os.replace`) where possible to avoid partial files.
- Termination: `AutomatedTrader.stop_automation()` waits for monitor threads up to 30s, saves state, and sends a shutdown email. Manual loops (`run_loop.py`, `monitor_loop.py`) simply print errors and continue sleeping, relying on the operator to intervene.
