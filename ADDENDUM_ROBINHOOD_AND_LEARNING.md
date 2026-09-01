# Addendum 2 — Robinhood Viability, Phase 2 Platform Path, and the Learning Layer

**Supplements `BUILD_SPEC.md` v1.0 and `ADDENDUM_LIVE_APPROVAL.md` v1.1.**
**Status:** v1.2

---

## 1. Your Robinhood claim, checked

You said short-term buy/sells shouldn't be an issue on Robinhood. **You're
substantially right**, with one open question that you need to test yourself before
building around it.

### 1.1 Settlement — you're right, conditionally

Per Robinhood's own settlement documentation:

- **Margin-type account (Instant / Gold):** you can "instantly trade with funds from
  unsettled stock and option sales." No T+1 wait. The freeriding and good-faith
  violation problems from Addendum 1 §8 **do not apply**.
- **Cash account:** must wait 1 trading day for proceeds to settle before buying
  again. Everything in Addendum 1 §8 applies in full.

**Action item:** have your mom check which type her account is *before* you build.
Account → settings. If it's a cash account, either she converts it or you cap the
strategy at one round trip per symbol per day. This is a two-minute check that
changes the design.

**Note on the $2,000 threshold:** a margin *account type* is not the same as margin
*borrowing*. FINRA requires $2,000 minimum equity to borrow. With ~$500 she'd have
the account type (and therefore unsettled-funds access) but not the ability to
leverage. That's fine — you don't want leverage, and I'd suggest she explicitly keep
margin investing disabled so there's no way to accidentally trade on borrowed money.

### 1.2 Pattern day trader — confirmed gone

The PDT designation and the $25,000 minimum were eliminated June 4, 2026. There is
no limit on day trades per five-day window anymore. Your original instinct was right
and my first answer was wrong.

### 1.3 The open question you must test: fractional share order types

**This is the finding that matters, and I could not resolve it from documentation.**

At $50–100 per position, almost every trade you place is fractional. A $75 position
in a $230 stock is 0.33 shares. Robinhood supports fractional trading down to $1,
and supports it in real time during market hours.

**But:** there is evidence that Robinhood rejects **limit orders on fractional
quantities**, at least on the sell side, with the error *"Limit order quantity cannot
include fractional shares."* Reports indicate fractional buys accept limit prices
while fractional sells do not. Robinhood's own help pages do not state the fractional
order-type rules explicitly, and I'm not willing to have you architect around a
guess.

**Why this is a big deal if true:** the entire risk model in the main spec rests on
"every entry defines its stop" (§7.4). If you cannot place a resting stop-loss order
on a fractional position, then:

- there is no broker-side protection while nobody is watching
- every exit becomes a manual market order your mom has to place in the moment
- your `stop_price` becomes an *alert* rather than an *order*
- real slippage will be far worse than the model assumes

### 1.4 The five-minute test that settles it

Do this before writing any Phase 9 code. In your mom's real account, one time:

1. Buy $5 of a liquid stock priced over $100 (so the quantity is clearly fractional).
2. Try to place a **limit sell** on that fractional position. Note whether it's
   accepted or rejected, and the exact error text.
3. Try to place a **stop-loss** or **stop-limit** sell on it. Same.
4. Screenshot both results.

Total cost: a few cents of spread. It answers definitively what no amount of research
will, and it's exactly the kind of thing you should get in the habit of verifying
yourself rather than trusting a document — including this one.

### 1.5 The workaround, if fractional stops are blocked

**Trade instruments where $50–100 buys whole shares.** This keeps full order-type
support and costs you nothing strategically. Revised universe for live mode:

```
Sector ETFs (liquid, tight spreads, mostly $30-100/share):
  XLF  XLE  XLI  XLU  XLP  XLV  XLB  XOP  KRE  HYG  EEM  EWZ  SLV

Liquid sub-$100 equities:
  F  BAC  T  KO  PFE  CSCO  INTC  SOFI  PLTR  WBD  VZ  KMI
```

Prices move — regenerate this list programmatically each week with a
`price < max_position_usd` filter plus your existing liquidity floor (>5M average
daily volume, >$2B market cap), rather than hardcoding it.

Sector ETFs are the better choice of the two groups: tighter spreads, no single-stock
earnings gap risk, and they behave well with the VWAP and ORB strategies. Build the
live universe from ETFs first.

### 1.6 Fees at your size

Robinhood charges $0 commission, but regulatory fees still apply **on sells**: SEC
Section 31 at $20.60 per million (so ~$0.0015 on a $75 sale) plus FINRA TAF per
share. At your trade size these round to fractions of a cent — genuinely negligible.

**Your real cost is the spread, and it doesn't scale down.** A 2-cent spread on a
$50 stock is 0.04% each way, whether you trade $75 or $75,000. On a $75 position
that's about $0.06 round trip. If a good day trade nets you $0.75, you just gave up
8% of it to the spread. That's the number to watch, and it's why the friction
tracking in §9 of the main spec matters more at small size, not less.

---

## 2. Phase 2 — "tie into whatever platform we need"

### 2.1 Robinhood cannot be the automation platform

**Robinhood has no official public API for stock trading.** They offer an official
*crypto* trading API, but nothing for equities. Every stock-trading "Robinhood API"
you'll find — `robin_stocks` and its relatives — is a reverse-engineered wrapper
around the private mobile app endpoints.

Using those means:
- violating Robinhood's terms of service
- storing your mom's Robinhood credentials in your code
- risking her account being restricted or closed
- building on endpoints that change without notice and break silently

That's your mom's account and her money. Don't.

### 2.2 The actual Phase 2 path

If automated live execution is the goal, the platform is **Alpaca live** — and here's
the payoff from the choice in v1.0: you've already built the entire system against
Alpaca's API. Going live is approximately a base-URL change plus a funded account.
Your strategies, execution layer, friction model, and journal all port unchanged.

| Platform | Official API | Notes |
|---|---|---|
| **Alpaca** | ✅ Full REST + WebSocket | Same API as your paper build. The obvious path. |
| **Tradier** | ✅ | Good API, brokerage + market data |
| **Interactive Brokers** | ✅ | Most powerful, hardest to work with |
| **Robinhood** | ❌ equities (crypto only) | Manual execution only |

So the sequence is: prove it on paper → prove it with manual Robinhood execution and
approval → *if* it works, move to Alpaca live where automation is legitimate.

### 2.3 The thing to be careful about in Phase 2

Automating execution deletes the approval step. That step is the load-bearing safety
feature of this entire design — it's what makes the setup legitimate and what keeps
your mom an actual participant instead of a rubber stamp.

My suggestion for Phase 2: **automate the data and the analysis, keep the decision
human.** If you eventually automate execution too, that's your mom's call as the
account holder, and it should come with hard server-side caps that she sets and only
she can raise.

### 2.4 Define "proved the POC" now, before you start

You said Phase 2 comes "after making money on this app to prove the POC." Define
what that means *today*, in writing, or you will rationalize whatever happens —
everyone does, it's not a character flaw.

**The statistics problem:** at these effect sizes, 20 or 30 trades tells you almost
nothing. Random noise easily produces a 60% win rate over 25 trades. You need on the
order of **100+ trades** before you can distinguish a real edge from luck, and even
then the confidence interval is wide.

**Write these down and commit to them before the first live trade:**

```yaml
poc_success_criteria:
  min_live_trades: 100
  min_duration_days: 90          # must span more than one market regime
  net_profit_after_all_costs: "> 0"
  beats_spy_buy_and_hold: true   # same capital, same period
  max_drawdown_pct: "< 20"
  profit_factor: ">= 1.2"
  # the honesty clause
  live_expectancy_within: "0.3R of paper expectancy"
    # if live is far worse than paper, the model is wrong,
    # not the market. Fix the model before scaling.

poc_failure_criteria:   # any ONE of these ends it
  - drawdown_pct > 25
  - net_loss_usd > 250
  - live_trades >= 50 and expectancy_r < 0
```

Put this in the repo as `POC_CRITERIA.yaml` and have the app evaluate it
automatically. A criterion you can edit after seeing results isn't a criterion — so
make the file append-only in git and have your mom review any proposed change.

---

## 3. The learning layer

You want it to store trends and analysis and get more accurate over time. Good
instinct — but the naive version of this is the most reliable way to blow up a
trading system, so here's the split between what works and what doesn't.

### 3.1 What does not work (and why it's so tempting)

**Continuously re-tuning strategy parameters based on recent P&L.** It feels like
learning. It is overfitting with extra steps. Markets are non-stationary and mostly
noise; a system that chases its own recent results will reliably fit the noise, look
brilliant on the data it fit, and fail forward. This is the single most common way
retail algo projects die.

Concretely, do **not** build: online parameter updates after each trade,
reinforcement learning on live P&L, "confidence scores" that rise after wins, or
anything that changes strategy behavior faster than monthly.

### 3.2 What does work — four things, in order of value

**1. A research dataset that grows forever.** Every signal, its full feature
snapshot, and its outcome, stored permanently. This is the actual asset you're
building. In a year it's a genuinely valuable dataset about your own market and your
own decisions, and every analysis below runs on it. It requires no cleverness — just
never delete anything.

**2. Regime tagging and per-regime performance.** The highest-value analysis in the
whole system. Label each day/session (`trend_up`, `trend_down`, `chop`, `high_vol`,
`low_vol`) and measure each strategy's expectancy within each regime. What you'll
almost certainly find: EMA crossover works in trends and bleeds in chop; VWAP
reversion is the reverse. Then the "learning" is simply *not running the wrong
strategy in the wrong regime* — which is a real, defensible edge and requires no ML
at all.

**3. Scheduled walk-forward re-fitting.** Monthly, never continuous. Champion/
challenger: the current parameters keep running while a candidate is fit on new data
and validated out-of-sample. The challenger only gets promoted if it beats the
champion by a margin on data neither has seen. Every version is recorded so you can
always roll back and always answer "what was running on March 4th?"

**4. Meta-learning on your own decisions.** This is where the real improvement
actually lives, and almost nobody builds it:
- Which hours of the day are your trades profitable? (Most retail edges die after
  the first hour.)
- Does the quality of your written `requester_thesis` correlate with outcome?
- When your mom declines, is she right? (Already in the Approver Scorecard.)
- Do you perform worse after a loss? After two? (You will. The question is how much.)
- Does an override of the strategy ever beat following it?

### 3.3 Schema additions

```sql
-- The permanent research dataset. Append-only. Never delete.
CREATE TABLE feature_store (
  signal_id       UUID PRIMARY KEY REFERENCES signals(id),
  symbol          TEXT NOT NULL,
  ts              TIMESTAMPTZ NOT NULL,
  features        JSONB NOT NULL,        -- full snapshot, versioned
  feature_version TEXT NOT NULL,
  regime_id       UUID REFERENCES regime_labels(id),
  outcome_r       NUMERIC(8,4),          -- filled in on resolution
  outcome_known_at TIMESTAMPTZ
);

CREATE TABLE regime_labels (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_date    DATE NOT NULL,
  symbol          TEXT,                  -- null = market-wide
  trend_label     TEXT NOT NULL,         -- 'up','down','chop'
  vol_label       TEXT NOT NULL,         -- 'low','normal','high'
  adx             NUMERIC(8,4),
  atr_percentile  NUMERIC(6,4),
  spy_return_pct  NUMERIC(8,4),
  labeled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (session_date, symbol)
);

CREATE TABLE strategy_performance_by_regime (
  strategy_id     UUID NOT NULL REFERENCES strategies(id),
  trend_label     TEXT NOT NULL,
  vol_label       TEXT NOT NULL,
  n_trades        INTEGER NOT NULL,
  win_rate        NUMERIC(6,4),
  expectancy_r    NUMERIC(8,4),
  profit_factor   NUMERIC(8,4),
  -- honesty column: is n big enough to mean anything?
  is_significant  BOOLEAN NOT NULL DEFAULT false,
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (strategy_id, trend_label, vol_label)
);

-- Every parameter set that has ever run. Full lineage.
CREATE TABLE model_versions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id       UUID NOT NULL REFERENCES strategies(id),
  version           INTEGER NOT NULL,
  params            JSONB NOT NULL,
  fit_window_start  DATE NOT NULL,
  fit_window_end    DATE NOT NULL,
  oos_window_start  DATE NOT NULL,
  oos_window_end    DATE NOT NULL,
  oos_expectancy_r  NUMERIC(8,4) NOT NULL,
  oos_n_trades      INTEGER NOT NULL,
  status            TEXT NOT NULL,       -- 'champion','challenger','retired'
  promoted_at       TIMESTAMPTZ,
  retired_at        TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (strategy_id, version)
);

-- Meta-learning on human behavior
CREATE TABLE decision_audit (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id          UUID REFERENCES trade_requests(id),
  hour_of_day         SMALLINT NOT NULL,
  minutes_since_open  INTEGER NOT NULL,
  consecutive_losses  INTEGER NOT NULL,
  seconds_since_last_exit INTEGER,
  thesis_length       INTEGER NOT NULL,
  followed_strategy   BOOLEAN NOT NULL,   -- false = manual override
  approver_decision   TEXT,
  approver_seconds    INTEGER,
  outcome_r           NUMERIC(8,4)
);
```

### 3.4 Anti-overfitting rules — enforce these in code

| Rule | Threshold |
|---|---|
| Minimum trades before any per-regime conclusion | 30 (set `is_significant`) |
| Minimum trades before a re-fit is allowed | 100 new since last fit |
| Re-fit frequency | Monthly maximum. Hard-block more often. |
| Out-of-sample requirement | ≥ 3 months never touched during fitting |
| Embargo between fit and OOS windows | 5 trading days (prevents leakage) |
| Challenger promotion margin | OOS expectancy must beat champion by ≥ 0.1R |
| Multiple-testing correction | Deflated Sharpe ratio when comparing >5 candidates |
| Parameter change size cap | No single parameter may move >25% in one re-fit |

And one product rule: **the app must display sample size next to every statistic.**
"41% win rate (n=17)" is honest. "41% win rate" is not. Never render a performance
number without its n.

---

## 4. LLM speed — the architecture question you're actually asking

You said the LLM needs to be very quick at analyzing real-time data. Here's the
thing that will save you a lot of pain:

**The LLM should not be in the real-time decision path at all.** Not because of
Grok — because of the task. Four reasons, all concrete:

1. **Latency.** Any LLM round trip is roughly 0.5–3 seconds. Your deterministic
   strategy engine evaluates the same bar in **under 10 milliseconds**. That's a
   100–300× difference on the one axis you said matters.
2. **Non-determinism.** Same inputs, different outputs. A non-deterministic decision
   maker cannot be backtested, which means it cannot pass the gate in §8.5, which
   means you can never know whether it works.
3. **Cost per decision.** Trivial per call, but it changes the economics of a system
   evaluating 30 symbols every minute across a 6.5-hour session — that's ~11,700
   evaluations a day.
4. **It breaks the hallucination guarantee.** §11.1 of the main spec works precisely
   *because* the reason was determined by deterministic rules and recorded before the
   model ever saw it.

Numeric threshold comparison is what code is for. Language is what the LLM is for.
Don't cross the streams.

### 4.1 The three-tier architecture

| Tier | Latency budget | What runs | Technology |
|---|---|---|---|
| **1 — Decide** | < 10 ms | Watch the market. Compute indicators, evaluate rules, fire signals, enforce risk. | numpy/pandas. No network calls. No LLM. |
| **2 — Explain** | 1–5 s, async | Turn the recorded decision into English. Select the tip. | Grok 4.6, off the hot path |
| **3 — Analyze** | Minutes, batch | Nightly and weekly: regime summaries, pattern detection across the whole dataset, "what changed this week," meta-analysis of your decisions. | Grok 4.6 with higher reasoning, over the feature store |

**This gives you the speed you want.** Signal fires in <10 ms → push notification to
your phone in <500 ms → you and your mom see the trade request essentially instantly
→ the written explanation streams in 2–4 seconds later while she's reading the risk
numbers. The decision was never waiting on the model.

Tier 3 is where an LLM genuinely earns its place: pointed at a year of accumulated
signals and outcomes, asked "what patterns show up in the losing trades that don't
show up in the winners," with a 500k context window to work in. That's a real
research assistant. Asking it to eyeball a live tick is not.

### 4.2 Grok configuration

```bash
XAI_API_KEY=
XAI_BASE_URL=https://api.x.ai/v1
EXPLANATION_MODEL=grok-4.6      # tier 2: low/no reasoning, prioritize latency
ANALYSIS_MODEL=grok-4.6         # tier 3: higher reasoning, latency irrelevant
```

Grok 4.6 supports configurable reasoning — use minimal reasoning for Tier 2 (you want
speed, and the answer is already determined) and higher reasoning for Tier 3 (you
want depth, and it runs overnight). $2/1M input, $6/1M output, 500k context. Stream
Tier 2 responses so text appears as it generates rather than after a pause.

**Tier 2 must degrade gracefully.** If the API is slow or down, the deterministic
template renders instead and the trade proceeds normally. The explanation is the
product, but it can never be allowed to block or delay a decision.

---

## 5. Revised phase order

```
Phases 1–8    (BUILD_SPEC)           paper trading system
Phase 9–11    (ADDENDUM 1)           roles, request/approve, approver tooling
Phase 11.5    ← NEW: run the §1.4 fractional test, finalize live universe
Phase 12      (ADDENDUM 1)           live reconciliation, friction calibration
Phase 13      ← NEW: feature store, regime tagging, per-regime performance
Phase 14      ← NEW: Tier 3 batch analysis, weekly research report
Phase 15      ← NEW: champion/challenger re-fit pipeline (only after ~100 trades)
Phase 16      POC evaluation against POC_CRITERIA.yaml
              → only then consider Alpaca live automation
```

Phase 13 is worth starting early — the feature store is just "write everything down,"
and every month you delay it is a month of data you don't have.

---

## Sources

- [Robinhood — Settlement and buying power](https://robinhood.com/us/en/support/articles/settlement-and-buying-power)
- [Robinhood — Investing accounts (margin vs cash)](https://robinhood.com/us/en/support/articles/robinhood-accounts/)
- [Robinhood — Fractional shares](https://robinhood.com/us/en/support/articles/fractional-shares/)
- [robin_stocks issue #452 — "Limit order quantity cannot include fractional shares"](https://github.com/jmfernandes/robin_stocks/issues/452)
- [E*TRADE — Pattern Day Trader rule change, effective June 4 2026](https://us.etrade.com/knowledge/library/margin/pattern-day-trading-rule-change)
- [What is the Robinhood API? (unofficial status, crypto-only official API)](https://apidog.com/blog/robinhood-api/)
- [FINRA Information Notice — Section 31 fee rate, effective April 4 2026](https://www.finra.org/sites/default/files/2026-03/Information-Notice-20260317.pdf)
- [xAI — models and pricing](https://docs.x.ai/docs/models)
- [Alpaca — Trading API documentation](https://docs.alpaca.markets/us/docs/trading-api)
