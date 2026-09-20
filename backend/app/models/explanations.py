"""The `explanations` table — Phase 5, BUILD_SPEC §11.2.

Deviation from BUILD_SPEC, flagged per CLAUDE.md's convention (same as
`Position.entry_order_id` and `GateReportRecord` elsewhere in this codebase):
§5's schema predates Phase 5 and defines no explanations table at all. This
module is that table. Columns are designed directly from §11.2's four output
sections plus what §16's Phase 5 acceptance criteria and §11.3's tip
selector need to persist:

- `entry_rationale` / `exit_rationale` / `what_went_right` / `what_went_wrong`
  — the four sections §11.2 names, always populated (by the LLM path or the
  deterministic template fallback — CLAUDE.md rule 2 / §11.2's "never
  silently drop the explanation").
- `tip_id` / `tip_body` — the tip §11.3's selector chose for this trade.
  `tip_body` is denormalized (copied at generation time) rather than joined
  live from the tip library, so a trade's journal entry doesn't silently
  change wording if the library is edited later. `tip_id` is nullable: a
  trade whose context matches no trigger rule still gets a rotating
  fundamentals tip in practice, but the column stays nullable for the
  theoretical case where tip selection itself is unavailable.
- `source` — `'llm'` or `'template'`, so the UI/analytics can tell which
  path produced a given explanation without re-deriving it from `prompt_hash`.
- `prompt_hash` — sha256 of the deterministically-assembled input JSON, set
  only when `source='llm'` per §11.2's caching note ("cache by prompt_hash so
  re-renders don't re-bill"). The template path is free and instant, so it
  has no caching need for the hash itself, but idempotency (one explanation
  per trade, ever) is enforced by the `UniqueConstraint` on `trade_id`
  regardless of which path produced it.

One row per `Trade`, written synchronously right after the trade closes —
see `app/execution/order_service.py` for why this is synchronous rather than
the worker-queued design BUILD_SPEC §11.2 describes (no worker process
exists in this codebase tonight; documented there, not here, since that's
the deviation's origin).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid_pk


class ExplanationRecord(Base):
    __tablename__ = "explanations"
    __table_args__ = (UniqueConstraint("trade_id", name="uq_explanations_trade_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    trade_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )

    entry_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    exit_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    what_went_right: Mapped[str] = mapped_column(Text, nullable=False)
    what_went_wrong: Mapped[str] = mapped_column(Text, nullable=False)

    # Denormalized tip selection — see module docstring.
    tip_id: Mapped[str | None] = mapped_column(String(64))
    tip_body: Mapped[str | None] = mapped_column(Text)

    # 'llm' | 'template'
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # sha256 of the assembled input JSON. Only set when source == 'llm'.
    prompt_hash: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
