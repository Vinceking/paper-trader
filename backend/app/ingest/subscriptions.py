"""Bounded, prioritized websocket subscription manager.

BUILD_SPEC §2.4: the Alpaca free tier caps concurrent websocket symbol
subscriptions (currently 30). The watchlist must therefore be a bounded,
prioritized set that can hot-swap symbols without dropping the socket.

Pure logic, no I/O — the caller applies the returned diff to the real stream.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubscriptionDiff:
    subscribe: tuple[str, ...]
    unsubscribe: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.subscribe and not self.unsubscribe


class SubscriptionManager:
    """Holds the active symbol set under a hard cap.

    Priority is explicit and lower is better. Symbols with open positions must
    always be priority 0 — dropping the feed for a symbol you are holding is the
    worst possible way to spend the cap.
    """

    def __init__(self, max_symbols: int = 30):
        if max_symbols < 1:
            raise ValueError("max_symbols must be >= 1")
        self.max_symbols = max_symbols
        self._priorities: dict[str, int] = {}
        self._active: set[str] = set()

    @property
    def active(self) -> frozenset[str]:
        return frozenset(self._active)

    def set_desired(self, symbols: dict[str, int]) -> SubscriptionDiff:
        """Replace the desired set. `symbols` maps symbol -> priority (lower wins).

        Ties break alphabetically so the result is deterministic and testable.
        """
        normalized = {s.upper(): p for s, p in symbols.items()}
        self._priorities = normalized

        ranked = sorted(normalized.items(), key=lambda kv: (kv[1], kv[0]))
        desired = {s for s, _ in ranked[: self.max_symbols]}

        to_add = desired - self._active
        to_drop = self._active - desired
        self._active = desired

        return SubscriptionDiff(
            subscribe=tuple(sorted(to_add)),
            unsubscribe=tuple(sorted(to_drop)),
        )

    def pin(self, symbol: str) -> SubscriptionDiff:
        """Force a symbol to top priority (e.g. a position was just opened)."""
        merged = dict(self._priorities)
        merged[symbol.upper()] = 0
        return self.set_desired(merged)

    def release(self, symbol: str) -> SubscriptionDiff:
        """Drop a symbol from the desired set (e.g. a position was closed)."""
        merged = dict(self._priorities)
        merged.pop(symbol.upper(), None)
        return self.set_desired(merged)

    def would_evict(self, symbols: dict[str, int]) -> tuple[str, ...]:
        """Preview which active symbols a desired set would evict. No mutation."""
        normalized = {s.upper(): p for s, p in symbols.items()}
        ranked = sorted(normalized.items(), key=lambda kv: (kv[1], kv[0]))
        desired = {s for s, _ in ranked[: self.max_symbols]}
        return tuple(sorted(self._active - desired))
