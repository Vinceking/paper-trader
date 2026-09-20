"""AlpacaSource tests. app/ingest/alpaca_source.py.

Regression test for a real bug found tonight running the live ingest
process for the first time outside the replay harness: `StockDataStream`'s
`feed` parameter is typed as the `DataFeed` enum, not a plain string, and
calls `.value` on it internally when building the websocket URL. Passing
the plain "iex"/"sip" string `Settings.alpaca_data_feed` already uses
elsewhere in this codebase crashed with `AttributeError: 'str' object has
no attribute 'value'` the instant the stream tried to connect -- a class of
bug the replay-based tests (which never construct a real `StockDataStream`)
cannot catch, hence this dedicated one.

Mocks `alpaca.data.live.StockDataStream` -- no network access, no real
Alpaca credentials needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ingest.alpaca_source import AlpacaSource


class TestFeedIsConvertedToEnum:
    @pytest.mark.asyncio
    async def test_stream_passes_a_datafeed_enum_not_a_plain_string(self):
        from alpaca.data.enums import DataFeed

        source = AlpacaSource(api_key="k", api_secret="s", feed="iex")

        with patch("alpaca.data.live.StockDataStream") as mock_cls:
            mock_stream = MagicMock()
            mock_stream._run_forever = AsyncMock(return_value=None)
            mock_stream.close = AsyncMock(return_value=None)
            mock_cls.return_value = mock_stream

            import asyncio

            agen = source.stream(["SPY"])
            task = asyncio.ensure_future(agen.__anext__())
            # Nothing will ever arrive on the queue (no real socket) -- give
            # the coroutine one scheduling tick to reach the constructor
            # call (that's all this test needs), then cancel and let the
            # cancellation itself finish running inside the generator frame
            # before touching it again -- calling aclose() while the task is
            # still suspended mid-cancellation is what raised
            # "generator is already running" here.
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert mock_cls.call_count == 1
        _, kwargs = mock_cls.call_args
        assert kwargs["feed"] == DataFeed.IEX
        assert isinstance(kwargs["feed"], DataFeed)

    def test_unknown_feed_string_raises_a_clear_error_at_connect_time(self):
        source = AlpacaSource(api_key="k", api_secret="s", feed="not_a_real_feed")
        with patch("alpaca.data.live.StockDataStream"):
            agen = source.stream(["SPY"])
            with pytest.raises(ValueError):
                import asyncio
                asyncio.run(agen.__anext__())
