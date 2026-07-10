"""Model pricing table + deterministic USD cost of a token usage.

Answers the doc's "how many tokens, at what cost" question: given a model and
the input/output token counts it reported, compute the USD cost from a static
rate card (USD per 1M tokens). Pure and typed — no I/O, no clock, no globals —
so the chat and synthesis paths can record a ``cost_usd`` observability
attribute deterministically.

Rates are provider list prices per million tokens as of 2026-07; update this
table when the rate card changes. An unrecognised model falls back to a sane
default rather than raising, so swapping the configured model can never turn a
served chat turn into a 500 or silently report a spend of zero.
"""

from __future__ import annotations

from typing import Final

# model -> (USD per 1M input tokens, USD per 1M output tokens)
_PRICING: Final[dict[str, tuple[float, float]]] = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

# Fallback for an unrecognised model — Sonnet-tier list price, so an unknown
# model is costed conservatively rather than reported as free.
_DEFAULT_RATES: Final[tuple[float, float]] = (3.0, 15.0)

_TOKENS_PER_MILLION: Final[float] = 1_000_000.0


def rates_for(model: str) -> tuple[float, float]:
    """The ``(input, output)`` USD-per-1M rates for ``model``, or the default."""
    return _PRICING.get(model, _DEFAULT_RATES)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of ``input_tokens``/``output_tokens`` at ``model``'s list price.

    Deterministic and pure. Negative counts are clamped to zero so a bogus usage
    report can never yield a negative cost.
    """
    input_rate, output_rate = rates_for(model)
    billable_input = max(input_tokens, 0)
    billable_output = max(output_tokens, 0)
    return (billable_input * input_rate + billable_output * output_rate) / _TOKENS_PER_MILLION
