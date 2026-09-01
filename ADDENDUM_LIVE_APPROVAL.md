# Addendum — Two-Role Live Mode (Requester / Approver)

**Supplements `BUILD_SPEC.md` v1.0. Read that first.**
**Status:** v1.1
**Change:** adds a supervised live-money path where a minor requests trades and an
adult account holder approves and executes them.

---

## 1. What changed and why it's allowed now

The blocker in v1.0 was never "trading is forbidden." It was two specific things:

1. A minor cannot legally hold a brokerage account or place orders.
2. An adult acting on unvalidated signals is exposed to a system nobody has tested.

The two-role design fixes (1) properly — your mom has legal capacity, it is her
account, and she makes the decision. It *partially* fixes (2), and §4 is entirely
about making sure it fully does.

At $50–100 per position, the money at risk is bounded to something a parent can
deliberately choose to spend on their kid's education. That's a legitimate call for
her to make.

### The one thing that still has to be true

**The approval must be a real decision, not a tap.** If your mom ends up glancing
at "BUY AAPL — approve?" and tapping yes, then the architecture is theater and
we're back where we started, except now it *feels* supervised. Everything in §4
exists to prevent that specific failure, which is the single most likely way this
project goes wrong.

---

## 2. Architecture decision: the app never touches a broker

**The app has no broker API integration in live mode. None. Your mom executes
manually in her own broker app.**

This is not caution — it's the better engineering, for four concrete reasons:

1. **No credentials.** You never store her brokerage login or API keys. There is no
   version of this project where storing a parent's brokerage credentials in a
   teenager's side project is a good idea. Removing the capability removes the risk
   permanently.
2. **No runaway-loop risk.** A bug in an app with order-placement rights can submit
   500 orders in a second. A bug in an app with no order-placement rights sends 500
   notifications, which is annoying and costs nothing.
3. **The approval stays real.** She has to open her broker and type the order. That
   physical friction is the thing that keeps her actually deciding.
4. **Zero regulatory surface.** A tool that shows analysis to its own users, who then
   act in their own accounts, is a calculator. Automated order routing on someone
   else's behalf is a different category entirely.

### Flow

```
YOU (requester, 15)                    MOM (approver, 18+)
        │                                       │
  strategy fires ──► signal + evidence          │
        │                                       │
  you review, tap REQUEST                       │
        │                                       │
        ├──── push notification ───────────────►│
        │                                       │
        │                              reviews REQUEST CARD:
        │                                 · plain-English reason
        │                                 · dollar risk + stop
        │                                 · your 30-day track record
        │                                 · paper vs. live divergence
        │                                       │
        │                              APPROVE / DECLINE / ASK
        │                                       │
        │◄──── decision + her note ─────────────┤
        │                                       │
        │                              places order in Robinhood/Fidelity
        │                                       │
        │◄──── enters actual fill price ────────┤
        │                                       │
  app records ACTUAL fill, runs the same
  friction/journal/explanation pipeline
        │
  explanation + tip + benchmark delta
```

Every request is logged whether or not it was approved. **Declined requests are the
highest-value data in the app** — the app tracks how those trades would have gone,
so over time you both learn whether her vetoes were saving money or costing it.
That single feature turns her from a gatekeeper into a participant.

---

## 3. Data model additions

```sql
-- Roles ---------------------------------------------------------------
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'requester';
  -- 'requester' | 'approver'

CREATE TABLE households (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              TEXT NOT NULL,
  requester_id      UUID NOT NULL REFERENCES users(id),
  approver_id       UUID NOT NULL REFERENCES users(id),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (requester_id <> approver_id)
);

-- Limits the APPROVER sets. The requester can view but never edit.
CREATE TABLE live_limits (
  household_id            UUID PRIMARY KEY REFERENCES households(id),
  max_position_usd        NUMERIC(10,2) NOT NULL DEFAULT 100.00,
  max_open_positions      INTEGER NOT NULL DEFAULT 2,
  max_requests_per_day    INTEGER NOT NULL DEFAULT 3,
  max_daily_loss_usd      NUMERIC(10,2) NOT NULL DEFAULT 50.00,
  max_weekly_loss_usd     NUMERIC(10,2) NOT NULL DEFAULT 100.00,
  total_capital_usd       NUMERIC(10,2) NOT NULL DEFAULT 500.00,
  require_paper_first     BOOLEAN NOT NULL DEFAULT true,
  min_paper_trades        INTEGER NOT NULL DEFAULT 50,
  halt_until              TIMESTAMPTZ,
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by              UUID NOT NULL REFERENCES users(id)
);

-- Audit trail on limit changes. Append-only, never deleted.
CREATE TABLE live_limit_changes (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id    UUID NOT NULL REFERENCES households(id),
  changed_by      UUID NOT NULL REFERENCES users(id),
  field           TEXT NOT NULL,
  old_value       TEXT,
  new_value       TEXT,
  changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The request/approval record -----------------------------------------
CREATE TABLE trade_requests (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id        UUID NOT NULL REFERENCES households(id),
  requested_by        UUID NOT NULL REFERENCES users(id),
  signal_id           UUID REFERENCES signals(id),   -- null for manual requests
  symbol              TEXT NOT NULL,
  side                TEXT NOT NULL,
  intent              TEXT NOT NULL,                 -- 'entry' | 'exit'
  notional_usd        NUMERIC(10,2) NOT NULL,
  qty                 NUMERIC(18,6) NOT NULL,
  ref_price           NUMERIC(18,4) NOT NULL,        -- quote at request time
  stop_price          NUMERIC(18,4) NOT NULL,        -- REQUIRED, no exceptions
  target_price        NUMERIC(18,4),
  dollar_risk         NUMERIC(10,2) NOT NULL,        -- (ref - stop) * qty
  -- the requester must write this themselves; not generated
  requester_thesis    TEXT NOT NULL,
  -- generated from the recorded signal evidence
  explanation_id      UUID REFERENCES explanations(id),
  status              TEXT NOT NULL DEFAULT 'pending',
    -- 'pending','approved','declined','expired','cancelled'
  requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at          TIMESTAMPTZ NOT NULL,          -- default +10 min
  decided_at          TIMESTAMPTZ,
  decided_by          UUID REFERENCES users(id),
  approver_note       TEXT,
  decline_reason      TEXT,
  -- shadow tracking: what happened anyway (see §5)
  shadow_exit_price   NUMERIC(18,4),
  shadow_pnl          NUMERIC(10,2),
  shadow_closed_at    TIMESTAMPTZ
);
CREATE INDEX ON trade_requests (household_id, requested_at DESC);
CREATE INDEX ON trade_requests (status) WHERE status = 'pending';

-- What actually happened in the real broker account --------------------
CREATE TABLE live_executions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id          UUID NOT NULL REFERENCES trade_requests(id),
  entered_by          UUID NOT NULL REFERENCES users(id),  -- approver only
  broker              TEXT NOT NULL,                  -- 'robinhood','fidelity',...
  actual_qty          NUMERIC(18,6) NOT NULL,
  actual_fill_price   NUMERIC(18,4) NOT NULL,
  actual_filled_at    TIMESTAMPTZ NOT NULL,
  -- the gap between what the app assumed and what really happened
  slippage_vs_ref     NUMERIC(18,4) NOT NULL,
  fees_reported       NUMERIC(10,4) NOT NULL DEFAULT 0,
  notes               TEXT,
  entered_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Authorization rules — enforce server-side, not in the UI

| Action | Requester | Approver |
|---|---|---|
| Create trade request | ✅ | ✅ |
| Approve / decline request | ❌ **403** | ✅ |
| Enter actual fill | ❌ **403** | ✅ |
| Edit `live_limits` | ❌ **403** | ✅ |
| View everything | ✅ | ✅ |
| Halt live mode | ✅ (either can) | ✅ |

Write an integration test that asserts a requester JWT receives 403 on every
approver-only endpoint. This is the one authorization boundary that matters — if
it leaks, the whole design is void.

---

## 4. Making the approval real

This section is the point of the addendum. A rubber stamp is the failure mode.

### 4.1 The approver's request card must show

Not just "BUY AAPL $75." All of this, every time:

```
┌────────────────────────────────────────────────┐
│  BUY  AAPL  ·  1.4 shares  ·  $74.60          │
│                                                │
│  WHY                                           │
│  Price is 2.34 std below session VWAP while    │
│  above the 200-day EMA, on 1.31x normal        │
│  volume. The strategy's rule is to buy that    │
│  stretch and exit at the VWAP touch.           │
│                                                │
│  WHAT IT RISKS                                 │
│  Stop at $52.10  →  you lose $8.40 if wrong    │
│  Target $54.90   →  you make $12.60 if right   │
│                                                │
│  VINNY'S TRACK RECORD (this strategy)          │
│  Paper: 74 trades · 41% win · −0.06R expectancy│
│  Live:   6 trades · 2 wins · −$14.20 net       │
│  ⚠ This strategy is NEGATIVE over 74 paper     │
│    trades. It has not proven an edge.          │
│                                                │
│  TODAY                                         │
│  Request 2 of 3 · $18 of $50 daily loss used   │
│                                                │
│  HIS REASONING (he wrote this)                 │
│  "VWAP snapback, trend filter is up, volume    │
│   confirms. Same setup as Tuesday's winner."   │
│                                                │
│  [ DECLINE ]  [ ASK A QUESTION ]  [ APPROVE ]  │
└────────────────────────────────────────────────┘
```

**Mandatory elements:**

- **The strategy's own track record, stated bluntly**, including the warning line
  when expectancy is negative. Do not hide or soften this. If the strategy is
  losing, her card says so on every single request.
- **Dollar risk in dollars**, never percentages or R-multiples. "$8.40" is a
  number a parent can evaluate; "1R" is not.
- **`requester_thesis`, typed by you, every time.** No template, no autofill,
  minimum 20 characters. Writing down why you want a trade before you place it is
  the highest-value habit in this whole project, and it also gives her something
  real to evaluate.
- **Budget consumed today**, so she can see the pattern, not just the instance.

### 4.2 `ASK A QUESTION` — build this, don't skip it

She types a question; it goes back to you; you answer; the request stays pending
(and its expiry extends by 5 minutes). This turns approvals into conversations,
which is the entire educational point of the setup. It is also the single best
defense against rubber-stamping.

### 4.3 Anti-rubber-stamp mechanics

Build all five:

1. **Requests expire in 10 minutes.** An expired request cannot be revived — you
   must submit a new one at the current price. This prevents her approving stale
   ideas hours later, and it prevents you from nagging.
2. **Approval requires scrolling the card to the bottom.** Track it client-side;
   the APPROVE button stays disabled until the risk section has been on screen.
3. **Weekly approver digest** (email, Sunday): every request, her decision, the
   outcome, and — critically — how the **declined** ones would have gone. If her
   declines are consistently saving money, she should see that. If they're
   consistently costing money, she should see that too.
4. **Fast-approval nudge.** If median decision time over the last 10 requests drops
   below 15 seconds, the next card shows: *"You've been approving in under 15
   seconds. Requests are meant to be evaluated, not confirmed."* Log it as a
   `risk_event`.
5. **Either party can halt live mode instantly.** One button, no confirmation, sets
   `halt_until`. Un-halting is approver-only and requires a 24-hour wait. Easy to
   stop, deliberately slow to restart.

### 4.4 Automatic halts — server-enforced

Live requests are blocked, no override, when any of these is true:

- `max_daily_loss_usd` or `max_weekly_loss_usd` reached
- `max_requests_per_day` reached
- Fewer than `min_paper_trades` completed on that strategy in paper mode
- The strategy's `gate_passed` is false (§8.5 of the main spec still applies)
- Three consecutive live losses → 24-hour cooldown
- Within 10 minutes of the open or close (worst spreads, worst fills)
- `halt_until > now()`

---

## 5. Shadow tracking — the feature that makes this worth building

For **every** request — approved, declined, or expired — the app continues to track
what the trade would have done, using the recorded stop and target, until it
resolves. Stored in `trade_requests.shadow_*`.

This gives you four learning quadrants instead of one:

| | Approved | Declined |
|---|---|---|
| **Would have won** | You earned it | Her caution cost you $X |
| **Would have lost** | Shared lesson | Her caution saved you $X |

After 50 requests you'll both have a real answer to "is Mom's judgment adding
value?" — which is a far more interesting question than "did the trade work," and
one almost no trading app can answer.

Surface it as an **Approver Scorecard**: decline accuracy, dollars saved vs. missed,
and average decision time.

---

## 6. Reconciling reality

The app assumes a fill price. Your mom's broker gives a real one. **The gap between
those two numbers is the most educational data you will ever collect**, because it
is exactly the friction that made all those day-trading studies come out the way
they did.

**Flow:** after she places the order, the app prompts her for actual quantity, fill
price, and time. `slippage_vs_ref` is computed and stored. Prompt again on the exit.

**Then build a calibration report** (weekly): mean and distribution of
`slippage_vs_ref`, compared against what your friction model in §9 predicted. If
your model is optimistic, **tune the model to match reality** — do not tune reality
to match the model. Once calibrated, your paper results finally mean something,
because they're using measured friction instead of guessed friction.

This closes the loop between the paper app and the real world, and it is the single
most valuable thing the live mode contributes.

---

## 7. Grok integration

Fine — it's a swap, not a rewrite. The `explainer` module already isolates this.

```python
# app/education/llm.py
from openai import AsyncOpenAI   # xAI is OpenAI-SDK compatible

class LLMProvider(Protocol):
    async def explain(self, payload: dict) -> Explanation: ...

class GrokProvider:
    def __init__(self, api_key: str, model: str = "grok-4.6"):
        self.client = AsyncOpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        self.model = model
```

```bash
XAI_API_KEY=
XAI_BASE_URL=https://api.x.ai/v1
EXPLANATION_MODEL=grok-4.6
LLM_PROVIDER=grok
```

**Current pricing:** Grok 4.6 is $2.00 per 1M input tokens / $6.00 per 1M output,
500k context. At ~2k in / 400k out per explanation, a few hundred explanations a
month runs roughly $1–3. Negligible either way.

**Keep the provider abstraction regardless.** Two reasons: you'll want to A/B the
explanations, and if one provider is down you still get journal entries.

### The constraint that matters more than the model choice

§11.1 of the main spec is not optional and does not get easier with a better model.
The LLM receives **only the recorded signal evidence** — rule, conditions with
thresholds and actual values, feature snapshot, outcome. It never receives raw price
history and is never asked to *determine* why something happened. It translates a
reason that was already recorded at decision time.

Every model will happily invent a confident, fluent, plausible market narrative if
you let it. That is not a knock on Grok specifically — it's the nature of the task.
The architecture is what prevents it, not the model.

**Keep the golden-file test from Phase 5:** assert the explanation output contains
no number that wasn't in the input payload.

---

## 8. Things you need to know before the first live trade

Short section, said once, all practical:

**Settlement.** If your mom uses a **cash account**, T+1 settlement applies: selling
and re-buying with the same unsettled money causes violations. Freeriding is **one
violation → 90-day restriction**. Good faith violations: three in 12 months → same.
With $500 total capital, that limit binds fast. If she has a **margin account**, this
doesn't apply — and note the PDT rule was eliminated on June 4, 2026, so the old
$25k day-trade minimum is gone. Either way, she should know which account type she
has *before* the first trade, not after a restriction letter.

**Taxes.** Every sale is a taxable event on **her** return. Short-term gains are
taxed as ordinary income at her marginal rate — there is no favorable long-term rate
under one year. Her broker issues a 1099-B. Wash-sale rules can disallow losses if
you re-buy the same security within 30 days, which is easy to trip when day trading
one symbol. This is a genuine reason to keep the trade count low, and it's the kind
of thing people discover in April. Track realized P&L in the app so she isn't
surprised.

**The math on $50–100.** Be clear-eyed: a genuinely excellent day trade on a $75
position might net $0.75. A great month might be $15. That is not income, and the
app should never present it as though it were — show percentages next to dollars,
and keep the Reality Ledger (§12) prominent in live mode too. But as *tuition*, it's
outstanding: you're buying real, calibrated, emotionally-real market data about your
own decision-making for less than the cost of a video game. The learning is the
return. Build it as though that's the point, because it is.

**The thing to watch for in yourself.** The failure mode isn't losing $50. It's the
$50 working, twice, and the position sizes creeping up. `max_position_usd` lives in
`live_limits` and only your mom can change it. That's on purpose. If you find
yourself wanting to argue it upward after a good week, that impulse is the single
most useful thing this whole app could ever teach you to notice — and the app should
literally log it: any requested limit increase gets recorded in
`live_limit_changes` and shown in the weekly digest, with the account's performance
at the time of the request.

---

## 9. Revised build phases

Phases 1–8 from the main spec are unchanged. Live mode comes **after** all of them.

### Phase 9 — Roles and authorization
Households, roles, JWT claims, server-side authorization on every approver-only
endpoint.

✅ Integration test: requester JWT gets 403 on approve, fill-entry, and limits
   endpoints.
✅ A user cannot be requester and approver in the same household.

### Phase 10 — Request / approve loop (still paper-backed)
Full request → notify → card → approve/decline/ask → record loop, **still executing
against the paper broker.** Run it this way for at least two weeks.

✅ Requests expire at 10 minutes and cannot be revived.
✅ ASK A QUESTION round-trips and extends expiry.
✅ Scroll-to-approve enforced.
✅ Declined requests are shadow-tracked to resolution.

### Phase 11 — Approver tooling
Limits UI, weekly digest email, Approver Scorecard, fast-approval nudge, halt button.

✅ Digest shows declined-request outcomes.
✅ Either party can halt; un-halt is approver-only with a 24h delay.

### Phase 12 — Live reconciliation
Manual fill entry, `slippage_vs_ref`, calibration report, friction model tuning.

✅ Friction model is re-fit from at least 20 measured real fills.
✅ Paper mode uses the calibrated parameters afterward.

### Gate into live money — all must be true

- [ ] Phases 1–11 complete and running
- [ ] ≥ 50 paper trades on the specific strategy being used
- [ ] That strategy passed the §8.5 backtest gate
- [ ] Two weeks of paper-backed request/approve with a **median approval time over
      30 seconds** (proof the loop isn't already a rubber stamp)
- [ ] Your mom has opened `live_limits` herself and set every value deliberately
- [ ] Both of you can state, out loud, what happens if the first five trades all lose

That last one isn't a joke. Answer it before you need to.

---

## 10. What I'd still do differently

You've got a genuinely defensible setup here and I'd build it. One suggestion that
costs nothing:

Run **two accounts side by side.** The $500 day-trading pot with all of the above,
and a second $500 that just buys an index fund on the first of every month and is
never touched. Same start date, same chart, shown next to each other in the Reality
Ledger.

In a year one of those lines will be higher. Neither of us knows which — that's
genuinely what makes it worth doing. But whichever way it lands, you'll have
answered the question with your own money and your own data instead of taking
anyone's word for it, which is worth considerably more than $500.

---

## Sources

- [xAI API overview — base URL and OpenAI compatibility](https://docs.x.ai/docs/overview)
- [xAI models and pricing](https://docs.x.ai/docs/models)
- [Fidelity — Avoiding cash trading violations (T+1, freeriding, good faith)](https://www.fidelity.com/learning-center/trading-investing/trading/avoiding-cash-trading-violations)
- [Schwab — Trading in cash accounts](https://www.schwab.com/learn/story/avoid-these-violations-when-trading-cash)
- [E*TRADE — Pattern Day Trader rule change, effective June 4 2026](https://us.etrade.com/knowledge/library/margin/pattern-day-trading-rule-change)
- [IRS — Topic 409, Capital Gains and Losses](https://www.irs.gov/taxtopics/tc409)
- [IRS — Wash sales (Publication 550)](https://www.irs.gov/publications/p550)
