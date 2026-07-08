"""Verification layer — deterministic gate, fail-closed.

ARCHITECTURE §"Components → verification" and §"Rationale #5": every
agent output — memory-file synthesis at write time, chat answers at
serve time — passes through this layer.  It is deterministic code.
The LLM is a **synthesizer**, never the safety control.

Two entry points:

- ``verify_memory_file(summary, context)`` — called by the Poller right
  after the synthesizer produces a proposed summary.  If it fails the
  gate, the summary is dropped and never touches the store.
- ``verify_answer(claims, patient_id, fhir_client)`` — called by the
  chat handler.  Re-fetches each cited resource by ID before allowing
  a claim to stream to the UI.  (Wired in a later unit that ships the
  chat endpoint; the shared logic lives here today.)

The public shape is `VerificationResult`; `action` says whether the
caller may serve, must degrade (drop failing claims, keep passing ones),
or must withhold entirely.  Domain flags (allergy/med conflict, critical
labs) are always surfaced regardless of claim-pass status.
"""

from copilot.verification.core import (
    VerificationContext,
    Verifier,
    build_context_from_resources,
    extract_field_value,
    extract_numbers,
)
from copilot.verification.entailment import LlmEntailment
from copilot.verification.rules import (
    DomainRule,
    allergy_medication_conflict,
    critical_lab,
    default_rules,
)

__all__ = [
    "DomainRule",
    "LlmEntailment",
    "VerificationContext",
    "Verifier",
    "allergy_medication_conflict",
    "build_context_from_resources",
    "critical_lab",
    "default_rules",
    "extract_field_value",
    "extract_numbers",
]
