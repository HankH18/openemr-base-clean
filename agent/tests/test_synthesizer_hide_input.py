"""P3 bite-proof: synthesizer ValidationErrors must not carry the parsed claim.

Round-2 commit ``edf8b24`` set ``hide_input_in_errors=True`` on the four
*extraction* schemas so a pydantic ``ValidationError`` stops embedding parsed
PHI into the error text. ``_ClaudeSynthesizerResponse`` parses untrusted LLM
output the same way — ``model_validate_json(text)`` at ``synthesizer.py:207`` —
but was missed. Its ``ValidationError`` is stringified into
``SynthesisError(f"... {exc}")`` and that message propagates to ``worker.poller``
where it is emitted in the ``poller.result`` observability event → Langfuse.
Without the config, ``str(exc)`` includes ``input_value=`` fragments carrying the
model's synthesized clinical claims: the same leak class edf8b24 closed.

Each test asserts BOTH halves: the offending value is absent (the leak is
closed) and the field path + error type survive (the error stays diagnosable).
Reverting the config flips the value back into ``str(exc)`` and reddens these —
that is the bite. No pre-existing test guards this path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.worker.synthesizer import _ClaudeSynthesizerResponse


def test_synthesizer_response_hides_fabricated_claim_value() -> None:
    # The model handed back its synthesized claims as a bare string instead of the
    # array the schema requires. The string stands in for fabricated clinical
    # content that must never reach the poller.result event / Langfuse.
    marker = "SYNTH_CLAIM_TROPONIN_9_9_ACUTE_MI"
    payload = '{"claims": "' + marker + '", "acuity_score": 8.0, "rank_reason": "r"}'

    with pytest.raises(ValidationError) as excinfo:
        _ClaudeSynthesizerResponse.model_validate_json(payload)

    message = str(excinfo.value)
    assert marker not in message, (
        "the synthesized claim value must never reach the error text — it is "
        "stringified into SynthesisError and emitted in the poller.result event"
    )
    assert "claims" in message, "the field path must survive so the failure stays diagnosable"
    assert "list_type" in message, "the error type must survive — we hide the value, not the fault"


def test_synthesizer_response_hides_acuity_number() -> None:
    # A numeric-shaped leak on a top-level field of the outer model: acuity_score
    # handed back as an unparseable string. The hidden-input config strips it too.
    marker = "ACUITY_LEAK_9c3f_POTASSIUM_5_7"
    payload = '{"claims": [], "acuity_score": "' + marker + '", "rank_reason": "r"}'

    with pytest.raises(ValidationError) as excinfo:
        _ClaudeSynthesizerResponse.model_validate_json(payload)

    message = str(excinfo.value)
    assert marker not in message, "a value on any top-level field of the response must be hidden"
    assert "acuity_score" in message, "the field path must survive"
    assert "float_parsing" in message, "the error type must survive"
