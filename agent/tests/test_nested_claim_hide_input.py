"""P3 bite-proof (PHI): nested _ClaudeClaim models must self-hide input values.

Round-3 added ``hide_input_in_errors=True`` to the OUTER response models
(``_ClaudeSynthesizerResponse`` in ``worker/synthesizer.py`` and ``_ClaudeAnswer``
in ``agent/claude.py``). ``hide_input_in_errors`` is honoured from the config of
the model whose validator is *entered*: a ``ValidationError`` raised through the
outer model already has every nested value stripped (verified separately). But
the NESTED ``_ClaudeClaim`` models carried no config of their own, so validating a
claim DIRECTLY — any future caller that parses a single claim rather than the whole
response — still embedded a distinctive clinical value (e.g. ``value`` as a number)
into ``str(ValidationError)``. That is the same leak class ``edf8b24`` closed, and
the same defense-in-depth rationale R3 used to harden ``_ClaudeAnswer`` even though
``_parse_answer`` swallows its error today: the model must be self-protecting, not
safe only by virtue of its current caller.

Each test validates the nested claim model DIRECTLY (the code path the outer config
does not cover) and asserts BOTH halves: the offending value is absent (leak closed)
and the field path + error type survive (still diagnosable). Reverting the nested
``model_config`` flips the value back into ``str(exc)`` and reddens these.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.agent.claude import _ClaudeClaim as _ChatClaim
from copilot.worker.synthesizer import _ClaudeClaim as _SynthClaim


def test_synthesizer_nested_claim_hides_value_on_direct_validation() -> None:
    # A synthesized claim's `value` handed back as a number instead of a string.
    # The number stands in for a fabricated clinical reading (e.g. a troponin
    # level) that must never surface in a stringified error.
    marker = "987650042"

    with pytest.raises(ValidationError) as excinfo:
        _SynthClaim.model_validate(
            {
                "text": "t",
                "resource_type": "Observation",
                "resource_id": "trop-1",
                "field": "valueQuantity.value",
                "value": int(marker),
            }
        )

    message = str(excinfo.value)
    assert marker not in message, (
        "a value on a claim field must never reach the error text — the nested "
        "model must self-hide, not rely on its current outer caller"
    )
    assert "value" in message, "the field path must survive so the failure stays diagnosable"
    assert "string_type" in message, "the error type must survive — we hide the value, not the fault"


def test_chat_agent_nested_claim_hides_value_on_direct_validation() -> None:
    # A chat claim's `resource_id` handed back as a number. Stands in for fabricated
    # pointer/narrative content that must not surface in a stringified error.
    marker = "551234789"

    with pytest.raises(ValidationError) as excinfo:
        _ChatClaim.model_validate({"resource_type": "Observation", "resource_id": int(marker)})

    message = str(excinfo.value)
    assert marker not in message, (
        "a value on a claim field must never reach the error text (defense-in-depth "
        "for any caller that validates a single claim and stringifies the error)"
    )
    assert "resource_id" in message, "the field path must survive so the failure stays diagnosable"
    assert "string_type" in message, "the error type must survive — we hide the value, not the fault"
