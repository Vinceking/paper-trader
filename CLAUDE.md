# Project rules — read before every task

This is a **paper trading** application with a supervised two-role live mode.
Read `BUILD_SPEC.md` for the core design and `ADDENDUM_LIVE_APPROVAL.md` for the
requester/approver live path.

## Non-negotiable constraints

1. **The app never places a real order.** The only `Broker` implementation is
   `PaperBroker`, pointed at `https://paper-api.alpaca.markets`. Do not write a live
   broker driver. Do not add, store, or request brokerage credentials for any live
   account — not as an env var, not encrypted, not "temporarily." Live trades are
   placed manually by the human approver in their own broker app and entered back
   into the system as `live_executions` rows. The app refuses to boot if
   `ENABLE_LIVE_TRADING != false` or if the base URL lacks `paper-api`.

1b. **Approver-only actions are enforced server-side.** Approve, decline, enter fill,
   and edit `live_limits` return 403 for a requester JWT. UI hiding is not
   enforcement. This boundary has an integration test; do not weaken it.

2. **Reasons are logged at decision time, never reconstructed.** The `signals` row
   — rule, all conditions with thresholds and actual values, full feature snapshot
   — is written *before* any order is submitted. The explanation layer translates
   that record into English. It never receives raw price history and never infers
   a cause. See BUILD_SPEC §11.1.

3. **Friction is always applied.** Backtest and live paper use the same friction
   code path. There is no clean/frictionless mode. See §9.

4. **No lookahead, ever.** Indicators at time `t` use only bars `<= t`. Signals on
   bar `t` execute at `t+1` open. Strategies evaluate only on finalized bars.

5. **The backtest gate is enforced.** `strategies.enabled` cannot be set true
   unless `gate_passed` is true. Return 409 otherwise. See §8.5.

6. **Every entry defines its stop.** A strategy that returns an entry signal with
   `stop_price = None` is rejected by the risk engine. Position size is derived
   from stop distance.

7. **The Reality Ledger is not optional or hideable.** Benchmark comparison against
   SPY buy-and-hold ships in the same phase as the analytics it accompanies. Do not
   soften its copy.

## Working style

- **One phase per session.** Phases are defined in BUILD_SPEC §16. Do not scaffold
  ahead. Do not start Phase N+1 in a Phase N session.
- **Write the tests from the acceptance criteria first**, then implement until they
  pass.
- Prefer boring, explicit code over clever abstractions. This codebase is meant to
  be read and understood by its author.
- Any deviation from BUILD_SPEC: say so explicitly and explain why, rather than
  quietly implementing something different.

## Stack quick reference

Python 3.12 · FastAPI · PostgreSQL + TimescaleDB · Redis · alpaca-py ·
pandas-ta-classic · VectorBT + Backtrader · React + Vite + TS · Expo · Tailwind ·
lightweight-charts

Three processes: `api`, `ingest` (single instance, owns the data socket), `worker`.
