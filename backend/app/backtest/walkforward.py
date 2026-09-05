"""Walk-forward train/test splitting. BUILD_SPEC §8.5 rule 4.

"Parameter sweeps must be walk-forward, not a single global optimization. A
single optimization over the whole history is curve fitting, and it will
produce a beautiful equity curve that means nothing."

This module is deliberately dumb: it only knows how to slice a
chronologically-ordered bar list into consecutive (train, test) windows. It
has no opinion on what a "good" parameter set is or how many bars a fold
should have — that's `app.backtest.sweep`'s job. Keeping the split itself
this simple makes the one property that actually matters easy to see by
inspection and easy to unit test: **test always comes strictly after train,
and the two never overlap** — so nothing evaluated on `test` was available
when parameters were chosen on `train`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ingest.bars import FinalBar


@dataclass(frozen=True)
class WalkForwardSplit:
    """One fold: `train` (in-sample) strictly precedes `test` (out-of-sample)."""

    train: list[FinalBar]
    test: list[FinalBar]


def rolling_splits(
    bars: list[FinalBar],
    train_size: int,
    test_size: int,
    step: int | None = None,
) -> list[WalkForwardSplit]:
    """Slice `bars` (oldest first) into rolling (train, test) windows.

    Fold 0 is `bars[0:train_size]` / `bars[train_size:train_size+test_size]`.
    Each subsequent fold advances by `step` bars (default: `test_size`, i.e.
    non-overlapping test windows -- the classic walk-forward roll). A fold
    is only included if both its train and test windows are completely
    full; a trailing partial window is dropped rather than silently
    evaluated on less data than requested.
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    step = test_size if step is None else step
    if step <= 0:
        raise ValueError("step must be positive")

    splits: list[WalkForwardSplit] = []
    start = 0
    while True:
        train_end = start + train_size
        test_end = train_end + test_size
        if test_end > len(bars):
            break
        splits.append(
            WalkForwardSplit(train=bars[start:train_end], test=bars[train_end:test_end])
        )
        start += step
    return splits


def single_holdout_split(bars: list[FinalBar], test_size: int) -> WalkForwardSplit:
    """The simplest valid walk-forward split: the most recent `test_size`
    bars are held out as the out-of-sample test window; everything before
    them is in-sample. Used when there isn't enough history for multiple
    rolling folds but a single honest train/test boundary is still required
    (BUILD_SPEC §8.5 rule 4 -- this is NOT "a single global optimization"
    because the test window is still never touched during parameter
    selection; it is simply one fold instead of several).
    """
    if test_size <= 0 or test_size >= len(bars):
        raise ValueError("test_size must be positive and less than len(bars)")
    return WalkForwardSplit(train=bars[:-test_size], test=bars[-test_size:])
