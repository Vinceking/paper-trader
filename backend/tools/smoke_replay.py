"""End-to-end Phase 1 smoke test — no database, no network, no API keys.

Generates a synthetic recording with a deliberate 3-minute hole, replays it
through the real StreamRunner + BarBuilder pipeline, and verifies that bars are
produced and the gap is detected.

    python tools/smoke_replay.py

This is the offline proof that the data spine works. Once you have Alpaca keys,
record a real day with tools/record_day.py and replay that instead.
"""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.ingest.bars import BarBuilder, detect_gaps
from app.ingest.replay import ReplaySource
from app.ingest.stream import StreamRunner, TradeMsg

SYMBOL = "XLF"
SESSION_START = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
MINUTES = 30
GAP_AT, GAP_LEN = 12, 3  # simulate a 3-minute feed outage


def generate_recording(path: Path) -> int:
    rng = random.Random(42)
    price = Decimal("52.00")
    written = 0

    with path.open("w", encoding="utf-8") as fh:
        for minute in range(MINUTES):
            if GAP_AT <= minute < GAP_AT + GAP_LEN:
                continue  # the hole
            bar_open = SESSION_START + timedelta(minutes=minute)
            for tick in range(rng.randint(3, 8)):
                price += Decimal(str(round(rng.uniform(-0.05, 0.05), 2)))
                ts = bar_open + timedelta(seconds=rng.randint(0, 59))
                fh.write(json.dumps({
                    "t": "trade", "symbol": SYMBOL, "ts": ts.isoformat(),
                    "price": str(price), "size": rng.randint(1, 500),
                }) + "\n")
                written += 1
    return written


async def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "session.ndjson"
    n = generate_recording(tmp)
    print(f"generated {n} synthetic trades -> {tmp}")

    builder = BarBuilder(SYMBOL, "1Min", grace_seconds=2.0)
    finalized: list = []

    def handle(msg) -> None:
        if isinstance(msg, TradeMsg):
            bar = builder.on_trade(msg.ts, msg.price, msg.size)
            if bar is not None:
                finalized.append(bar)

    runner = StreamRunner(
        source=ReplaySource(tmp, speed=0.0),
        on_message=handle,
        on_reconnect=lambda: print("  reconnect hook fired -> would backfill here"),
    )
    await runner.run([SYMBOL], max_reconnects=0)

    # Flush the last working bar the way the real service does at session end.
    tail = builder.maybe_finalize(SESSION_START + timedelta(minutes=MINUTES + 1))
    if tail:
        finalized.append(tail)

    print(f"finalized {len(finalized)} bars")
    assert finalized, "pipeline produced no bars"

    for b in finalized[:3]:
        print(f"  {b.ts:%H:%M}  O {b.open}  H {b.high}  L {b.low}  C {b.close}  "
              f"V {b.volume}  VWAP {b.vwap}")

    gaps = detect_gaps(
        SYMBOL, "1Min", [b.ts for b in finalized],
        SESSION_START, SESSION_START + timedelta(minutes=MINUTES),
    )
    print(f"detected {len(gaps)} gap(s)")
    for g in gaps:
        print(f"  {g.start:%H:%M} -> {g.end:%H:%M}  ({g.expected_bars} bars missing)")

    assert len(gaps) == 1, f"expected exactly 1 gap, got {len(gaps)}"
    assert gaps[0].expected_bars == GAP_LEN, (
        f"expected {GAP_LEN} missing bars, got {gaps[0].expected_bars}"
    )

    # Sanity: OHLC invariants hold on every bar.
    for b in finalized:
        assert b.low <= b.open <= b.high and b.low <= b.close <= b.high, b

    print("\nPASS - bars built, gap detected, OHLC invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
