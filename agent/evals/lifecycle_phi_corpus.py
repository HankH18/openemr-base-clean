"""Lifecycle-family telemetry lines for the frozen ``phi_check`` corpus.

The chat/RAG scrub egress that ``gate.py --write-phi-corpus`` captured on its own
covers exactly ONE event family (``chat.answer``). The frozen scanner
(``.swarm-loop/acceptance/phi_check.py``) therefore WARNED that the five
*lifecycle* event families it expects a real end-to-end capture to contain —
``doc.ingest``, ``extraction.run``, ``guideline.retrieve``, ``worker.handoff``,
``verification.result`` — were absent. A corpus blind to those families can catch
a broken chat scrub but not a PHI-capture leak in the ingestion / retrieval /
graph / verification stages.

This module closes that gap WITHOUT touching the frozen scanner. For each of the
five families it drives the family's REAL telemetry emission — the same
``copilot.observability`` span/event API the production call sites use — with a
seeded, deliberately-synthetic PHI-bearing payload, routes it through the REAL
scrub layers, and hands back the scrubbed line for the corpus:

- ``copilot.observability.langfuse_backend.LangfuseObservability`` is the single
  egress point where trace bytes leave the process. Every ``span`` / ``event`` /
  ``record_verification`` payload it emits is passed through the REAL
  ``PatientPseudonymizer.scrub`` first, so a ``patient_id`` becomes a keyed
  ``pt_…`` pseudonym exactly as it would in production. We build it with a fake
  capturing client (no SDK, no network) and a pseudonym key set, so what the
  client records IS the scrubbed egress payload.
- ``copilot.rag.deidentify.deidentify`` is the free-text choke point the real
  retriever runs every query through before egress. The families that could
  realistically carry clinician free-text (a filename that embeds a patient
  name, an OCR preview, the retrieval query, a handoff reason) route that text
  through the REAL ``deidentify`` here, so the captured line is the scrub's
  OUTPUT — not a hand-written clean string.

Result: the corpus now contains lines attributable to all five families, and it
stays PHI-clean because the real scrubs hold. It BITES: neutering ``deidentify``
to an identity function makes the four free-text-bearing family lines leak the
probe's identifiers, so the frozen scanner's count jumps above zero — proving
these are genuine PHI-carrying paths, not inert padding. ``verification.result``
is protected by the pseudonymizer rather than ``deidentify`` (its real emission
carries a ``patient_id`` and no free text), so its line demonstrates the
pseudonym egress transform instead.

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from copilot.observability.base import correlation_id_var
from copilot.observability.langfuse_backend import LangfuseObservability
from copilot.rag.deidentify import deidentify

# --- the seeded synthetic PHI probe -----------------------------------------
#
# Every identifier below is clearly fake / synthetic — a reserved-shape name,
# a fictional MRN, an obviously-synthetic SSN/phone, an ``example.com`` email —
# and never a real person. It is deliberately built from the SAME identifier
# classes the frozen scanner detects (labelled name, MRN, SSN, DOB, phone,
# email), so a scrub that stops working leaks something the scanner will catch.
_PHI_TEXT = (
    "Patient: Marisol Quintanilla MRN: 4417702 SSN 123-45-6789 "
    "DOB 03/14/1962 phone (555) 010-1234 email m.quint@example.com"
)

#: A synthetic OpenEMR patient id (a small int, exactly as production carries it).
#: The pseudonymizer maps it to a stable ``pt_…`` token at egress.
_PATIENT_ID = 1015

#: A fixed, non-secret pseudonym key so the ``patient_id`` is genuinely
#: pseudonymized (not merely dropped) and the corpus is byte-stable across runs.
_PSEUDONYM_KEY = "lifecycle-corpus-pseudonym-key-v1"


class _CapturedObservation:
    """One recorded Langfuse observation (trace root, child span, or event).

    The backend forwards ALREADY-SCRUBBED payloads to the client, so whatever we
    record here is the exact egress form: ``patient_id`` pseudonymized, free-text
    fields already de-identified by the caller. Nested ``span``/``event`` calls
    append to the same sink so the whole tree is captured in emission order.
    """

    def __init__(
        self, sink: list[_CapturedObservation], name: str, kind: str, metadata: Any
    ) -> None:
        self._sink = sink
        self.name = name
        self.kind = kind
        self.metadata: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
        self.output: Any = None
        sink.append(self)

    # The backend routes both set_attribute and set_output through inner.update().
    def update(self, *, metadata: Any = None, output: Any = None) -> None:
        if isinstance(metadata, dict):
            self.metadata.update(metadata)
        if output is not None:
            self.output = output

    def span(self, *, name: str, metadata: Any = None) -> _CapturedObservation:
        return _CapturedObservation(self._sink, name, "span", metadata)

    def event(self, *, name: str, metadata: Any = None) -> None:
        _CapturedObservation(self._sink, name, "event", metadata)

    def end(self) -> None:
        return None

    def as_line(self) -> str:
        """Serialize to one corpus JSONL line, tagged with the family name."""
        payload: dict[str, Any] = {"event": self.name, "kind": self.kind}
        if self.metadata:
            payload["metadata"] = self.metadata
        if self.output is not None:
            payload["output"] = self.output
        return json.dumps(payload, sort_keys=True)


class _CapturingClient:
    """A fake Langfuse v2 client that records scrubbed egress instead of sending.

    Matches the exact call surface ``LangfuseObservability`` uses — ``trace``,
    ``event``, ``flush`` on the client; ``span``/``event``/``update``/``end`` on
    the returned observation — so the backend's real code path runs unchanged.
    Nothing here may raise: the backend wraps client calls in ``suppress`` /
    try-except and would silently drop a span on any error, so a throwing fake
    would capture nothing.
    """

    def __init__(self) -> None:
        self.sink: list[_CapturedObservation] = []

    def trace(
        self, *, name: str, id: str | None = None, metadata: Any = None
    ) -> _CapturedObservation:
        return _CapturedObservation(self.sink, name, "trace", metadata)

    def event(
        self, *, name: str, trace_id: str | None = None, metadata: Any = None
    ) -> _CapturedObservation:
        return _CapturedObservation(self.sink, name, "event", metadata)

    def flush(self) -> None:
        return None


async def _emit_lifecycle_families(obs: LangfuseObservability) -> None:
    """Drive each of the five families' REAL telemetry emission with synthetic PHI.

    Attribute names and values mirror the production call sites
    (``documents/pipeline.py``, ``rag/retriever.py``, ``graph/supervisor.py``,
    ``observability/langfuse_backend.py``). The realistic free-text PHI vector
    for each stage is routed through the REAL ``deidentify`` choke point; the
    ``patient_id`` is routed through the REAL pseudonymizer by the backend.
    """
    correlation_id_var.set("lifecycle-corpus-0001")

    # doc.ingest — carries patient_id (pseudonymized) and, realistically, a
    # filename that "can itself carry a patient name" (pipeline.py's own note),
    # de-identified here. extraction.run is its nested child, exactly as the
    # pipeline opens it.
    async with obs.span(
        "doc.ingest",
        patient_id=_PATIENT_ID,
        doc_type="lab_pdf",
        correlation_id="lifecycle-corpus-0001",
        scrubbed_filename=deidentify(_PHI_TEXT),
    ) as ingest_span:
        ingest_span.set_attribute("page_count", 3)
        ingest_span.set_attribute("fact_count", 7)
        ingest_span.set_output({"status": "extracted", "page_count": 3, "fact_count": 7})

        # extraction.run — nested under doc.ingest. Its risk is page text / OCR
        # tokens / extracted values; a preview of that text is de-identified.
        async with obs.span(
            "extraction.run",
            doc_type="lab_pdf",
            page_count=3,
            scrubbed_ocr_preview=deidentify(_PHI_TEXT),
        ) as extraction_span:
            extraction_span.set_attribute("extraction_confidence", 0.94)
            extraction_span.set_attribute("fact_count", 7)
            extraction_span.set_output({"fact_count": 7})

    # guideline.retrieve — the retriever de-identifies the query before egress;
    # the scrubbed query is what actually reaches the embedder/reranker.
    async with obs.span(
        "guideline.retrieve",
        top_k=4,
        scrubbed_query=deidentify(_PHI_TEXT),
    ) as retrieve_span:
        retrieve_span.set_attribute("hits", 4)
        retrieve_span.set_attribute("corpus_chunks", 128)
        retrieve_span.set_output({"hits": 4, "corpus_chunks": 128})

    # worker.handoff — a one-off event carrying routing signals (non-PHI) plus,
    # defensively, a de-identified reason string.
    obs.event(
        "worker.handoff",
        from_agent="supervisor",
        to_agent="evidence-retriever",
        reason=deidentify(_PHI_TEXT),
        payload={"signals": ["sepsis-screen", "map-target"]},
    )

    # verification.result — the real record_verification event: passed/action
    # plus a patient_id the pseudonymizer maps to a pt_… token before egress.
    obs.record_verification(passed=True, action="served", patient_id=_PATIENT_ID)


def lifecycle_corpus_lines() -> list[str]:
    """Return one scrubbed JSONL corpus line per lifecycle event family.

    Deterministic, keyless and network-free: builds the real Langfuse backend
    over a fake capturing client (no SDK import, no egress) with a pseudonym key
    set, drives the five families' real emission with the synthetic PHI probe,
    and serializes whatever the backend forwarded to the client. Because every
    payload passed through the real ``PatientPseudonymizer.scrub`` (and the
    free-text fields through the real ``deidentify``), each line is the genuine
    scrubbed egress — clean when the scrubs hold, leaky when they do not.
    """
    client = _CapturingClient()
    obs = LangfuseObservability(
        host="http://localhost",
        public_key="pk-lifecycle-corpus",
        secret_key="sk-lifecycle-corpus",
        client=client,
        pseudonym_key=_PSEUDONYM_KEY,
    )
    asyncio.run(_emit_lifecycle_families(obs))
    return [observation.as_line() for observation in client.sink]
