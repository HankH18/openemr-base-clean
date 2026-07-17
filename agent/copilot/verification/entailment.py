"""Optional LLM entailment pass.

Runs after the deterministic gate.  Given a claim that already passed
attribution + value match, asks Claude whether the claim's ``text`` is
entailed by the source resource JSON.  Returns a boolean.

Not the safety control — the deterministic gate is.  This catches
*narrative drift* the gate misses: a claim whose numbers all match but
whose surrounding language misrepresents the finding.

Off by default; requires ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from copilot.domain.contracts import Claim
from copilot.resilience import GATING_MAX_RETRIES, GATING_TIMEOUT

_ENTAILMENT_SYSTEM_PROMPT = """You verify whether a clinical claim is
entailed (i.e., faithfully supported without additions or inversions) by
a single FHIR resource.

Respond with EXACTLY one word: "yes" or "no". No punctuation, no
explanation.
"""


class LlmEntailment:
    def __init__(
        self,
        anthropic_api_key: str,
        model: str,
        client: object | None = None,
    ) -> None:
        if not anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set — LlmEntailment refuses to run.")
        self._model = model
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic  # local import

            # A gating-kind call: one word out (8 max_tokens), on the request
            # path, and explicitly "not the safety control" — so it gets the same
            # tight, cheap-to-abandon budget as the critic's safety pass rather
            # than the SDK's inherited 600s. See copilot.resilience.
            self._client = AsyncAnthropic(
                api_key=anthropic_api_key,
                timeout=GATING_TIMEOUT,
                max_retries=GATING_MAX_RETRIES,
            )

    async def entails(self, claim: Claim, resource: Mapping[str, Any]) -> bool:
        payload = json.dumps(
            {"claim": claim.text, "resource": dict(resource)},
            ensure_ascii=False,
        )
        response = await self._client.messages.create(  # type: ignore[attr-defined]
            model=self._model,
            max_tokens=8,
            system=_ENTAILMENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": payload}],
        )
        text = _extract_text(response).strip().lower()
        return text.startswith("y")


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
