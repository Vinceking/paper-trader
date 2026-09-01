# Paper Trader — Build Specification

A simulated day-trading platform with a live market data feed, a pluggable strategy
engine, and a post-trade education layer that explains every decision.

**Status:** design spec, v1.0
**Target builder:** Claude Code (backend + logic), optionally Replit (web frontend prototype)
**Execution mode:** paper only — simulated fills, no real money, no broker order routing

---

## 0. Read this before you build anything

### 0.1 What this app is not

This is not a money-making machine, and building it as if it were is the single
most likely way to waste the effort. The design below deliberately makes some
things *harder* than a naive trading app would — a mandatory backtest gate before
a strategy can run, a friction model that makes simulated results worse, a
benchmark panel that constantly compares your results to doing nothing. Those are
not bugs. They are the parts that make it a learning instrument instead of a
slot machine with charts.

### 0.2 The numbers you are building against

Every serious study of retail day trading finds the same thing. These are the
actual published figures:

| Study | Sample | Finding |
|---|---|---|
| Barber & Odean, *Trading Is Hazardous to Your Wealth* (2000), J. Finance | 66,465 US individual investors, 1991–97 | Active traders underperformed by 86 bps/month (~10.3%/yr) |
| Barber, Lee, Liu & Odean, *The Cross-Section of Speculator Skill* (2014), J. Financial Markets | 450,000 Taiwanese day traders, 1992–2006 | 19% of heavy traders earned positive abnormal returns; **<1% consistently profitable** |
| Barber, Lee, Liu, Odean & Zhang, *Learning, Fast or Slow* (2019), RAPS | Taiwanese day traders, 1992–2006 | **97% lose money on any given day, net of fees** |
| Chague, De-Losso & Giovannetti, *Day Trading for a Living?* (2020) | 20,000 new Brazilian futures traders, 2012–17 | Of the 1,500 most active, **17 earned above minimum wage** after costs (1.1%) |
| Jordan & Diltz (2003), Financial Analysts Journal | 324 US day traders, 1998–99 | 64% lost money; 20% made over $5,000 |

The design goal of this app is to let you find out, at zero cost, which side of
those numbers your ideas land on — **before** anyone risks a dollar. If your
strategy can't beat buy-and-hold SPY in six months of paper trading with realistic
friction applied, that is a genuinely valuable finding, and it cost you nothing.

### 0.3 Account and legal reality

- **You cannot open your own brokerage account under 18** (21 in a few states).
  Contracts with minors aren't enforceable, so brokers won't do it.
- **A UGMA/UTMA custodial account is legally the custodian's account.** The adult
  custodian controls it and places the trades; the minor takes control at a
  state-set age between 18 and 25. Contributions are irrevocable. Custodial
  accounts generally don't get margin, which means no shorting and cash-settlement
  rules (T+1, no free-riding) apply.
- **The FINRA pattern day trader rule was eliminated on June 4, 2026.** The SEC
  approved FINRA amendments removing the PDT designation and the $25,000 minimum
  equity requirement; margin accounts now need only the standard $2,000 minimum.
  *(I told you earlier the $25k rule applied — that was wrong, and worth correcting.
  Note the direction of the change though: day trading just became far easier to
  start with a small account, which makes the outcome statistics in 0.2 more
  relevant, not less.)*
- **Alpaca paper accounts require none of this.** Per Alpaca's docs, anyone
  globally can create a paper-only account with just an email address — no funded
  or approved brokerage account. This is why the whole app is buildable and
  runnable by you, today, legitimately.

### 0.4 If this ever goes live

Out of scope for this build, but so the architecture doesn't paint you into a corner:

- A system that generates buy/sell recommendations for another person's money is
  regulated territory (investment advice). Personal use by the account owner is fine;
  handing signals to someone else to act on is a different thing legally.
- The `Broker` interface in §7.5 is deliberately abstract so a live driver *could*
  be added later by an adult with their own funded account. **Only the paper driver
  is implemented here.** Do not implement a live driver as part of this build.
- Gate on evidence, not enthusiasm: minimum 6 months of forward paper results,
  positive expectancy after friction, and outperformance vs. the SPY benchmark
  in §12. If those aren't all true, the answer is no.

---

## 1. Product definition

**One sentence:** a real-time paper trading app that runs your strategies against
live market data, executes simulated trades with realistic friction, and after
every single trade tells you exactly why it opened, why it closed, and what you
should learn from it.

### Core loop

```
live quotes  →  strategy evaluates  →  signal + evidence snapshot
                                            ↓
                              risk engine sizes / vetoes
                                            ↓
                        simulated fill (with friction applied)
                                            ↓
                          position managed → exit triggered
                                            ↓
              explanation generated from the RECORDED evidence
                                            ↓
                 journal card: why in, why out, one tip, benchmark delta
```

### Non-negotiable product rules

1. **Reasons are recorded at decision time, never reconstructed afterward.**
   See §11.1. This is the most important rule in the document.
2. **Friction is always on.** No "clean mode" toggle. See §9.
3. **Every P&L number is shown next to the SPY benchmark for the same period.**
   See §12.
4. **A strategy cannot go live-paper until it passes the backtest gate.** See §8.5.
5. **Manual orders have a 3-second hold-to-confirm.** Yes, you asked for a fast
   front end, and it is fast — but see §13.4 for why this specific friction stays.

---

## 2. Market data landscape (researched Aug 2026)

### 2.1 Comparison

| Provider | Free tier | Real-time? | WebSocket | Paid entry | Notes |
|---|---|---|---|---|---|
| **Alpaca** | $0 — 200 calls/min, 7+ yrs history | **Yes, via WebSocket (IEX only)**; REST is 15-min delayed | Yes, **30 symbol cap** on free | $99/mo Algo Trader Plus → full SIP, unlimited symbols, real-time OPRA options | **Best free real-time option.** Same vendor as the paper broker = one integration. |
| **Massive** (formerly Polygon.io — polygon.io now redirects to massive.com) | $0 — 5 calls/min, 2 yrs, **EOD only, no WebSocket** | Only on $199 tier | Starter+ ($29) | $29 Starter (15-min delayed) / $79 Developer / $199 Advanced (real-time) | Excellent data quality, real-time is out of budget |
| **Finnhub** | Generous free tier; quotes + news + sentiment | Limited real-time | Yes | Low | Good **secondary** source for news/sentiment |
| **Alpha Vantage** | Constrained free tier | Paid tiers | No | Low-mid | NASDAQ/OPRA licensed; fine for fundamentals |
| **Tiingo** | Limited | EOD-focused | Limited | Cheap | Clean historical EOD; not for intraday |
| **Twelve Data** | Limited | Paid | Yes | Mid | Built-in technical indicators |
| **Databento** | None (pay-as-you-go) | Yes, institutional | Yes | Usage-based | Overkill here |

### 2.2 Recommendation

**Primary: Alpaca free tier, for both data and paper execution.**

- One vendor, one set of credentials, one mental model.
- Free real-time WebSocket data is rare — most "free" APIs are 15-min delayed.
- Paper account needs only an email address.

**Secondary: Finnhub free tier**, for company news and sentiment, used as a
*context feature* in explanations (not as a trade trigger — news-reaction trading
on a delayed retail feed is a losing game against colocated systems).

### 2.3 The IEX limitation — this matters, build for it

Alpaca's free real-time WebSocket carries **IEX data only**, not the full SIP
consolidated tape. IEX is a single exchange with a low single-digit percentage of
US equity volume. Consequences you must handle in code:

- IEX quotes can differ from the national best bid/offer (NBBO).
- Thin or illiquid symbols may print sparsely or gap on IEX.
- Your simulated fill price is therefore *approximate*, biased optimistic.

**Mitigations (implement all three):**
1. Restrict the tradable universe to **high-liquidity large caps and major ETFs**
   (see §8.6), where IEX tracks NBBO closely.
2. Widen the assumed spread in the friction model (§9.2) to compensate.
3. Show a persistent, honest badge in the UI: `Data: IEX (partial tape)`.
   Do not hide this. It is exactly the kind of detail that separates a real
   understanding of markets from a toy.

### 2.4 The 30-symbol WebSocket cap

Free tier allows 30 streaming symbols. Design the ingest service with a
**subscription manager** that holds a bounded, prioritized watchlist and can
hot-swap symbols without dropping the socket. Don't hardcode a symbol list.

---

## 3. Recommended stack

### Backend
| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | The entire quant/TA ecosystem lives here |
| API framework | **FastAPI** | Async-native, native WebSocket support, auto OpenAPI docs |
| ASGI server | **uvicorn** (+ gunicorn workers in prod) | Standard |
| Database | **PostgreSQL 16** + **TimescaleDB** extension | Hypertables make OHLCV bar queries fast and compress well |
| Cache / pubsub | **Redis 7** | Latest-quote cache, cross-process fan-out to WebSocket clients |
| Task scheduling | **APScheduler** (in-process) or **Celery + Redis** if you outgrow it | Market open/close jobs, EOD reconciliation |
| Market data + paper broker | **alpaca-py** (official SDK) | Data stream + trading in one library |
| Technical indicators | **pandas-ta-classic** (maintained community fork; the original `pandas-ta` is unmaintained) | 250+ indicators, no C build step, unlike TA-Lib |
| Backtesting | **VectorBT** for fast parameter sweeps; **Backtrader** for event-driven realism | Use both — see §8.5 |
| Data handling | pandas, numpy | — |
| Validation | Pydantic v2 | Shared with FastAPI |
| Migrations | Alembic | — |
| Explanations | **Anthropic Python SDK** (Claude API) | §11 |
| Testing | pytest, pytest-asyncio, hypothesis | — |

### Frontend
| Concern | Choice | Why |
|---|---|---|
| Web (desktop) | **React 18 + TypeScript + Vite** | — |
| Mobile | **React Native via Expo** | Share TS types + API client with web; one codebase, both stores |
| Styling | **Tailwind CSS** (web) / **NativeWind** (mobile) | Same class vocabulary in both |
| Charts | **lightweight-charts** (TradingView, free, open source) | Purpose-built for financial charts; fast; works on both platforms |
| State | **TanStack Query** (server state) + **Zustand** (UI state) | Avoid Redux ceremony |
| Realtime | Native `WebSocket` with reconnect/backoff wrapper | — |
| Shared code | `/packages/shared` — TS types generated from the backend OpenAPI schema | Single source of truth for DTOs |

### Infrastructure
| Concern | Choice |
|---|---|
| Backend hosting | **Fly.io** or **Railway** (both handle long-lived WebSocket processes well) |
| Database | **Neon** or **Supabase** (managed Postgres; Timescale available on some tiers) |
| Redis | **Upstash** (free tier sufficient) |
| Web hosting | **Vercel** or **Cloudflare Pages** |
| Mobile distribution | **Expo EAS** (dev builds; TestFlight when ready) |
| Secrets | Platform env vars; `.env` locally, **never committed** |

---

## 4. System architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Alpaca (free tier)                        │
│    Market Data WS (IEX)          Paper Trading REST API          │
└───────────┬──────────────────────────────┬───────────────────────┘
            │                              │
            ▼                              ▼
   ┌────────────────┐            ┌──────────────────────┐
   │ ingest service │            │  execution service   │
   │ (long-running) │            │  (paper broker +     │
   │  - WS client   │            │   friction model)    │
   │  - reconnect   │            └──────────┬───────────┘
   │  - bar builder │                       │
   └───────┬────────┘                       │
           │                                │
      ┌────▼──────┐                         │
      │   Redis   │◄────────────────────────┤
      │ quotes +  │                         │
      │  pubsub   │                         │
      └────┬──────┘                         │
           │                                │
   ┌───────▼─────────┐             ┌────────▼─────────┐
   │ strategy engine │────signal──►│   risk engine    │
   │  (plugins)      │             │  sizing / veto   │
   │  + evidence     │             └────────┬─────────┘
   │    snapshot     │                      │
   └─────────────────┘                      │
                                            ▼
   ┌──────────────────┐            ┌──────────────────┐
   │ education service│◄───trade───│   PostgreSQL     │
   │  Claude API +    │   closed   │  + TimescaleDB   │
   │  tip selector    │            └──────────────────┘
   └────────┬─────────┘
            │
            ▼
   ┌──────────────────────────────────────────────┐
   │           FastAPI  (REST + WebSocket)        │
   └───────┬──────────────────────────┬───────────┘
           │                          │
      ┌────▼─────┐              ┌─────▼──────┐
      │  React   │              │React Native│
      │ (desktop)│              │  (mobile)  │
      └──────────┘              └────────────┘
```

### Process topology

Run as **three separate processes** — do not put the WebSocket consumer inside
the API server:

1. `api` — FastAPI, stateless, horizontally scalable
2. `ingest` — single instance, owns the Alpaca data socket (a second instance
   would duplicate the connection and race on bar building)
3. `worker` — strategy evaluation, execution, explanation generation, scheduled jobs

---

## 5. Data model

```sql
-- ============ users & config ============
CREATE TABLE users (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email           TEXT UNIQUE NOT NULL,
  display_name    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE paper_accounts (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  starting_cash   NUMERIC(18,2) NOT NULL DEFAULT 100000.00,
  cash            NUMERIC(18,2) NOT NULL,
  equity          NUMERIC(18,2) NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- benchmark anchor: SPY price when this account started
  benchmark_symbol TEXT NOT NULL DEFAULT 'SPY',
  benchmark_start_price NUMERIC(18,4)
);

-- ============ market data ============
CREATE TABLE bars (
  symbol          TEXT NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  timeframe       TEXT NOT NULL,           -- '1Min','5Min','15Min','1Day'
  open            NUMERIC(18,4) NOT NULL,
  high            NUMERIC(18,4) NOT NULL,
  low             NUMERIC(18,4) NOT NULL,
  close           NUMERIC(18,4) NOT NULL,
  volume          BIGINT NOT NULL,
  vwap            NUMERIC(18,4),
  trade_count     INTEGER,
  source          TEXT NOT NULL DEFAULT 'alpaca_iex',
  PRIMARY KEY (symbol, timeframe, ts)
);
SELECT create_hypertable('bars', 'ts', if_not_exists => TRUE);
CREATE INDEX ON bars (symbol, timeframe, ts DESC);

-- ============ strategies ============
CREATE TABLE strategies (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  slug            TEXT NOT NULL,           -- 'orb', 'vwap_reversion', ...
  name            TEXT NOT NULL,
  params          JSONB NOT NULL,
  enabled         BOOLEAN NOT NULL DEFAULT false,
  -- backtest gate (see §8.5): cannot enable until passed
  gate_passed     BOOLEAN NOT NULL DEFAULT false,
  gate_report_id  UUID,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, slug, name)
);

-- ============ signals: the evidence record ============
-- This table is the heart of the education layer. Written BEFORE any order.
CREATE TABLE signals (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id     UUID NOT NULL REFERENCES strategies(id),
  account_id      UUID NOT NULL REFERENCES paper_accounts(id),
  symbol          TEXT NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  side            TEXT NOT NULL,           -- 'buy' | 'sell'
  intent          TEXT NOT NULL,           -- 'entry' | 'exit'
  rule_id         TEXT NOT NULL,           -- e.g. 'orb.breakout_long'
  rule_text       TEXT NOT NULL,           -- human-readable rule as configured
  -- every input the strategy actually looked at, at this instant
  features        JSONB NOT NULL,
  -- which conditions evaluated true/false and their thresholds
  conditions      JSONB NOT NULL,
  confidence      NUMERIC(5,4),
  acted_on        BOOLEAN NOT NULL DEFAULT false,
  veto_reason     TEXT                     -- set by risk engine if rejected
);
CREATE INDEX ON signals (account_id, ts DESC);

-- ============ orders & fills ============
CREATE TABLE orders (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES paper_accounts(id),
  signal_id       UUID REFERENCES signals(id),
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,
  qty             NUMERIC(18,6) NOT NULL,
  order_type      TEXT NOT NULL,           -- 'market' | 'limit' | 'stop'
  limit_price     NUMERIC(18,4),
  stop_price      NUMERIC(18,4),
  status          TEXT NOT NULL,           -- 'pending','filled','partial','cancelled','rejected'
  source          TEXT NOT NULL,           -- 'strategy' | 'manual'
  submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  broker_order_id TEXT
);

CREATE TABLE fills (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id        UUID NOT NULL REFERENCES orders(id),
  qty             NUMERIC(18,6) NOT NULL,
  -- friction breakdown, itemized so the UI can teach cost awareness
  reference_price NUMERIC(18,4) NOT NULL,  -- mid at decision time
  fill_price      NUMERIC(18,4) NOT NULL,  -- after slippage
  slippage_cost   NUMERIC(18,4) NOT NULL,
  spread_cost     NUMERIC(18,4) NOT NULL,
  commission      NUMERIC(18,4) NOT NULL,
  reg_fees        NUMERIC(18,4) NOT NULL,
  filled_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ positions & trades ============
CREATE TABLE positions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES paper_accounts(id),
  symbol          TEXT NOT NULL,
  qty             NUMERIC(18,6) NOT NULL,
  avg_entry_price NUMERIC(18,4) NOT NULL,
  stop_price      NUMERIC(18,4),
  target_price    NUMERIC(18,4),
  opened_at       TIMESTAMPTZ NOT NULL,
  strategy_id     UUID REFERENCES strategies(id),
  entry_signal_id UUID REFERENCES signals(id),
  status          TEXT NOT NULL DEFAULT 'open'
);

-- A completed round trip. One row per closed trade.
CREATE TABLE trades (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES paper_accounts(id),
  strategy_id     UUID REFERENCES strategies(id),
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,
  qty             NUMERIC(18,6) NOT NULL,
  entry_price     NUMERIC(18,4) NOT NULL,
  exit_price      NUMERIC(18,4) NOT NULL,
  opened_at       TIMESTAMPTZ NOT NULL,
  closed_at       TIMESTAMPTZ NOT NULL,
  gross_pnl       NUMERIC(18,4) NOT NULL,
  total_friction  NUMERIC(18,4) NOT NULL,
  net_pnl         NUMERIC(18,4) NOT NULL,
  r_multiple      NUMERIC(8,4),            -- net_pnl / initial_risk
  exit_reason     TEXT NOT NULL,           -- 'stop','target','signal','eod_flat','risk_halt'
  entry_signal_id UUID REFERENCES signals(id),
  exit_signal_id  UUID REFERENCES signals(id),
  -- benchmark: what SPY did over the identical holding window
  benchmark_return_pct NUMERIC(10,6)
);
CREATE INDEX ON trades (account_id, closed_at DESC);

-- ============ education ============
CREATE TABLE explanations (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trade_id        UUID NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
  entry_rationale TEXT NOT NULL,
  exit_rationale  TEXT NOT NULL,
  what_went_right TEXT,
  what_went_wrong TEXT,
  tip_id          TEXT NOT NULL REFERENCES tips(id),
  tip_text        TEXT NOT NULL,
  -- audit trail: exactly what was sent to the model
  model           TEXT NOT NULL,
  prompt_hash     TEXT NOT NULL,
  generated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tips (
  id              TEXT PRIMARY KEY,        -- 'risk.stop_too_tight'
  category        TEXT NOT NULL,           -- 'risk','psychology','execution','costs','strategy'
  title           TEXT NOT NULL,
  body            TEXT NOT NULL,
  trigger_rule    JSONB NOT NULL           -- see §11.3
);

CREATE TABLE user_tip_history (
  user_id         UUID NOT NULL REFERENCES users(id),
  tip_id          TEXT NOT NULL REFERENCES tips(id),
  shown_count     INTEGER NOT NULL DEFAULT 0,
  last_shown_at   TIMESTAMPTZ,
  PRIMARY KEY (user_id, tip_id)
);

-- ============ risk ============
CREATE TABLE risk_settings (
  account_id            UUID PRIMARY KEY REFERENCES paper_accounts(id),
  risk_per_trade_pct    NUMERIC(6,4) NOT NULL DEFAULT 0.01,   -- 1% of equity
  max_daily_loss_pct    NUMERIC(6,4) NOT NULL DEFAULT 0.03,   -- 3% halts the day
  max_open_positions    INTEGER NOT NULL DEFAULT 3,
  max_trades_per_day    INTEGER NOT NULL DEFAULT 10,
  max_position_pct      NUMERIC(6,4) NOT NULL DEFAULT 0.20,
  cooldown_after_losses INTEGER NOT NULL DEFAULT 3,
  cooldown_minutes      INTEGER NOT NULL DEFAULT 30
);

CREATE TABLE risk_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES paper_accounts(id),
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_type      TEXT NOT NULL,           -- 'veto','daily_halt','cooldown_start'
  detail          JSONB NOT NULL
);
```

---

## 6. Repository structure

```
paper-trader/
├── README.md
├── BUILD_SPEC.md                  # this file
├── docker-compose.yml             # postgres+timescale, redis (local dev)
├── .env.example
│
├── backend/
│   ├── pyproject.toml
│   ├── alembic/
│   ├── app/
│   │   ├── main.py                # FastAPI app factory
│   │   ├── config.py              # pydantic-settings
│   │   ├── db.py
│   │   ├── deps.py
│   │   ├── api/
│   │   │   ├── routes_account.py
│   │   │   ├── routes_market.py
│   │   │   ├── routes_strategies.py
│   │   │   ├── routes_trades.py
│   │   │   ├── routes_education.py
│   │   │   └── ws.py              # client WebSocket fan-out
│   │   ├── models/                # SQLAlchemy
│   │   ├── schemas/               # Pydantic DTOs
│   │   ├── ingest/
│   │   │   ├── stream.py          # Alpaca WS client + reconnect
│   │   │   ├── subscriptions.py   # 30-symbol manager
│   │   │   └── bars.py            # tick → bar aggregation
│   │   ├── strategies/
│   │   │   ├── base.py            # Strategy ABC
│   │   │   ├── registry.py
│   │   │   ├── orb.py
│   │   │   ├── vwap_reversion.py
│   │   │   ├── ema_cross.py
│   │   │   └── rsi2.py
│   │   ├── execution/
│   │   │   ├── broker.py          # Broker ABC
│   │   │   ├── paper_broker.py    # ONLY implementation
│   │   │   ├── friction.py        # §9
│   │   │   └── positions.py
│   │   ├── risk/
│   │   │   ├── engine.py
│   │   │   └── sizing.py
│   │   ├── education/
│   │   │   ├── explainer.py       # Claude API
│   │   │   ├── prompts.py
│   │   │   ├── tips.py            # selector
│   │   │   └── tip_library.yaml   # ~60 curated tips
│   │   ├── analytics/
│   │   │   ├── metrics.py         # expectancy, Sharpe, drawdown, R-dist
│   │   │   └── benchmark.py       # §12
│   │   └── backtest/
│   │       ├── runner.py
│   │       ├── gate.py            # §8.5
│   │       └── walkforward.py
│   └── tests/
│
├── web/                           # React + Vite
│   └── src/
│       ├── screens/
│       ├── components/
│       ├── hooks/useMarketSocket.ts
│       └── api/
│
├── mobile/                        # Expo
│   └── src/
│
└── packages/shared/               # TS types generated from OpenAPI
```

---

## 7. Backend services in detail

### 7.1 Ingest service

Long-running process, single instance.

**Responsibilities**
- Maintain the Alpaca data WebSocket (`alpaca-py` `StockDataStream`).
- Subscribe to bars, quotes, and trades for the active watchlist (≤30 symbols).
- Write 1-minute bars to Postgres; aggregate to 5/15-minute in the DB or on read.
- Write latest quote per symbol to Redis (`quote:{symbol}`, TTL 60s).
- Publish to Redis channel `md:{symbol}` for fan-out to the client WebSocket.

**Reliability requirements — implement all of these:**
- Exponential backoff reconnect with jitter, capped at 30s.
- On reconnect, **backfill the gap** via the REST bars endpoint. Do not silently
  leave a hole; a missing bar corrupts every indicator downstream.
- Heartbeat: if no message for 60s during market hours, force reconnect.
- Track `last_bar_ts` per symbol; expose in `/health`.
- Respect the market calendar — don't alarm on a closed market. Use Alpaca's
  clock/calendar endpoints, and handle half-days.

### 7.2 Bar aggregation

Alpaca sends minute bars directly, but you also want to construct bars from trades
for resilience. Rules:

- Bar timestamps are **bar-open** time, UTC in the DB, rendered in
  America/New_York in the UI.
- A bar is **not final** until the next bar's first tick arrives or the minute
  boundary plus a 2-second grace period elapses.
- **Strategies evaluate only on finalized bars.** Evaluating on a forming bar is
  a classic self-inflicted lookahead bug that makes backtests look brilliant and
  live results terrible.

### 7.3 Strategy engine

Runs in the worker process. On each finalized bar for a subscribed symbol:

1. Load the rolling window (e.g. last 200 bars) from Postgres/cache.
2. Compute the indicator set once per symbol per bar; share across strategies.
3. For each enabled, gate-passed strategy: call `evaluate()`.
4. If a signal is returned, **write the `signals` row first** (with full evidence),
   then hand to the risk engine.

### 7.4 Risk engine

Runs *after* signal creation, *before* order submission. Order of checks:

```python
def evaluate(signal, account, settings) -> RiskDecision:
    if daily_loss_exceeded(account, settings):        veto('daily_halt')
    if in_cooldown(account, settings):                veto('cooldown')
    if open_positions(account) >= settings.max_open_positions: veto('max_positions')
    if trades_today(account) >= settings.max_trades_per_day:   veto('max_trades')
    if market_closes_within(minutes=10):              veto('near_close')  # entries only
    qty = position_size(signal, account, settings)
    if qty <= 0:                                      veto('size_zero')
    if notional(qty) > settings.max_position_pct * account.equity: qty = clamp(...)
    return RiskDecision(approved=True, qty=qty)
```

**Position sizing — fixed fractional risk:**

```
risk_dollars = equity * risk_per_trade_pct
stop_distance = abs(entry_price - stop_price)     # stop comes from the strategy
qty = floor(risk_dollars / stop_distance)
```

A strategy that does not define a stop cannot be sized and is rejected. This is
intentional: **every entry must know where it is wrong before it is placed.**

Every veto writes a `risk_events` row and surfaces in the journal as a teachable
moment ("your strategy wanted to trade here; the risk engine said no, because…").
Vetoes are some of the most valuable content in the app.

### 7.5 Execution service

```python
class Broker(ABC):
    @abstractmethod
    async def submit(self, order: OrderRequest) -> BrokerOrder: ...
    @abstractmethod
    async def cancel(self, order_id: str) -> None: ...
    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...
    @abstractmethod
    async def get_account(self) -> BrokerAccount: ...
```

**`PaperBroker` is the only implementation in this build.** It calls Alpaca's paper
endpoint (`https://paper-api.alpaca.markets`) and then applies the local friction
model in §9 on top of the returned fill.

Why both? Alpaca paper gives you realistic order lifecycle, market-hours behavior,
and position bookkeeping for free. But per Alpaca's own documentation, paper
trading does **not** simulate market impact, latency slippage, order queue position,
borrow fees, dividends, or regulatory fees — and it fills partially at random ~10%
of the time without validating quantity against real NBBO liquidity. Left alone,
that is an optimistically biased simulator. §9 corrects for it.

---

## 8. Strategy framework

### 8.1 Interface

```python
@dataclass(frozen=True)
class Signal:
    side: Literal['buy', 'sell']
    intent: Literal['entry', 'exit']
    symbol: str
    rule_id: str
    rule_text: str
    features: dict[str, float | str | bool]   # everything examined
    conditions: list[Condition]               # each: name, operator, threshold, actual, passed
    stop_price: float | None                  # REQUIRED for entries
    target_price: float | None
    confidence: float = 0.5

class Strategy(ABC):
    slug: str
    default_params: dict
    param_schema: type[BaseModel]

    @abstractmethod
    def evaluate(self, ctx: BarContext) -> Signal | None: ...

    @abstractmethod
    def manage(self, ctx: BarContext, pos: Position) -> Signal | None:
        """Called each bar for open positions — trailing stops, time exits."""
```

### 8.2 The `Condition` record — critical

```python
@dataclass(frozen=True)
class Condition:
    name: str            # 'rsi_below_threshold'
    description: str     # 'RSI(2) is below the oversold threshold'
    operator: str        # '<'
    threshold: float     # 10.0
    actual: float        # 7.3
    passed: bool         # True
```

Every strategy must emit the **full condition list, including conditions that
failed**. This is what lets the app explain not only "why it entered" but "how
close it came to not entering" — which is far more instructive.

### 8.3 Starter strategies (implement all four)

**1. Opening Range Breakout (`orb`)**
- Define the opening range as the high/low of the first N minutes (default 15).
- Long on a close above range high; short-equivalent (exit/avoid) below range low.
- Filters: minimum range width as a multiple of ATR; volume above the 20-day
  average for that time of day.
- Stop: opposite side of the opening range. Target: 2R, or trail after 1R.
- Time exit: flat by 15:55 ET.

**2. VWAP Mean Reversion (`vwap_reversion`)**
- Entry when price is more than K standard deviations below session VWAP
  (default K = 2.0) **and** the daily trend filter is up (price > 200-period EMA).
- Exit at VWAP touch, or stop at 1.5 × ATR(14).
- The trend filter is not optional — mean reversion against a downtrend is how
  people learn about falling knives the expensive way.

**3. EMA Crossover with regime filter (`ema_cross`)**
- Fast EMA (9) crosses above slow EMA (21) on the 5-minute chart.
- Regime filter: only take longs when the daily close is above the 200-day SMA.
- Stop: recent swing low or 2 × ATR. Trail at 1.5 × ATR after 1R.
- Include this specifically so you can *watch it underperform* in choppy markets.
  Crossovers whipsaw. Seeing that in your own journal teaches more than reading it.

**4. RSI(2) Mean Reversion (`rsi2`)**
- Larry Connors' classic. Long when RSI(2) < 10 and price > 200-day SMA.
- Exit when RSI(2) > 70 or price closes above the 5-day SMA.
- Hard time stop at 5 days.

### 8.4 Indicator set (computed once per symbol per bar)

`EMA(9,21,50,200)`, `SMA(5,20,200)`, `RSI(2,14)`, `MACD(12,26,9)`, `ATR(14)`,
`Bollinger(20,2)`, session `VWAP` + std bands, `volume_zscore(20)`,
`relative_volume`, `opening_range_high/low`, `gap_pct`, `spread_bps`,
`minutes_since_open`, `regime` (`trend_up`/`trend_down`/`chop`, from ADX + EMA slope).

Store the full snapshot in `signals.features` — this is the raw material the
explanation layer reads from.

### 8.5 The backtest gate — required before a strategy can trade

`strategies.enabled` cannot be set `true` unless `gate_passed` is `true`.

**Gate criteria** (all must hold on out-of-sample data):

| Check | Threshold |
|---|---|
| Sample size | ≥ 100 trades |
| Out-of-sample period | ≥ 12 months, never seen during parameter selection |
| Expectancy after friction | > 0 |
| Profit factor | ≥ 1.2 |
| Max drawdown | ≤ 20% |
| Walk-forward efficiency | ≥ 0.5 (OOS return ÷ in-sample return) |
| Beats SPY buy-and-hold | Yes, on risk-adjusted return over the same window |

**Backtest implementation rules — violating any of these invalidates the result:**

1. **No lookahead.** Indicators use only bars `<= t`. Signals on bar `t` execute at
   bar `t+1`'s open, never bar `t`'s close.
2. **Friction applied identically** to live paper (§9). Same code path.
3. **Survivorship bias:** use a point-in-time universe if you can get one;
   if not, restrict to symbols continuously listed across the window and say so.
4. **Parameter sweeps must be walk-forward**, not a single global optimization.
   A single optimization over the whole history is curve fitting, and it will
   produce a beautiful equity curve that means nothing.
5. Report the **distribution** of outcomes, not just the mean. A strategy whose
   profit comes from two lucky trades is not a strategy.

Use VectorBT for fast parameter sweeps, then re-verify the winner in Backtrader's
event-driven loop to catch lookahead bugs the vectorized version hides.

### 8.6 Tradable universe (v1)

Restrict to liquid names where IEX tracks NBBO closely and spreads are tight:

```
ETFs:    SPY QQQ IWM DIA XLF XLE XLK SMH
Large:   AAPL MSFT NVDA AMZN GOOGL META TSLA AMD AVGO JPM
         V UNH JNJ WMT XOM COST HD NFLX
```

Hard-block: sub-$5 stocks, average daily volume under 5M shares, anything with a
market cap under $2B. Penny stocks and low-float names are where the IEX data gap,
the spread model, and reality all diverge the most — and they're where retail
traders lose fastest.

---

## 9. Friction model — the part most homemade trading apps get wrong

If you skip this section, your paper results will be meaningfully better than
reality and you will draw a false conclusion. This is the highest-value 200 lines
of code in the project.

### 9.1 What Alpaca paper does *not* charge you

Per Alpaca's documentation: market impact, latency slippage, queue position,
borrow fees, dividends, regulatory fees. It also fills at the quoted bid/ask
without validating your size against available liquidity.

### 9.2 What to model

```python
@dataclass
class FrictionConfig:
    # half-spread paid on market orders, widened for IEX-only data
    spread_multiplier: float = 1.5
    min_spread_bps: float = 2.0
    # slippage as a fraction of ATR, scaled by order size vs. typical volume
    slippage_atr_frac: float = 0.05
    # commission: $0 at most retail brokers, but keep the hook
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    # Regulatory fees — SELLS ONLY. These rates change; see note below.
    # SEC Section 31: $20.60 per $1M of principal, effective April 4, 2026.
    # (It was $0.00/million before that date — the rate is reset annually and has
    #  been set to zero in some periods, so read it from config, never hardcode.)
    sec_fee_rate: float = 0.0000206      # = 20.60 / 1_000_000
    # FINRA Trading Activity Fee: per-share on sales, with a per-trade cap.
    # Current rate lives in FINRA By-Laws Schedule A, Section 1 — look it up and
    # set it here rather than trusting this default.
    taf_per_share: float = 0.000166
    taf_cap: float = 8.30
    # extra penalty for trading in the first/last 5 minutes
    open_close_penalty_multiplier: float = 2.0
```

```python
def apply_friction(order, quote, atr, cfg) -> FillResult:
    mid = (quote.bid + quote.ask) / 2
    spread = max(quote.ask - quote.bid, mid * cfg.min_spread_bps / 10_000)
    half_spread = (spread / 2) * cfg.spread_multiplier

    size_factor = min(order.qty / quote.typical_bar_volume, 1.0)
    slip = atr * cfg.slippage_atr_frac * (0.5 + size_factor)

    if in_first_or_last_5_minutes(order.ts):
        half_spread *= cfg.open_close_penalty_multiplier
        slip *= cfg.open_close_penalty_multiplier

    direction = 1 if order.side == 'buy' else -1
    fill_price = mid + direction * (half_spread + slip)
    ...
```

### 9.3 Make friction visible

The UI must show, per trade: gross P&L, then each friction component subtracted,
then net. And on the dashboard, a running **"total paid to friction this month"**
figure.

This single number is the most underrated lesson in trading. A strategy that
trades 10 times a day at 3 bps of round-trip friction gives up roughly 7.5% a month
before it makes a cent. Watching that counter climb teaches trade-frequency
discipline better than any tip text ever will.

---

## 10. Realtime delivery to clients

**Client WebSocket:** `wss://api.yourapp.com/ws?token=<jwt>`

Server → client message envelope:

```json
{ "type": "quote", "data": { "symbol": "AAPL", "bid": 231.44, "ask": 231.46, "ts": "..." } }
{ "type": "bar",   "data": { "symbol": "AAPL", "timeframe": "1Min", "o":.., "h":.., "l":.., "c":.., "v":.. } }
{ "type": "signal","data": { "signal_id": "...", "symbol": "AAPL", "rule_id": "orb.breakout_long", "acted_on": true } }
{ "type": "fill",  "data": { "order_id": "...", "symbol": "AAPL", "qty": 43, "fill_price": 231.52 } }
{ "type": "trade_closed", "data": { "trade_id": "...", "net_pnl": -18.40, "r_multiple": -1.0 } }
{ "type": "explanation_ready", "data": { "trade_id": "...", "explanation_id": "..." } }
{ "type": "risk_event", "data": { "event_type": "daily_halt", "detail": {...} } }
{ "type": "heartbeat", "ts": "..." }
```

Client → server: `{"type":"subscribe","symbols":["AAPL","MSFT"]}`.

**Client requirements:** exponential backoff reconnect, resubscribe on reconnect,
show a stale-data indicator if no heartbeat for 30s. Never let the UI display a
price without the user being able to tell how old it is.

---

## 11. The education layer

This is the reason to build this app rather than use an existing one. Get it right.

### 11.1 The cardinal rule: log reasons, never reconstruct them

**Wrong (and it is what most people build):** trade closes → send the price chart
to an LLM → ask "why did this trade lose?" → get a fluent, confident, invented
narrative.

That is a hallucination generator. It will teach you superstitions.

**Right:** at the moment the strategy fires, write the `signals` row containing the
exact rule, every condition with its threshold and actual value, and the complete
feature snapshot. When the trade closes, the LLM's job is **not to determine why**
— it is to *translate an already-determined, recorded reason into clear English.*

The model is a writer, not an analyst. It gets the answer handed to it.

### 11.2 Explanation generation

Input assembled deterministically:

```python
{
  "entry": {
    "rule_id": "vwap_reversion.long_entry",
    "rule_text": "Enter long when price is >2.0 std below session VWAP and price > EMA200",
    "conditions": [
      {"name":"vwap_zscore","operator":"<","threshold":-2.0,"actual":-2.34,"passed":true},
      {"name":"above_ema200","operator":">","threshold":0,"actual":1,"passed":true},
      {"name":"rel_volume","operator":">","threshold":1.2,"actual":1.31,"passed":true}
    ],
    "features": { "...full snapshot..." },
    "stop_price": 230.10, "target_price": 233.80, "risk_per_share": 1.34
  },
  "exit": {
    "rule_id": "risk.stop_hit",
    "rule_text": "Stop loss triggered at 1.5x ATR below entry",
    "conditions": [ ... ]
  },
  "outcome": {
    "gross_pnl": -57.62, "friction": 4.31, "net_pnl": -61.93,
    "r_multiple": -1.0, "hold_minutes": 23,
    "mae": -1.41, "mfe": 0.62,
    "benchmark_return_pct": 0.08
  },
  "context": {
    "regime": "chop", "trades_today": 6,
    "consecutive_losses": 2,
    "strategy_30d": {"win_rate": 0.41, "expectancy_r": -0.06, "n": 74}
  }
}
```

**System prompt constraints (enforce these):**

- You are explaining a decision that has already been made and recorded. Do not
  speculate about causes that are not present in the supplied conditions or features.
- Every factual claim must reference a supplied value. Cite the number.
- If the data does not support a conclusion, say so plainly.
- Do not predict future prices. Do not suggest what to trade next.
- Do not be encouraging about a losing process. A loss that followed the rules is
  a good trade; a win that broke the rules is a bad trade. Say which happened.
- Maximum 120 words per section.

**Output sections:** `entry_rationale`, `exit_rationale`, `what_went_right`,
`what_went_wrong`.

That fifth constraint is the pedagogically important one. Most trading apps
congratulate you on wins. This one should tell you when you got lucky.

**Cost/latency:** use Claude Haiku for routine trades, escalate to Sonnet for the
daily summary. Generate async in the worker; push `explanation_ready` over the
WebSocket. Cache by `prompt_hash` so re-renders don't re-bill.

**Fallback:** if the API is unavailable, render a deterministic template from the
same JSON. The app must never silently drop the explanation — the explanation *is*
the product.

### 11.3 Trading tips — selected, not random

You asked for a tip after each trade. A random tip from a list is noise. A tip that
matches what just happened to you is a lesson. Tips are selected by rule:

```yaml
- id: risk.stop_too_tight
  category: risk
  title: Your stop may be inside the noise
  trigger_rule:
    all:
      - { metric: exit_reason, op: eq, value: stop }
      - { metric: mfe_r, op: gt, value: 0.8 }        # went your way first
      - { metric: stop_distance_atr, op: lt, value: 1.0 }
  body: >
    This trade moved 0.8R in your favour before stopping out, and your stop sat
    less than one ATR away. A stop inside the average bar range gets hit by
    ordinary noise rather than by your thesis being wrong. Size smaller and stop
    wider — the risk in dollars stays identical.

- id: costs.frequency_drag
  category: costs
  trigger_rule:
    all:
      - { metric: trades_today, op: gte, value: 8 }
      - { metric: friction_pct_of_gross_today, op: gt, value: 0.30 }
  body: >
    Friction ate more than 30% of your gross P&L today across {trades_today}
    trades. At this rate your strategy has to be right substantially more often
    just to break even. Fewer, higher-conviction trades beat more trades at
    almost every skill level.

- id: psychology.revenge_trade
  category: psychology
  trigger_rule:
    all:
      - { metric: consecutive_losses, op: gte, value: 2 }
      - { metric: seconds_since_last_exit, op: lt, value: 120 }
      - { metric: source, op: eq, value: manual }
  body: >
    You opened this manually within two minutes of a second consecutive loss.
    That timing is the classic signature of a revenge trade. The cooldown timer
    exists for this exact moment — let it run.

- id: process.good_loss
  category: psychology
  trigger_rule:
    all:
      - { metric: net_pnl, op: lt, value: 0 }
      - { metric: followed_rules, op: eq, value: true }
      - { metric: r_multiple, op: gte, value: -1.05 }
  body: >
    This was a good trade. It lost money, and it lost exactly the amount you
    decided to risk, for the reason you planned for. Separating decision quality
    from outcome is the skill that takes longest to build and matters most.
```

**Selector logic:** evaluate all trigger rules against the trade context; rank
matches by category priority (`risk` > `psychology` > `costs` > `execution` >
`strategy`); suppress any tip shown in the last 10 trades unless it triggers three
times in a row (repetition is signal at that point); fall back to a rotating
fundamentals tip if nothing matches.

Ship with ~60 tips across the five categories.

---

## 12. The Reality Ledger — benchmark everything

A dedicated, always-visible panel answering one question: **is any of this better
than having done nothing?**

Displays:
- Your account equity curve vs. SPY buy-and-hold from the same start date, same
  starting capital.
- Per-trade: your return vs. what SPY did over the identical holding window
  (`trades.benchmark_return_pct`).
- Rolling 30-day: expectancy in R, win rate, profit factor, max drawdown, total
  friction paid, and **hours spent** (track session time — the opportunity cost is
  real and nobody measures it).
- A plain-language verdict line, updated weekly, e.g.:
  *"Over 62 trades in 30 days, your strategies returned −2.1% net. SPY returned
  +1.8%. You paid $214 in simulated friction across 41 hours of screen time."*

Do not soften this copy. If it reads uncomfortably, it is working. This panel is
the honest counterweight to an interface designed to make trading fast and fun,
and it is the single feature most likely to save you real money later.

---

## 13. Frontend

### 13.1 Design principles

Simple front end, complex backend — as specified. The rule: **the screen shows
what to do now; everything explaining why lives one tap away.** No dense
multi-panel terminal. One primary action visible at a time.

Dark theme default. Green/red for P&L, but pair every color with a
`+`/`−` sign and a text label so it is readable for colorblind users.

### 13.2 Mobile (React Native / Expo) — four tabs

**Tab 1 — Now** (the default screen)
- Top: account equity, day P&L (dollar + %), a small SPY comparison chip.
- Center: a single large card, whichever applies:
  - open position → symbol, size, live P&L, distance to stop, distance to target,
    one big `CLOSE` button
  - active signal awaiting confirmation → symbol, direction, the plain-English
    reason, `TAKE` / `SKIP`
  - nothing happening → the next thing your strategies are watching for
- Bottom: a strip of watchlist sparklines.
- One thumb, no scrolling required for the primary action.

**Tab 2 — Chart**
- `lightweight-charts`, candles, VWAP, active EMAs.
- Entry/exit markers on every historical trade for that symbol.
- Timeframe pills: 1m / 5m / 15m / 1D.

**Tab 3 — Journal** (this is where the learning happens)
- Reverse-chronological trade cards. Each card:
  - header: symbol, direction, net P&L, R multiple
  - `Why in:` one sentence
  - `Why out:` one sentence
  - `Tip:` the selected tip, visually distinct
  - tap → full explanation, condition table showing every check with threshold vs.
    actual, friction breakdown, and the trade marked on a mini chart
- Filter chips: All / Wins / Losses / Rule-following / Rule-breaking.
- Vetoed signals appear here too, greyed — "your strategy wanted this; risk said no."

**Tab 4 — Reality** (§12)

### 13.3 Desktop (React) — three panes

```
┌────────────┬──────────────────────────────┬──────────────────┐
│ Watchlist  │        Chart + markers       │  Live journal    │
│ + signals  │                              │  (stream)        │
│            ├──────────────────────────────┤                  │
│            │  Positions / Orders / Risk   │  Explanation     │
└────────────┴──────────────────────────────┴──────────────────┘
```
Keyboard shortcuts: `B` buy, `S` sell, `C` close, `Esc` cancel, `/` symbol search.
Same components as mobile where possible; different layout only.

### 13.4 The 3-second confirm — keep it

You asked for a front end fast enough to trade quickly. It is: strategy-generated
orders execute automatically with no delay, and that is where speed actually
matters.

**Manual** orders require a 3-second press-and-hold. Impulsive manual clicking
during a losing streak is the single most reliably destructive behavior in retail
trading, and three seconds is enough to interrupt it without meaningfully
affecting any trade whose edge is real. If your edge dies in three seconds, it was
never an edge you could capture on a retail connection anyway.

The hold ring animation shows the position size and dollar risk while it fills.

---

## 14. API surface

```
POST   /auth/register
POST   /auth/login                          → JWT

GET    /account                             → equity, cash, day P&L, benchmark delta
GET    /account/positions
GET    /account/risk-settings
PATCH  /account/risk-settings

GET    /market/watchlist
PUT    /market/watchlist                    → manages the 30-symbol cap
GET    /market/bars?symbol=&timeframe=&start=&end=
GET    /market/quote?symbol=
GET    /market/clock                        → market open/closed, next open

GET    /strategies
POST   /strategies                          → create from slug + params
PATCH  /strategies/{id}                     → enable/disable (blocked unless gate_passed)
POST   /strategies/{id}/backtest            → async job
GET    /strategies/{id}/backtest/{job_id}
GET    /strategies/{id}/gate                → gate report, pass/fail per criterion

GET    /trades?limit=&cursor=&filter=
GET    /trades/{id}                         → full detail incl. friction breakdown
GET    /trades/{id}/explanation
GET    /signals?acted_on=&limit=            → includes vetoed signals

POST   /orders                              → manual order (requires confirm_token)
DELETE /orders/{id}

GET    /analytics/summary?period=30d
GET    /analytics/benchmark?period=30d      → Reality Ledger data
GET    /analytics/friction?period=30d

WS     /ws
```

---

## 15. Environment variables

```bash
# Alpaca — PAPER ONLY. Never put live keys in this project.
ALPACA_API_KEY=
ALPACA_API_SECRET=
ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_FEED=iex                 # free tier; 'sip' requires Algo Trader Plus

FINNHUB_API_KEY=                     # optional, news/sentiment context

ANTHROPIC_API_KEY=
EXPLANATION_MODEL=claude-haiku-4-5
SUMMARY_MODEL=claude-sonnet-4-5

DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
JWT_SECRET=
ENVIRONMENT=development

# Safety rail — the paper broker asserts this is false on every submit.
ENABLE_LIVE_TRADING=false
```

Add a startup assertion: if `ENABLE_LIVE_TRADING` is anything but `false`, or if
the base URL does not contain `paper-api`, **refuse to boot** with a clear error.

---

## 16. Build phases

Each phase has acceptance criteria. Do not start the next phase until they pass.

### Phase 1 — Data spine (week 1)
Build: docker-compose (Postgres+Timescale, Redis), Alembic migrations, Alpaca
paper account + keys, ingest service with reconnect and gap backfill, bars written
to DB, `/market/bars` and `/market/quote`.

✅ Ingest runs a full trading day without a gap in 1-minute bars for 10 symbols.
✅ Killing the process mid-session and restarting backfills the gap automatically.
✅ `/health` reports `last_bar_ts` per symbol.

### Phase 2 — Execution + risk (week 2)
Build: `Broker` ABC, `PaperBroker`, friction model, position/trade lifecycle, risk
engine with all vetoes, manual order endpoint.

✅ A manual market order produces `orders` → `fills` → `positions` rows with
   itemized friction.
✅ Closing produces a `trades` row with correct gross/net P&L and R multiple.
✅ Exceeding daily loss halts new entries and writes a `risk_events` row.
✅ Unit tests cover every friction component against hand-computed values.

### Phase 3 — Strategy engine (week 3)
Build: `Strategy` ABC, indicator pipeline, all four strategies, signal + evidence
persistence, bar-finalization gating.

✅ Each strategy emits complete `conditions` arrays including failed conditions.
✅ A test proves no indicator reads a bar with `ts > signal_ts` (no lookahead).
✅ Signals fire only on finalized bars.

### Phase 4 — Backtest + gate (week 4)
Build: VectorBT sweep runner, Backtrader verification, walk-forward split, gate
report, enable/disable enforcement.

✅ Attempting to enable a strategy with `gate_passed = false` returns 409.
✅ Gate report shows pass/fail per criterion with the actual value.
✅ Backtest and live paper share the same friction code path (assert in tests).

### Phase 5 — Education layer (week 5)
Build: explainer with constrained prompt, tip library (60 tips), tip selector,
deterministic fallback renderer, explanation persistence.

✅ Every closed trade gets an explanation within 30 seconds.
✅ With the Anthropic key removed, the fallback template still renders.
✅ A test asserts the explainer never receives raw price history — only the
   recorded signal evidence. (This is the anti-hallucination guarantee.)
✅ Tip selection is deterministic and reproducible for a given trade context.

### Phase 6 — Web frontend (week 6)
Build: React app, WebSocket hook with reconnect, three-pane desktop layout,
journal, Reality Ledger.

✅ Reconnects and resubscribes after a dropped connection.
✅ Stale-data indicator appears within 30s of heartbeat loss.
✅ Journal renders the full condition table for any trade.

### Phase 7 — Mobile (week 7)
Build: Expo app, four tabs, shared API client and types.

✅ Runs on a physical device via Expo Go.
✅ Primary action on the Now tab is reachable with one thumb, no scrolling.
✅ 3-second hold-to-confirm works and shows dollar risk while filling.

### Phase 8 — Hardening (week 8)
Market calendar edge cases (half days, holidays), EOD reconciliation against
Alpaca's own position report, structured logging, Sentry, daily summary email.

✅ Local positions match Alpaca's paper positions at every close for 5 days.
✅ Half-day sessions handled without spurious reconnect alarms.

---

## 17. Claude Code vs Replit

**Recommendation: Claude Code for everything except optional early UI sketching.**

| | Claude Code | Replit |
|---|---|---|
| Backend (ingest, strategies, execution) | ✅ Real repo, real git, real tests, multi-process | ❌ Long-lived WebSocket processes need paid always-on; awkward multi-process |
| Backtesting | ✅ Heavy compute, local iteration | ❌ Constrained |
| React web frontend | ✅ Fine | ✅ Instant preview + hosting, good for the first look |
| React Native / Expo | ✅ Fine | ❌ Poor fit |
| Learning value | ✅ You learn the actual toolchain | ⚠️ More is hidden from you |

**Practical suggestion:** if you want to sketch the UI visually before committing
to a layout, do a throwaway static React mock in Replit in an afternoon. Then
build the real thing — all of it — in Claude Code in one repo. Deploy the backend
to Fly.io or Railway, the web app to Vercel.

**How to drive Claude Code with this document:**
1. Put this file in the repo root as `BUILD_SPEC.md`.
2. Add a `CLAUDE.md` containing the non-negotiable rules: paper only, friction
   always on, reasons logged at decision time never reconstructed, backtest gate
   enforced, no live broker driver.
3. Work **one phase per session**. Start each with: *"Read BUILD_SPEC.md. Implement
   Phase N. Do not start Phase N+1. Write the tests from the acceptance criteria
   first."*
4. Do not let it scaffold all eight phases at once. You will not understand the
   result, and understanding it is the entire point.

---

## 18. Testing requirements

- **Determinism:** given a fixed bar sequence, strategies produce identical signals
  every run. Seed everything.
- **No-lookahead property test:** hypothesis-generated bar series; assert no
  indicator value at time `t` changes when bars after `t` are appended.
- **Friction unit tests:** each component checked against hand-computed values.
- **Golden-file explanations:** fixed signal JSON → snapshot the fallback template
  output; assert it never contains a number absent from the input.
- **Reconciliation test:** local position state vs. Alpaca paper positions after a
  simulated session.
- **Replay harness:** record a real market day of WebSocket messages to a file;
  replay at 10× to test the full pipeline offline. Build this in Phase 1 — it will
  save you more time than anything else in this document.

---

## 19. Running costs

| Item | Cost |
|---|---|
| Alpaca data + paper trading | **$0** |
| Finnhub (news/sentiment) | **$0** free tier |
| Fly.io / Railway backend | ~$5–10/mo |
| Neon / Supabase Postgres | $0 free tier initially |
| Upstash Redis | $0 free tier |
| Vercel web hosting | $0 hobby |
| Anthropic API (explanations) | ~$1–5/mo at Haiku prices for a few hundred trades |
| **Total** | **~$5–15/month** |

Optional later: Alpaca Algo Trader Plus at $99/mo for full SIP data. Do not buy it
until the free-tier version has been running for months and you can articulate
exactly what the IEX-only feed is costing you.

---

## 20. What to do first

1. Create the Alpaca paper account (email only, free, no age or funding barrier).
2. Build the Phase 1 replay harness before anything else — record one real market
   day. Everything downstream gets easier.
3. Implement the friction model early, in Phase 2, not as a Phase 8 polish item.
   Building it late means everything you learn before then is wrong.
4. Run one strategy, on five symbols, for one month, before adding a second.

Then look at the Reality Ledger and see what it says. That number is the real
output of this project — the app is just the instrument that produces it.

---

## Sources

- [Alpaca — Market Data plans and pricing](https://alpaca.markets/data)
- [Alpaca — Paper Trading documentation](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca — About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api)
- [Massive (formerly Polygon.io) — pricing](https://massive.com/pricing)
- [Finnhub — Stock APIs](https://finnhub.io/)
- [Best Financial Data APIs in 2026 — comparison](https://www.nb-data.com/p/best-financial-data-apis-in-2026)
- [The Data on Day Trading — academic studies summarized](https://www.currentmarketvaluation.com/posts/the-data-on-day-trading.php)
- [Barber & Odean — Day Traders Lose Money and Keep Trading (Taiwan)](https://www.tradicted.com/research/barber-learning-2020/)
- [E*TRADE — Pattern Day Trader rule change, effective June 4 2026](https://us.etrade.com/knowledge/library/margin/pattern-day-trading-rule-change)
- [SEC — FINRA rule filing SR-FINRA-2025-017](https://www.sec.gov/files/rules/sro/finra/2026/34-105226.pdf)
- [FINRA — Day Trading (investor guidance)](https://www.finra.org/investors/investing/investment-products/stocks/day-trading)
- [FINRA Information Notice 03/17/26 — new Section 31 fee rate ($20.60/million, effective April 4 2026)](https://www.finra.org/sites/default/files/2026-03/Information-Notice-20260317.pdf)
- [FINRA — Trading Activity Fee guidance](https://www.finra.org/rules-guidance/guidance/trading-activity-fee)
- [Fidelity — UGMA/UTMA custodial accounts](https://www.fidelity.com/learning-center/personal-finance/custodial-account-for-kids)
- [pandas-ta-classic — maintained indicator library](https://github.com/xgboosted/pandas-ta-classic)
- [Python backtesting libraries compared, 2026](https://rmbell09-lang.github.io/tradesight/blog/python-backtesting-libraries-2026.html)
