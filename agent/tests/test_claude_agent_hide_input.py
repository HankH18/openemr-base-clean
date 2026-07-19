"""P3 bite-proof: chat-agent ValidationErrors must not carry the parsed answer.

``_ClaudeAnswer.model_validate_json`` (``agent/claude.py``) parses untrusted LLM
output, the same leak class ``edf8b24`` closed on the extraction schemas. Today
it is safe only by accident — ``_parse_answer`` swallows the ``ValidationError``
so the value never reaches a log. This test guards the schema directly (bypassing
that swallow) so the defense-in-depth holds even if a future caller stringifies
the error the way the synthesizer path does.

Asserts BOTH halves: the offending value is absent (leak closed) and the field
path + error type survive (still diagnosable). Reverting the config reddens this.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.agent.claude import _ClaudeAnswer


def test_claude_answer_hides_fabricated_content() -> None:
    # The model handed back its claim pointers as a bare string instead of the
    # array the schema requires. The string stands in for fabricated clinical
    # narrative that must never surface in a stringified error.
    marker = "ANSWER_PATIENT_HAS_ACUTE_MI_TROPONIN_9_9"
    payload = '{"answer": "ok", "claims": "' + marker + '"}'

    with pytest.raises(ValidationError) as excinfo:
        _ClaudeAnswer.model_validate_json(payload)

    message = str(excinfo.value)
    assert marker not in message, (
        "the parsed answer content must never reach the error text (defense-in-depth "
        "for any caller that stringifies the ValidationError)"
    )
    assert "claims" in message, "the field path must survive so the failure stays diagnosable"
    assert "list_type" in message, "the error type must survive — we hide the value, not the fault"
