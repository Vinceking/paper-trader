"""The paper-only safety rails. CLAUDE.md rule 1, BUILD_SPEC §15.

If these tests ever go green while the rails are removed, the whole design is
void. Treat a failure here as a stop-the-line event, not a flaky test.
"""

from __future__ import annotations

import pytest

from app.config import Settings


class TestPaperOnlyRails:
    def test_defaults_are_paper(self):
        s = Settings(_env_file=None)
        assert s.enable_live_trading is False
        assert "paper-api" in s.alpaca_paper_base_url

    def test_refuses_to_boot_with_live_trading_enabled(self):
        with pytest.raises(RuntimeError, match="never places real"):
            Settings(_env_file=None, enable_live_trading=True)

    def test_refuses_non_paper_base_url(self):
        with pytest.raises(RuntimeError, match="not a paper endpoint"):
            Settings(_env_file=None, alpaca_paper_base_url="https://api.alpaca.markets")

    def test_refuses_live_url_even_when_flag_is_false(self):
        """Both rails are independent; neither alone is sufficient."""
        with pytest.raises(RuntimeError):
            Settings(
                _env_file=None,
                enable_live_trading=False,
                alpaca_paper_base_url="https://api.alpaca.markets/v2",
            )

    def test_rejects_unknown_data_feed(self):
        with pytest.raises(Exception):
            Settings(_env_file=None, alpaca_data_feed="nasdaq_totalview")

    def test_iex_is_the_free_tier_default(self):
        assert Settings(_env_file=None).alpaca_data_feed == "iex"
