"""Application configuration.

Includes the hard safety rails from BUILD_SPEC.md §15: the process refuses to boot
if it is pointed at anything other than a paper trading endpoint.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = "development"

    # ---- Alpaca (PAPER ONLY) ------------------------------------------------
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"  # 'iex' on the free tier; 'sip' needs Algo Trader Plus

    # Safety rail. See BUILD_SPEC §15 and CLAUDE.md rule 1.
    enable_live_trading: bool = False

    # ---- Infrastructure -----------------------------------------------------
    database_url: str = "postgresql+asyncpg://trader:trader@localhost:5432/paper_trader"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-only-change-me"

    # ---- Market data --------------------------------------------------------
    # Free tier caps concurrent websocket symbol subscriptions at 30.
    max_stream_symbols: int = 30
    # Seconds after the minute boundary before a bar is considered final (§7.2).
    bar_finalize_grace_seconds: float = 2.0
    # Force a reconnect if no message arrives for this long during market hours.
    stream_heartbeat_timeout_seconds: int = 60
    quote_cache_ttl_seconds: int = 60

    # ---- LLM ----------------------------------------------------------------
    llm_provider: str = "grok"  # 'grok' | 'anthropic' | 'none'
    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    anthropic_api_key: str = ""
    explanation_model: str = "grok-4.6"
    analysis_model: str = "grok-4.6"
    explanation_timeout_seconds: float = 8.0

    default_watchlist: list[str] = Field(
        default_factory=lambda: [
            "SPY", "QQQ", "XLF", "XLE", "XLI", "XLU", "XLP", "XLV", "KRE", "HYG",
        ]
    )

    @field_validator("alpaca_data_feed")
    @classmethod
    def _feed_known(cls, v: str) -> str:
        if v not in {"iex", "sip"}:
            raise ValueError("alpaca_data_feed must be 'iex' or 'sip'")
        return v

    @model_validator(mode="after")
    def _enforce_paper_only(self) -> Settings:
        """Refuse to boot against a live endpoint. BUILD_SPEC §15."""
        if self.enable_live_trading:
            raise RuntimeError(
                "ENABLE_LIVE_TRADING is true. This application never places real "
                "orders (CLAUDE.md rule 1). Live trades are executed manually by the "
                "human approver and recorded via live_executions."
            )
        if "paper-api" not in self.alpaca_paper_base_url:
            raise RuntimeError(
                f"Refusing to start: ALPACA_PAPER_BASE_URL is "
                f"{self.alpaca_paper_base_url!r}, which is not a paper endpoint. "
                "Expected a URL containing 'paper-api'."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
