# Paper Trader

Simulated day-trading platform with a live market data feed, a pluggable strategy
engine, and a post-trade education layer that explains every decision.

**This application never places a real order.** See `CLAUDE.md`.

## Documents

| File | What's in it |
|---|---|
| `BUILD_SPEC.md` | Core design: stack, architecture, schema, strategies, friction model, education layer, phases 1–8 |
| `ADDENDUM_LIVE_APPROVAL.md` | Two-role supervised live mode (requester / approver), phases 9–12 |
| `ADDENDUM_ROBINHOOD_AND_LEARNING.md` | Robinhood findings, Phase 2 platform path, learning layer, LLM tiering, phases 13–16 |
| `CLAUDE.md` | Non-negotiable rules. Claude Code reads this before every task. |

## Status

**Phase 1 (data spine) and Phase 2 (execution + risk) — scaffolded and passing.**
The full suite runs with no live database, no network, and no API keys — DB-backed
tests use an in-memory SQLite database instead of Postgres/Docker.

```
backend/app/config.py              paper-only safety rails, refuses to boot otherwise
backend/app/models/                SQLAlchemy: users, paper_accounts, bars, ingest_state,
                                    gap_events, orders, fills, positions, trades,
                                    risk_settings, risk_events
backend/app/db.py, deps.py         async engine/session, shared FastAPI dependencies
backend/alembic/                   migrations: 0001 (Phase 1 tables), 0002 (Phase 2 tables)
backend/app/ingest/bars.py         bar construction, finalization, gap detection
backend/app/ingest/subscriptions.py 30-symbol free-tier subscription manager
backend/app/ingest/stream.py       reconnect + full-jitter backoff + heartbeat watchdog
backend/app/ingest/replay.py       record/replay harness
backend/app/ingest/alpaca_source.py Alpaca websocket adapter
backend/app/execution/friction.py  the friction model (§9): spread, slippage, commission,
                                    SEC/FINRA fees, open/close penalty
backend/app/execution/broker.py    the Broker ABC (§7.5)
backend/app/execution/paper_broker.py PaperBroker — the only implementation
backend/app/execution/alpaca_trading_client.py real Alpaca paper adapter (async wrapper)
backend/app/execution/positions.py position/trade lifecycle: gross/net P&L, R-multiple
backend/app/execution/order_service.py manual-order orchestration: risk -> broker -> DB
backend/app/risk/sizing.py         fixed-fractional position sizing
backend/app/risk/engine.py         the risk engine: every veto from §7.4
backend/app/market_calendar.py     shared session-boundary helpers (calendar-naive)
backend/app/api/                   FastAPI: /health, /market/*, POST /orders
backend/tests/                     104 tests incl. hypothesis property tests and a full
                                    POST /orders integration test against SQLite
backend/tools/smoke_replay.py      end-to-end offline pipeline proof
backend/tools/record_day.py        record a real session for replay
```

**Known Phase 2 scope decisions** (see code comments for the full reasoning):
- `paper_accounts.cash`/`equity` are not yet mutated by trades — the daily-loss
  veto derives realized P&L directly from `trades` rows instead. Live
  mark-to-market equity needs the indicator/quote pipeline, which is Phase 3.
- The manual order endpoint takes its reference quote (bid/ask/ATR/typical
  volume) from the request body rather than a live source, since Phase 1 never
  wired ingest through to Postgres/Redis. A live snapshot provider arrives with
  Phase 3.
- `Position.entry_order_id` was added to the schema (not in BUILD_SPEC §5) so a
  closed trade's `total_friction` can include the entry leg's friction.

## Quick start

```bash
cd backend
pip install -e ".[dev]"

# 1. Verify the pipeline with zero setup
PYTHONPATH=. python tools/smoke_replay.py
PYTHONPATH=. python -m pytest -q

# 2. Bring up infrastructure
cd .. && docker compose up -d

# 3. Add credentials (paper account = email only, no funding)
cd backend && cp .env.example .env    # then fill in ALPACA_API_KEY / SECRET

# 4. Run migrations, then the API
PYTHONPATH=. alembic upgrade head
PYTHONPATH=. uvicorn app.main:app --reload
```

## Working with Claude Code

One phase per session. Start each with:

> Read `BUILD_SPEC.md` and `CLAUDE.md`. Implement Phase N. Do not start Phase N+1.
> Write the tests from the acceptance criteria first.

Do not scaffold all phases at once — understanding the result is the point.

## The three processes

| Process | Instances | Owns |
|---|---|---|
| `api` | many | HTTP + client websockets. Stateless. |
| `ingest` | **exactly one** | The Alpaca data socket and bar building. A second instance duplicates the connection and races. |
| `worker` | many | Strategy evaluation, execution, explanations, scheduled jobs. |

## Next up — Phase 3

Strategy engine: `Strategy` ABC, indicator pipeline (EMA/SMA/RSI/MACD/ATR/VWAP/etc.,
computed once per symbol per bar), all four starter strategies (ORB, VWAP reversion,
EMA crossover, RSI(2)), signal + evidence persistence (the `signals` table), and
bar-finalization gating so strategies only ever evaluate finalized bars.
