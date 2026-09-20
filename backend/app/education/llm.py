"""The optional LLM explanation path. BUILD_SPEC §11.2, ADDENDUM_LIVE_APPROVAL §7.

This module is the thin, swappable wrapper the addendum sketches — a
`LLMProvider` Protocol plus a Grok/OpenAI-compatible implementation. It is
**not** the guaranteed path. `app/education/explainer.py` owns the decision
of whether to call it at all and always falls back to the deterministic
template renderer on any failure (CLAUDE.md rule 2 / §11.2's "the app must
never silently drop the explanation").

Tonight's actual runtime state: `.env` has `LLM_PROVIDER=grok`, a base URL,
and a model name, but `XAI_API_KEY` is blank. `get_llm_provider` returns
`None` in that case, which is the signal `explainer.py` uses to skip the LLM
call entirely rather than attempt one that would only fail. This is checked
automatically on every call — nothing here requires a human to remember to
flip a flag.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings

# §11.2's exact system-prompt constraints. Not "inspired by" — this is what
# ships, verbatim, because the whole point of the education layer is that
# the model translates a decision that was already made and recorded; it
# never gets to determine one. See BUILD_SPEC §11.1/§11.2 and
# ADDENDUM_LIVE_APPROVAL §7 ("The constraint that matters more than the
# model choice").
SYSTEM_PROMPT = """You are explaining a decision that has already been made and recorded. Do not \
speculate about causes that are not present in the supplied conditions or features.
Every factual claim must reference a supplied value. Cite the number.
If the data does not support a conclusion, say so plainly.
Do not predict future prices. Do not suggest what to trade next.
Do not be encouraging about a losing process. A loss that followed the rules is \
a good trade; a win that broke the rules is a bad trade. Say which happened.
Maximum 120 words per section.

Respond with a JSON object with exactly these four string keys: \
"entry_rationale", "exit_rationale", "what_went_right", "what_went_wrong"."""

_SECTION_KEYS = ("entry_rationale", "exit_rationale", "what_went_right", "what_went_wrong")


@dataclass(frozen=True)
class Explanation:
    entry_rationale: str
    exit_rationale: str
    what_went_right: str
    what_went_wrong: str


class LLMExplanationError(Exception):
    """Raised on any failure to produce a usable explanation from the LLM path.

    Covers a missing/invalid key, a network or timeout error, and a response
    that doesn't parse into the four required sections. The caller
    (`app/education/explainer.py`) catches this specific exception and falls
    back to the template renderer -- it does not try to distinguish failure
    modes further, since the fallback is the same regardless.
    """


class LLMProvider(Protocol):
    async def explain(self, payload: dict) -> Explanation: ...


class GrokProvider:
    """xAI's Grok, via the OpenAI-compatible `openai` SDK pointed at `XAI_BASE_URL`.

    Matches ADDENDUM_LIVE_APPROVAL §7's own sketch. Never called tonight
    (no API key present — see module docstring), but built and wired so
    swapping in a real key later is a config change, not a rewrite.
    """

    def __init__(self, api_key: str, base_url: str, model: str, timeout_seconds: float):
        # Imported lazily, matching this codebase's convention for optional
        # heavy/optional dependencies (see app/backtest/sweep.py's lazy
        # vectorbt import) -- `openai` is lightweight, but importing it at
        # module scope would make it a hard dependency of the whole app just
        # to build a provider object that most requests tonight never use.
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        self._model = model

    async def explain(self, payload: dict) -> Explanation:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 -- any transport/API failure falls back
            raise LLMExplanationError(f"grok request failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
            parsed = json.loads(content)
            sections = {key: str(parsed[key]) for key in _SECTION_KEYS}
        except Exception as exc:  # noqa: BLE001 -- any shape surprise falls back
            raise LLMExplanationError(f"grok response did not parse: {exc}") from exc

        return Explanation(**sections)


def get_llm_provider(settings: Settings) -> LLMProvider | None:
    """The single place that decides whether an LLM call is even attempted.

    Returns `None` (never raises) whenever no usable provider is configured
    -- unknown provider name or a blank API key -- so the caller's fallback
    is automatic rather than something a human has to remember to trigger.
    """
    if settings.llm_provider != "grok":
        return None
    if not settings.xai_api_key:
        return None
    return GrokProvider(
        api_key=settings.xai_api_key,
        base_url=settings.xai_base_url,
        model=settings.explanation_model,
        timeout_seconds=settings.explanation_timeout_seconds,
    )
