"""The raw patient id must never reach the trace backend.

The defect these lock out: ``patient_id`` rides on nearly every span and event
(``span("poller.tick", patient_id=...)``, ``record_verification(...,
patient_id=...)``, ``record_poller_staleness(patient_id=...)``) and
``copilot.observability.langfuse_backend`` was a pure passthrough
(``span(name=..., metadata=attributes)`` / ``update(metadata={key: value})``), so
the bare OpenEMR PID reached a third-party SaaS on every observation of the
deployed stack. A bare patient identifier IS a HIPAA identifier —
§164.514(b)(2)(i)(H) lists medical record numbers among the eighteen — and no BAA
is in evidence for Langfuse.

The fix is at the BACKEND, not the Protocol: ``patient_id`` stays a real ``int``
everywhere in-process, and only ``LangfuseObservability`` — the single point where
bytes leave the process — maps it to a keyed pseudonym. So these tests drive the
REAL :class:`LangfuseObservability` against a recording client, exactly as
``tests/test_poller_telemetry.py`` does: a hand-rolled observability double would
prove nothing about the adapter that actually serializes the egress payload.

Four halves, and the last three are what stop the fix from being "emit nothing":

1. The raw pid reaches no span name, metadata, output, or event payload —
   however deeply nested.
2. A pseudonym IS emitted, and is STABLE across spans (or traces stop
   correlating and the field is worthless).
3. Different patients get different pseudonyms (or the field is worthless in the
   other direction — every patient looks like one).
4. In-process behavior and the Noop backend are untouched: upstream callers
   still see the real int.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from pydantic import BaseModel

from copilot.observability import NoopObservability, correlation_id_var
from copilot.observability.langfuse_backend import LangfuseObservability

# asyncio_mode = "auto" (pyproject) collects the async tests here; the sync ones
# below must stay unmarked, so this file sets no module-level asyncio mark.

# The patient whose chart is under discussion. A small int, which is the whole
# reason a bare sha256 would not do: an attacker precomputes the entire PID space.
PID = 1015
OTHER_PID = 2030

# A stable, high-entropy operator secret. The pseudonym is only non-reversible
# while this is out of the third party's reach.
KEY = "test-pseudonym-key-not-a-real-secret"


# --- Recording Langfuse client ----------------------------------------------


class _Recorder:
    """Every call the backend made, whole — this IS the egress payload."""

    def __init__(self) -> None:
        self.traces: list[dict[str, Any]] = []
        self.spans: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.flushed = 0


class _FakeObservation:
    """A Langfuse trace root / span, recording children, events and updates."""

    def __init__(self, name: str, trace_id: Any, rec: _Recorder) -> None:
        self.name = name
        self.trace_id = trace_id
        self._rec = rec

    def span(self, name: str, metadata: Any = None) -> _FakeObservation:
        self._rec.spans.append({"name": name, "trace_id": self.trace_id, "metadata": metadata})
        return _FakeObservation(name, self.trace_id, self._rec)

    def event(self, name: str, metadata: Any = None) -> None:
        self._rec.events.append({"name": name, "trace_id": self.trace_id, "metadata": metadata})

    def update(self, **kwargs: Any) -> None:
        # set_attribute/set_output both land here — an egress surface exactly
        # like span metadata, and the one a top-level-kwarg-only fix would miss.
        self._rec.updates.append({"parent": self.name, **kwargs})

    def end(self) -> None:
        return


class _FakeClient:
    """Stands in for the Langfuse SDK client."""

    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    def trace(self, name: str, id: Any = None, metadata: Any = None) -> _FakeObservation:
        self._rec.traces.append({"name": name, "id": id, "metadata": metadata})
        return _FakeObservation(name, id, self._rec)

    def event(self, name: str, trace_id: Any = None, metadata: Any = None) -> None:
        self._rec.events.append({"name": name, "trace_id": trace_id, "metadata": metadata})

    def flush(self) -> None:
        self._rec.flushed += 1


def _langfuse(rec: _Recorder, *, key: str = KEY) -> LangfuseObservability:
    return LangfuseObservability(
        host="https://x",
        public_key="pk",
        secret_key="sk",
        client=_FakeClient(rec),
        pseudonym_key=key,
    )


# --- recursive text harvest --------------------------------------------------
#
# Same shape as tests/test_graph_telemetry_no_phi.py::_strings — checking one
# known field instead would only prove the leak moved.


def _strings(value: Any) -> Iterator[str]:
    """Every string reachable inside ``value``, however deeply nested.

    Walks mappings (keys AND values), sequences, and pydantic models, and falls
    back to ``str(value)`` for anything else — so a pid parked inside a model or
    an opaque object whose repr embeds it is caught too, not just a bare kwarg.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, BaseModel):
        yield from _strings(value.model_dump())
    elif value is not None:
        yield str(value)


def _egress_text(rec: _Recorder) -> list[tuple[str, str]]:
    """Every (site, text) pair that left the process, labelled by origin."""
    harvested: list[tuple[str, str]] = []
    for site, calls in (
        ("client.trace", rec.traces),
        ("parent.span", rec.spans),
        ("event", rec.events),
        ("observation.update", rec.updates),
    ):
        for call in calls:
            for key, value in call.items():
                harvested += [(f"{site} field {key!r}", t) for t in _strings(value)]
    return harvested


def _leaks(rec: _Recorder, needle: int = PID) -> list[tuple[str, str]]:
    """Every egress site whose text contains the raw pid as a standalone token.

    Substring alone would false-positive on any number containing 1015; the
    surrounding-character check keeps this honest while still catching the pid
    embedded in a repr, a URL, or a formatted string.
    """
    found: list[tuple[str, str]] = []
    token = str(needle)
    for site, text in _egress_text(rec):
        start = text.find(token)
        while start != -1:
            before = text[start - 1] if start > 0 else ""
            after = text[start + len(token) :][:1]
            if not before.isdigit() and not after.isdigit():
                found.append((site, text))
                break
            start = text.find(token, start + 1)
    return found


def _assert_no_raw_pid(rec: _Recorder) -> None:
    leaks = _leaks(rec)
    assert not leaks, (
        "the raw patient id reached the trace backend — every entry below is a "
        "HIPAA identifier egressing to a third-party SaaS with no BAA:\n"
        + "\n".join(f"  - {site}: {text!r}" for site, text in leaks)
    )


def _pseudonyms(rec: _Recorder) -> list[str]:
    """Every pseudonym-shaped token that egressed."""
    return sorted({t for _site, t in _egress_text(rec) if t.startswith("pt_")})


# --- 1. the raw pid reaches no egress surface --------------------------------


class TestNoRawPidReachesLangfuse:
    async def test_span_metadata_carries_no_raw_pid(self) -> None:
        rec = _Recorder()
        async with _langfuse(rec).span("poller.tick", patient_id=PID):
            pass

        assert rec.traces, "the backend opened no trace — the test proves nothing"
        _assert_no_raw_pid(rec)

    async def test_nested_span_and_set_attribute_carry_no_raw_pid(self) -> None:
        """``update()`` is a separate egress path from ``span()``'s kwargs."""
        rec = _Recorder()
        obs = _langfuse(rec)
        # Entered exactly as copilot/graph/supervisor.py enters them, so the
        # child really is created via parent.span(), not the client's trace().
        async with (
            obs.span("graph.run", patient_id=PID),
            obs.span("finalize.verify", patient_id=PID) as span,
        ):
            span.set_attribute("patient_id", PID)
            span.set_output({"patient_id": PID, "action": "served"})

        assert rec.spans, "no child span was created — the test proves nothing"
        assert rec.updates, "no update() reached the client — the test proves nothing"
        _assert_no_raw_pid(rec)

    async def test_record_verification_carries_no_raw_pid(self) -> None:
        rec = _Recorder()
        obs = _langfuse(rec)
        token = correlation_id_var.set("corr-veri")
        try:
            obs.record_verification(passed=False, action="withheld", patient_id=PID)
        finally:
            correlation_id_var.reset(token)

        assert rec.events, "no verification event egressed — the test proves nothing"
        _assert_no_raw_pid(rec)

    async def test_record_poller_staleness_carries_no_raw_pid(self) -> None:
        rec = _Recorder()
        _langfuse(rec).record_poller_staleness(patient_id=PID, age_seconds=1800)

        assert rec.events, "no staleness event egressed — the test proves nothing"
        _assert_no_raw_pid(rec)
        # The gauge itself must survive — the fix is not "emit nothing".
        assert rec.events[0]["metadata"]["age_seconds"] == 1800

    async def test_a_pid_nested_deep_inside_a_payload_carries_no_raw_pid(self) -> None:
        """A top-level-kwarg-only fix would ship this one verbatim.

        The handoff payload is a free-form dict built by the supervisor, so
        nothing stops a pid landing inside it — which is exactly why the backend
        walks the whole structure rather than checking its own kwargs.
        """
        rec = _Recorder()
        obs = _langfuse(rec)
        async with obs.span("graph.run"):
            obs.event(
                "worker.handoff",
                to_agent="evidence-retriever",
                payload={"scope": {"patient_id": PID}, "document_ids": ["5"]},
            )

        assert rec.events, "no handoff event egressed — the test proves nothing"
        _assert_no_raw_pid(rec)
        # ...and the rest of the payload is untouched.
        assert rec.events[0]["metadata"]["payload"]["document_ids"] == ["5"]

    async def test_a_pid_inside_a_pydantic_model_output_carries_no_raw_pid(self) -> None:
        """``set_output(metrics.model_dump())`` is a real call site; a model that
        grows a pid field must not become a leak."""

        class _Metrics(BaseModel):
            patient_id: int
            latency_ms: int

        rec = _Recorder()
        async with _langfuse(rec).span("graph.run") as span:
            span.set_output(_Metrics(patient_id=PID, latency_ms=42))

        assert rec.updates, "no output egressed — the test proves nothing"
        _assert_no_raw_pid(rec)

    async def test_a_pid_as_a_string_carries_no_raw_pid(self) -> None:
        """Stringifying an id upstream must not route around the map."""
        rec = _Recorder()
        async with _langfuse(rec).span("doc.ingest", patient_id=str(PID)):
            pass

        _assert_no_raw_pid(rec)


# --- 2. a pseudonym IS emitted, and is stable --------------------------------


class TestPseudonymIsPresentAndStable:
    async def test_a_pseudonym_is_emitted_in_place_of_the_pid(self) -> None:
        """Dropping the pid must not mean dropping the ability to correlate."""
        rec = _Recorder()
        async with _langfuse(rec).span("poller.tick", patient_id=PID):
            pass

        metadata = rec.traces[0]["metadata"]
        assert "patient_id" in metadata, (
            f"the patient_id key must survive so traces stay groupable: {metadata}"
        )
        assert metadata["patient_id"].startswith("pt_"), (
            f"the emitted value must be a marked pseudonym: {metadata}"
        )

    async def test_the_pseudonym_is_stable_across_two_separate_spans(self) -> None:
        """Two spans, two backends, one process — the same patient must map to
        the same pseudonym or a trace cannot be joined to its siblings."""
        rec_a, rec_b = _Recorder(), _Recorder()
        async with _langfuse(rec_a).span("poller.tick", patient_id=PID):
            pass
        async with _langfuse(rec_b).span("chat", patient_id=PID):
            pass

        first = rec_a.traces[0]["metadata"]["patient_id"]
        second = rec_b.traces[0]["metadata"]["patient_id"]
        assert first == second, (
            f"the same pid produced two pseudonyms ({first!r} vs {second!r}) — traces "
            "for one patient can no longer be correlated, which is what the field is for"
        )

    async def test_the_pseudonym_is_stable_across_span_event_and_verification(self) -> None:
        """Stability must hold across egress PATHS, not just across calls: a
        span, an event, and record_verification must agree."""
        rec = _Recorder()
        obs = _langfuse(rec)
        async with obs.span("graph.run", patient_id=PID) as span:
            span.set_attribute("patient_id", PID)
            obs.record_verification(passed=True, action="served", patient_id=PID)

        assert len(_pseudonyms(rec)) == 1, (
            f"one patient must have exactly one pseudonym across every path: {_pseudonyms(rec)}"
        )

    async def test_the_pseudonym_is_reproducible_from_the_key_alone(self) -> None:
        """Stability across PROCESSES and restarts: the pseudonym is a pure
        function of (key, pid), so a fresh backend reproduces it. This is what a
        per-process random salt would fail."""
        from copilot.observability.pseudonymize import PatientPseudonymizer

        rec = _Recorder()
        async with _langfuse(rec).span("poller.tick", patient_id=PID):
            pass

        emitted = rec.traces[0]["metadata"]["patient_id"]
        assert emitted == PatientPseudonymizer(KEY).pseudonym(PID)

    async def test_two_different_pids_get_different_pseudonyms(self) -> None:
        rec_a, rec_b = _Recorder(), _Recorder()
        async with _langfuse(rec_a).span("poller.tick", patient_id=PID):
            pass
        async with _langfuse(rec_b).span("poller.tick", patient_id=OTHER_PID):
            pass

        first = rec_a.traces[0]["metadata"]["patient_id"]
        second = rec_b.traces[0]["metadata"]["patient_id"]
        assert first != second, (
            f"two different patients collapsed onto one pseudonym ({first!r}) — the "
            "field would silently merge two charts into one trace group"
        )

    async def test_a_different_key_yields_a_different_pseudonym(self) -> None:
        """The digest is KEYED — otherwise the tiny pid space is brute-forced."""
        rec_a, rec_b = _Recorder(), _Recorder()
        async with _langfuse(rec_a, key=KEY).span("poller.tick", patient_id=PID):
            pass
        async with _langfuse(rec_b, key="a-different-key").span("poller.tick", patient_id=PID):
            pass

        assert (
            rec_a.traces[0]["metadata"]["patient_id"] != rec_b.traces[0]["metadata"]["patient_id"]
        )

    def test_a_bare_sha256_of_the_pid_never_egresses(self) -> None:
        """The precomputation attack, spelled out: an unkeyed digest of a small
        int is a reversible encoding, so the emitted token must not be one."""
        import hashlib

        from copilot.observability.pseudonymize import PatientPseudonymizer

        emitted = PatientPseudonymizer(KEY).pseudonym(PID)
        for candidate in (str(PID), f"patient_id={PID}"):
            digest = hashlib.sha256(candidate.encode()).hexdigest()
            assert digest[: len(emitted)] not in emitted, (
                "the pseudonym is a bare sha256 — the whole pid space precomputes in seconds"
            )


# --- 3. the unkeyed policy: refuse to emit, never emit raw -------------------


class TestUnkeyedRefusesToEmitTheField:
    async def test_without_a_key_the_pid_is_omitted_not_emitted_raw(self) -> None:
        """The safe default: an operator who has not set the key cannot leak the
        pid by omission. Chosen over a per-process random salt, which would keep
        emitting a field that silently stops correlating across restarts."""
        rec = _Recorder()
        async with _langfuse(rec, key="").span("poller.tick", patient_id=PID, outcome="no_change"):
            pass

        _assert_no_raw_pid(rec)
        metadata = rec.traces[0]["metadata"]
        assert "patient_id" not in metadata, (
            f"unkeyed, the identifier must not leave at all: {metadata}"
        )
        # Everything else still ships — the tick is still diagnosable.
        assert metadata["outcome"] == "no_change"

    async def test_without_a_key_record_verification_still_reports_the_outcome(self) -> None:
        rec = _Recorder()
        _langfuse(rec, key="").record_verification(passed=False, action="withheld", patient_id=PID)

        _assert_no_raw_pid(rec)
        metadata = rec.events[0]["metadata"]
        assert "patient_id" not in metadata
        assert metadata["passed"] is False, "the fail-closed safety metric must survive"
        assert metadata["action"] == "withheld"


# --- 4. in-process behavior and the Noop backend are unchanged ---------------


class TestInProcessBehaviorUnchanged:
    async def test_the_noop_backend_still_accepts_the_real_int_and_records_nothing(self) -> None:
        """The Protocol is untouched: upstream keeps passing the real pid."""
        obs = NoopObservability()
        async with obs.span("poller.tick", patient_id=PID) as span:
            span.set_attribute("patient_id", PID)
            span.set_output({"patient_id": PID})
        assert obs.event("poller.result", patient_id=PID) is None
        assert obs.record_verification(passed=True, action="served", patient_id=PID) is None
        assert obs.record_poller_staleness(patient_id=PID, age_seconds=600) is None
        assert await obs.flush() is None

    async def test_correlation_id_and_span_nesting_survive_the_scrub(self) -> None:
        """The scrub must not disturb the trace shape: the root is still keyed by
        the correlation id, children are still children, and events still attach
        to the enclosing observation rather than orphaning at root."""
        rec = _Recorder()
        obs = _langfuse(rec)
        token = correlation_id_var.set("corr-nest-1")
        try:
            async with obs.span("poller.tick", patient_id=PID):
                obs.event("poller.result", patient_id=PID, outcome="no_change")
        finally:
            correlation_id_var.reset(token)

        assert [t["name"] for t in rec.traces] == ["poller.tick"]
        assert rec.traces[0]["id"] == "corr-nest-1", "trace root keyed by correlation id"
        assert [e["name"] for e in rec.events] == ["poller.result"]
        assert rec.events[0]["trace_id"] == "corr-nest-1", (
            "the event orphaned off the tick's trace — nesting broke"
        )

    async def test_non_patient_metadata_passes_through_untouched(self) -> None:
        """The scrub is targeted: it must not eat unrelated telemetry."""
        rec = _Recorder()
        async with _langfuse(rec).span(
            "doc.ingest",
            patient_id=PID,
            doc_type="lab_pdf",
            correlation_id="corr-ingest-1",
            page_count=3,
            signals=["guidelines", "recommend"],
            nested={"fact_count": 7, "confidence": 0.91},
        ):
            pass

        metadata = rec.traces[0]["metadata"]
        assert metadata["doc_type"] == "lab_pdf"
        assert metadata["correlation_id"] == "corr-ingest-1"
        assert metadata["page_count"] == 3
        assert metadata["signals"] == ["guidelines", "recommend"]
        assert metadata["nested"] == {"fact_count": 7, "confidence": 0.91}

    async def test_telemetry_still_never_breaks_the_caller(self) -> None:
        """A scrub that raised would take down every span it touched."""

        class _BadClient:
            def trace(self, **_: Any) -> Any:
                raise RuntimeError("langfuse down")

            def event(self, **_: Any) -> None:
                pass

            def flush(self) -> None:
                pass

        obs = LangfuseObservability(
            host="https://x",
            public_key="pk",
            secret_key="sk",
            client=_BadClient(),
            pseudonym_key=KEY,
        )
        async with obs.span("poller.tick", patient_id=PID) as span:
            span.set_attribute("patient_id", PID)


# --- the detector's own smoke test -------------------------------------------


class TestHarvestActuallyCatchesALeak:
    """A no-pid-found assertion is only worth what the detector behind it is
    worth: if ``_strings`` silently missed nested structures, every test above
    would pass while the pid shipped. So prove the walk finds the pid in the
    exact shapes the backend really emits, rather than trusting it."""

    def test_the_walk_finds_a_pid_nested_in_event_metadata(self) -> None:
        rec = _Recorder()
        rec.events.append({"name": "x", "metadata": {"payload": {"patient_id": PID}}})
        assert _leaks(rec), "the recursive walk must find a pid nested in a payload"

    def test_the_walk_finds_a_pid_in_an_update_output(self) -> None:
        rec = _Recorder()
        rec.updates.append({"parent": "x", "output": [{"deep": {"pid": PID}}]})
        assert _leaks(rec), "the recursive walk must find a pid inside an output"

    def test_the_walk_finds_a_pid_inside_a_pydantic_model(self) -> None:
        class _M(BaseModel):
            patient_id: int

        rec = _Recorder()
        rec.traces.append({"name": "x", "id": "c", "metadata": {"m": _M(patient_id=PID)}})
        assert _leaks(rec), "the recursive walk must dump models, not skip them"

    def test_the_walk_does_not_false_positive_on_a_longer_number(self) -> None:
        """1015 is a substring of 10150 — the detector must not cry wolf, or the
        real signal drowns."""
        rec = _Recorder()
        rec.traces.append({"name": "x", "id": "c", "metadata": {"latency_ms": 10150}})
        assert not _leaks(rec)

    def test_the_walk_finds_the_pid_embedded_in_a_repr(self) -> None:
        rec = _Recorder()
        rec.events.append({"name": "x", "metadata": {"error": f"no chart for patient {PID}"}})
        assert _leaks(rec), "the str() fallback must catch a pid embedded in prose"
