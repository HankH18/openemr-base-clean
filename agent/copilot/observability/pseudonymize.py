"""Patient-identifier pseudonymization at the trace-egress boundary.

The problem: ``patient_id`` rides on nearly every span and event
(``span("poller.tick", patient_id=...)``, ``record_verification(...,
patient_id=...)``, ``record_poller_staleness(patient_id=...)``), and
``copilot.observability.langfuse_backend`` is a pure passthrough — so the bare
OpenEMR PID reaches a third-party SaaS on every observation. A bare patient
identifier IS a HIPAA identifier: §164.514(b)(2)(i)(H) lists "medical record
numbers" among the eighteen. It has no business egressing to a processor with
no BAA in evidence.

The obvious fix — dropping ``patient_id`` from the ``Observability`` Protocol —
destroys the ability to ask "show me every trace for the patient whose chart
went wrong", which is the entire reason the field is there. So the field stays
in the Protocol and stays a real ``int`` everywhere in-process (spans, local
logs, the Noop backend). Only the Langfuse backend — the single point where
bytes actually leave the process — maps it, immediately before egress.

Why HMAC and not ``sha256(pid)``: the PID space is small integers. Every
plausible PID's SHA-256 can be precomputed in about a second, so a bare digest
is a reversible encoding of the identifier, not a pseudonym. A keyed digest is
only invertible by someone holding the key, which the third party does not.

Why the key is REQUIRED to emit the field (``COPILOT_OBSERVABILITY_PSEUDONYM_KEY``):
the pseudonym is worth something only if it is STABLE — the same PID must map to
the same pseudonym across spans, processes and restarts, or traces stop
correlating. That leaves two honest options when the key is unset:

* *Per-process random salt.* Safe, and the field keeps appearing — but it is
  stable only within one process lifetime. Across a restart or a second worker
  the same patient silently acquires a second pseudonym, so an operator
  grouping traces by it draws a WRONG conclusion (two patients where there is
  one) while the field looks like it works. A correlation key that lies is
  worse than an absent one, especially in a clinical debugging context.
* *Refuse to emit the field.* Chosen. The loss is bounded and obvious: the
  ``correlation_id`` still threads every observation of a trace, so per-trace
  debugging is completely unaffected — the only casualty is cross-trace
  grouping BY patient, and its absence is visible immediately rather than
  quietly wrong. It also makes the safe state the default: a deployment that
  has not set the key cannot leak the PID by omission.

Refusing to CONSTRUCT the backend without a key was rejected: it converts a
privacy defect into an availability defect (the app builds this at startup),
which is a strictly worse failure for a live clinical system.

Honest scope note: this is *pseudonymization*, not Safe Harbor
de-identification. §164.514(c) requires a re-identification code not be
"derived from or related to" information about the individual; an HMAC of the
PID is so derived. This shrinks the egress from a direct identifier to a keyed
pseudonym — it does not make the Langfuse dataset non-PHI, and it is not a
substitute for a BAA.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel

_logger = logging.getLogger(__name__)

# Marks the value as a pseudonym rather than an identifier, so nobody reading a
# trace mistakes it for something joinable against the chart.
_PREFIX: Final = "pt_"

# Domain separation: this key must never produce the same digest for the same
# input under a different purpose, should the secret ever be reused.
_DOMAIN: Final = b"copilot.observability.patient-pseudonym.v1"

# 64 bits of a SHA-256 HMAC. The PID space is a few thousand small ints, so
# collision risk is nil and a short token keeps traces readable.
_DIGEST_CHARS: Final = 16

# Keys whose value is a patient identifier, wherever they appear — top-level
# kwarg or nested inside a metadata dict. Matched case-insensitively.
PATIENT_ID_KEYS: Final = frozenset({"patient_id", "patient_ids"})

# A value sitting under a patient-id key whose shape we do not recognise. We
# cannot prove it is not an identifier, so it does not leave. Fail closed.
_UNREPRESENTABLE: Final = "pt_redacted"


class PatientPseudonymizer:
    """Maps patient identifiers to stable, non-reversible pseudonyms.

    Construct once per backend and share it: the mapping must be deterministic
    across every span and event in the process, and across processes, which it
    is because it is a pure function of (key, pid).
    """

    def __init__(self, key: str) -> None:
        self._key = key.encode("utf-8")
        if not self._key:
            _logger.warning(
                "observability pseudonym key unset — patient_id will be OMITTED from "
                "trace metadata rather than sent raw to the trace backend; traces stay "
                "correlated by correlation_id but cannot be grouped by patient",
                extra={"setting": "COPILOT_OBSERVABILITY_PSEUDONYM_KEY"},
            )

    @property
    def enabled(self) -> bool:
        """False ⇒ no key, so patient identifiers are dropped, never emitted."""
        return bool(self._key)

    def pseudonym(self, patient_id: int | str) -> str:
        """The stable pseudonym for one identifier.

        Keyed on ``str(patient_id)`` so an int and its string form agree — the
        same patient must not acquire two pseudonyms because one call site
        stringified the id.
        """
        if not self._key:
            raise RuntimeError("no pseudonym key — callers must check `enabled` first")
        mac = hmac.new(
            self._key,
            _DOMAIN + b"|" + str(patient_id).encode("utf-8"),
            hashlib.sha256,
        )
        return _PREFIX + mac.hexdigest()[:_DIGEST_CHARS]

    def scrub(self, value: Any) -> Any:
        """``value`` with every patient identifier inside it pseudonymized.

        Walks recursively rather than checking a top-level kwarg: the backend
        forwards whole metadata dicts, event payloads and span outputs verbatim,
        so an id nested two levels down (``payload={"patient": {"patient_id":
        1015}}``) egresses exactly as readily as a bare kwarg.
        """
        if isinstance(value, Mapping):
            scrubbed: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in PATIENT_ID_KEYS:
                    if not self.enabled:
                        continue  # unkeyed ⇒ the identifier does not leave
                    scrubbed[key] = self._map_identifier(item)
                else:
                    scrubbed[key] = self.scrub(item)
            return scrubbed
        if isinstance(value, BaseModel):
            # Models are serialized by the SDK anyway, so an id inside one
            # egresses. Only swap in the dumped form when scrubbing actually
            # changed something — otherwise hand back the original object and
            # leave the SDK's serialization byte-for-byte as it was.
            dumped = value.model_dump()
            scrubbed_model = self.scrub(dumped)
            return value if scrubbed_model == dumped else scrubbed_model
        if isinstance(value, tuple):
            return tuple(self.scrub(item) for item in value)
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        # Scalars, and sets/frozensets (whose members are hashable, so they
        # cannot nest a mapping) pass through untouched.
        return value

    def _map_identifier(self, value: Any) -> Any:
        """Map whatever sits under a patient-id key. Never returns it raw."""
        if value is None:
            return None
        if isinstance(value, bool):
            # Not an identifier; bool is an int subclass, so check it first.
            return value
        if isinstance(value, (int, str)):
            return self.pseudonym(value)
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._map_identifier(item) for item in value]
        return _UNREPRESENTABLE
