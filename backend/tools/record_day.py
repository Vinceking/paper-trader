"""Record a live market day to a replay file.

    python tools/record_day.py --symbols SPY,QQQ,XLF --out recordings/2026-08-31.ndjson

Run this on the first day you have working Alpaca keys, during market hours
(09:30-16:00 ET). One recorded session unlocks deterministic offline testing for
the entire rest of the project — replay it with:

    python tools/smoke_replay.py            # synthetic
    ReplaySource("recordings/....ndjson")   # real

Requires alpaca-py:  pip install alpaca-py
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog

from app.config import get_settings
from app.ingest.alpaca_source import AlpacaSource
from app.ingest.replay import Recorder
from app.ingest.stream import StreamRunner

log = structlog.get_logger(__name__)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ,XLF")
    ap.add_argument("--out", default="recordings/session.ndjson")
    ap.add_argument("--minutes", type=int, default=0,
                    help="stop after N minutes (0 = run until interrupted)")
    args = ap.parse_args()

    settings = get_settings()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if len(symbols) > settings.max_stream_symbols:
        raise SystemExit(
            f"{len(symbols)} symbols exceeds the free-tier cap of "
            f"{settings.max_stream_symbols}"
        )

    out = Path(args.out)
    source = AlpacaSource(
        settings.alpaca_api_key, settings.alpaca_api_secret, settings.alpaca_data_feed
    )
    recorder = Recorder(source, out)

    count = 0

    def handle(_msg) -> None:
        nonlocal count
        count += 1
        if count % 500 == 0:
            log.info("recording", messages=count, file=str(out))

    runner = StreamRunner(source=recorder, on_message=handle)
    task = asyncio.create_task(runner.run(symbols))

    try:
        if args.minutes:
            await asyncio.sleep(args.minutes * 60)
            runner.stop()
        await task
    except KeyboardInterrupt:
        runner.stop()

    print(f"\nrecorded {count} messages to {out}")
    if source.dropped:
        print(f"WARNING: dropped {source.dropped} messages (consumer fell behind)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
