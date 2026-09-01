"""Subscription manager tests — the 30-symbol free-tier cap. BUILD_SPEC §2.4."""

from __future__ import annotations

from app.ingest.subscriptions import SubscriptionManager


class TestSubscriptionManager:
    def test_subscribes_within_cap(self):
        m = SubscriptionManager(max_symbols=3)
        diff = m.set_desired({"SPY": 0, "QQQ": 1})
        assert diff.subscribe == ("QQQ", "SPY")
        assert diff.unsubscribe == ()
        assert m.active == frozenset({"SPY", "QQQ"})

    def test_enforces_cap_by_priority(self):
        m = SubscriptionManager(max_symbols=2)
        m.set_desired({"SPY": 0, "QQQ": 1, "XLF": 2})
        assert m.active == frozenset({"SPY", "QQQ"})

    def test_ties_break_alphabetically_for_determinism(self):
        m = SubscriptionManager(max_symbols=2)
        m.set_desired({"XLF": 5, "AAA": 5, "ZZZ": 5})
        assert m.active == frozenset({"AAA", "XLF"})

    def test_hot_swap_produces_minimal_diff(self):
        """Changing the watchlist must not churn symbols that stay subscribed."""
        m = SubscriptionManager(max_symbols=3)
        m.set_desired({"SPY": 0, "QQQ": 1, "XLF": 2})
        diff = m.set_desired({"SPY": 0, "QQQ": 1, "XLE": 2})
        assert diff.subscribe == ("XLE",)
        assert diff.unsubscribe == ("XLF",)

    def test_pin_evicts_lowest_priority(self):
        m = SubscriptionManager(max_symbols=2)
        m.set_desired({"SPY": 1, "QQQ": 2})
        diff = m.pin("XLE")            # opening a position in XLE
        assert "XLE" in diff.subscribe
        assert "QQQ" in diff.unsubscribe
        assert "SPY" in m.active

    def test_release_frees_a_slot(self):
        m = SubscriptionManager(max_symbols=2)
        m.set_desired({"SPY": 0, "QQQ": 1})
        diff = m.release("QQQ")
        assert diff.unsubscribe == ("QQQ",)
        assert m.active == frozenset({"SPY"})

    def test_would_evict_previews_without_mutating(self):
        m = SubscriptionManager(max_symbols=2)
        m.set_desired({"SPY": 0, "QQQ": 1})
        preview = m.would_evict({"SPY": 0, "XLE": 1})
        assert preview == ("QQQ",)
        assert m.active == frozenset({"SPY", "QQQ"})   # unchanged

    def test_symbols_normalized_to_uppercase(self):
        m = SubscriptionManager(max_symbols=5)
        m.set_desired({"spy": 0})
        assert m.active == frozenset({"SPY"})

    def test_no_change_is_empty_diff(self):
        m = SubscriptionManager(max_symbols=5)
        m.set_desired({"SPY": 0})
        assert m.set_desired({"SPY": 0}).is_empty
