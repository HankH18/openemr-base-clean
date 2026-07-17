"""Nothing bounded the pages sent per vision call; a long document broke the API.

``ClaudeVision.extract`` appended EVERY page of a document as a base64 image into
a single ``messages.create``. ``_MAX_TOKENS`` bounds the model's OUTPUT; there
was no input bound at all, so a 300-page upload meant 300 images in one request
— which fails past the API's per-request image limit, and fails only AFTER the
rasterize + OCR work, wasting the whole ingest.

The bound is per CALL, and a document past it is batched and merged rather than
refused: a 60-page discharge summary is ordinary paperwork. That choice is what
these tests mostly guard, because merging is where a naive implementation
silently corrupts data:

- ``page_no`` must stay TRUE across batches. A second batch's model sees images
  it would naturally number 1..N, but they are pages 21..40. Downstream,
  ``pipeline._reconcile_facts`` searches the OCR tokens of the page a fact
  names, so an off-by-batch page number mis-verifies every fact in the batch —
  silently. Pages are labelled and the echo is checked.
- A header field present in two batches with DIFFERENT values must not be
  silently coerced to one of them.

The image count is asserted against the RECORDED CALL PAYLOAD, not the return
value: the return value cannot show how many images went over the wire, which is
the entire defect.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from copilot.config import Settings
from copilot.documents.raster import RasterizedPage
from copilot.documents.vision import ClaudeVision, DocumentType, VisionExtractionError
from copilot.domain.documents import IntakeForm, LabReport


class _Block:
    def __init__(self, payload: Any) -> None:
        self.type = "tool_use"
        self.name = "record_extraction"
        self.input = payload


class _Response:
    def __init__(self, payload: Any) -> None:
        self.content = [_Block(payload)]


class _RecordingMessages:
    """Returns one queued payload per call and records every call's kwargs."""

    def __init__(self, payloads: list[Any]) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._payloads:
            raise AssertionError("vision made more model calls than the test queued")
        return _Response(self._payloads.pop(0))


class _RecordingClient:
    def __init__(self, payloads: list[Any]) -> None:
        self.messages = _RecordingMessages(payloads)


def _pages(count: int, *, first: int = 1) -> list[RasterizedPage]:
    return [
        RasterizedPage(page_no=n, image=b"\x89PNG\r\n\x1a\n", width=1700, height=2200)
        for n in range(first, first + count)
    ]


def _vision(payloads: list[Any], *, max_pages: int) -> tuple[ClaudeVision, _RecordingClient]:
    settings = Settings(anthropic_api_key="sk-test", vision_max_pages_per_call=max_pages)
    client = _RecordingClient(payloads)
    return ClaudeVision(settings, client=client), client


def _images(call: dict[str, Any]) -> list[dict[str, Any]]:
    content = call["messages"][0]["content"]
    return [block for block in content if block["type"] == "image"]


def _lab(*values: str, **header: Any) -> dict[str, Any]:
    return {"facts": [{"field_path": f"analyte_{v}", "value": v} for v in values], **header}


# --- the bound holds, measured where it actually matters --------------------


def test_no_model_call_ever_carries_more_images_than_the_bound() -> None:
    # THE defect, asserted against the recorded payload rather than the return
    # value: the old code put all 47 pages in one request. The return value
    # looked identical either way, which is why this went unnoticed.
    vision, client = _vision([_lab("1"), _lab("2"), _lab("3")], max_pages=20)

    anyio.run(vision.extract, _pages(47), DocumentType.lab_pdf)

    counts = [len(_images(call)) for call in client.messages.calls]
    assert counts == [20, 20, 7], f"expected batches of <=20, got {counts}"
    assert all(n <= 20 for n in counts), "a single call exceeded the page bound"
    assert sum(counts) == 47, "every page must still be sent exactly once — no page dropped"


def test_a_document_within_the_bound_is_still_exactly_one_call() -> None:
    # Regression guard: the common case must not become N calls.
    vision, client = _vision([_lab("13.5")], max_pages=20)

    report = anyio.run(vision.extract, _pages(20), DocumentType.lab_pdf)

    assert len(client.messages.calls) == 1, "a within-bound document must be a single call"
    assert len(_images(client.messages.calls[0])) == 20
    assert isinstance(report, LabReport)
    assert [f.value for f in report.facts] == ["13.5"]


def test_the_bound_is_configurable_via_settings() -> None:
    assert Settings().vision_max_pages_per_call == 20, "default must match the measured limit"
    vision, client = _vision([_lab("a"), _lab("b"), _lab("c")], max_pages=2)

    anyio.run(vision.extract, _pages(5), DocumentType.lab_pdf)

    assert [len(_images(c)) for c in client.messages.calls] == [2, 2, 1]


# --- page_no stays true across batches (the silent-corruption bug) ----------


def test_page_no_is_preserved_for_facts_from_the_second_batch() -> None:
    # The bug a naive chunker ships: batch 2's facts really are on pages 21-40,
    # and the reconciler uses page_no to pick which page's tokens to search.
    vision, _ = _vision(
        [
            {"facts": [{"field_path": "hgb", "value": "13.5", "page_no": 1}]},
            {"facts": [{"field_path": "wbc", "value": "7.2", "page_no": 21}]},
        ],
        max_pages=20,
    )

    report = anyio.run(vision.extract, _pages(21), DocumentType.lab_pdf)

    by_field = {f.field_path: f.page_no for f in report.facts}
    assert by_field == {"hgb": 1, "wbc": 21}, (
        "a second-batch fact must keep its TRUE page number — renumbering it to 1 "
        "would make the reconciler search the wrong page's tokens"
    )


def test_each_image_is_labelled_with_its_true_page_number() -> None:
    # The mechanism that makes the above possible: batch 2's model is TOLD its
    # images are pages 21-23, otherwise it would number them 1-3.
    vision, client = _vision([_lab("a"), _lab("b")], max_pages=20)

    anyio.run(vision.extract, _pages(23), DocumentType.lab_pdf)

    second = client.messages.calls[1]["messages"][0]["content"]
    labels = [b["text"] for b in second if b["type"] == "text" and b["text"].startswith("Page ")]
    assert labels == ["Page 21:", "Page 22:", "Page 23:"], (
        "the second batch must state its pages' real numbers, not restart at 1"
    )


def test_a_fact_claiming_a_page_the_call_never_carried_is_refused() -> None:
    # Never trust the echo. The model cannot have read page 99 from a call that
    # carried pages 21-22, so that page_no is fabricated provenance.
    vision, _ = _vision(
        [
            _lab("a"),
            {"facts": [{"field_path": "wbc", "value": "7.2", "page_no": 99}]},
        ],
        max_pages=20,
    )

    with pytest.raises(VisionExtractionError, match="page_no=99"):
        anyio.run(vision.extract, _pages(22), DocumentType.lab_pdf)


def test_a_fact_without_a_page_no_is_left_alone() -> None:
    # "I did not record which page" is honest, and is what the single-call path
    # has always allowed — the guard must not start inventing a number.
    vision, _ = _vision([_lab("a"), {"facts": [{"field_path": "wbc", "value": "7.2"}]}], max_pages=20)

    report = anyio.run(vision.extract, _pages(21), DocumentType.lab_pdf)

    assert [f.page_no for f in report.facts] == [None, None]


# --- header merge: present beats absent, conflict refuses -------------------


def test_a_header_field_present_in_only_one_batch_survives_the_merge() -> None:
    # patient_name is printed once, on page 1 — every later batch honestly says
    # None. Letting present beat absent is not invention; it is the one batch
    # that could read it doing so.
    vision, _ = _vision(
        [
            {
                "facts": [{"field_path": "d.0", "value": "RIVERA", "category": "demographic"}],
                "patient_name": "RIVERA, JORDAN A.",
                "date_of_birth": "03/11/1958",
            },
            {"facts": [{"field_path": "m.0", "value": "Metformin", "category": "medication"}]},
        ],
        max_pages=20,
    )

    form = anyio.run(vision.extract, _pages(21), DocumentType.intake_form)

    assert isinstance(form, IntakeForm)
    assert form.patient_name == "RIVERA, JORDAN A."
    assert form.date_of_birth == "03/11/1958"
    assert len(form.facts) == 2, "facts from both batches must survive"


def test_conflicting_header_fields_across_batches_raise_and_never_pick_a_winner() -> None:
    # The documented behaviour, and the reason it is not "first wins": two
    # different patient_names may mean two patients' documents in one upload,
    # and silently keeping the first would file patient B's facts under A.
    vision, _ = _vision(
        [
            {
                "facts": [{"field_path": "d.0", "value": "RIVERA", "category": "demographic"}],
                "patient_name": "RIVERA, JORDAN A.",
            },
            {
                "facts": [{"field_path": "d.1", "value": "OKONKWO", "category": "demographic"}],
                "patient_name": "OKONKWO, AMARA",
            },
        ],
        max_pages=20,
    )

    with pytest.raises(VisionExtractionError, match="patient_name") as excinfo:
        anyio.run(vision.extract, _pages(21), DocumentType.intake_form)

    message = str(excinfo.value)
    assert "RIVERA" not in message and "OKONKWO" not in message, (
        "the conflict message must name the field but never the values — it is PHI "
        "and this text travels into logs and traces"
    )


def test_an_identical_header_field_in_both_batches_is_not_a_conflict() -> None:
    # A header repeated on every page (very common on real scans) agrees with
    # itself; that must merge quietly rather than raise.
    vision, _ = _vision(
        [
            {"facts": [{"field_path": "a", "value": "1"}], "specimen": "serum"},
            {"facts": [{"field_path": "b", "value": "2"}], "specimen": "serum"},
        ],
        max_pages=20,
    )

    report = anyio.run(vision.extract, _pages(21), DocumentType.lab_pdf)

    assert report.specimen == "serum"


# --- the merged document is still strictly validated ------------------------


def test_a_batch_that_reads_nothing_does_not_fail_the_document() -> None:
    # Blank/signature continuation pages are real. The min_length=1 floor is a
    # statement about the DOCUMENT, so it is enforced on the merge, not per
    # batch — otherwise "pages 21-40 are the disclaimer" kills the ingest.
    vision, _ = _vision([_lab("13.5"), {"facts": []}], max_pages=20)

    report = anyio.run(vision.extract, _pages(21), DocumentType.lab_pdf)

    assert [f.value for f in report.facts] == ["13.5"]


def test_a_document_where_every_batch_reads_nothing_still_fails_loudly() -> None:
    # Proof the above did not loosen the floor: an empty extraction is never an
    # honest read of a document, and must still raise.
    from pydantic import ValidationError

    vision, _ = _vision([{"facts": []}, {"facts": []}], max_pages=20)

    with pytest.raises(ValidationError):
        anyio.run(vision.extract, _pages(21), DocumentType.lab_pdf)


def test_a_wrong_typed_value_in_a_later_batch_still_raises() -> None:
    # Strictness survives merging: batch 2's numeric value is not coerced.
    from pydantic import ValidationError

    vision, _ = _vision(
        [_lab("13.5"), {"facts": [{"field_path": "wbc", "value": 7.2, "page_no": 21}]}],
        max_pages=20,
    )

    with pytest.raises(ValidationError):
        anyio.run(vision.extract, _pages(21), DocumentType.lab_pdf)


def test_destringify_and_drop_extras_still_apply_to_a_later_batch() -> None:
    # Both are per-response behaviours pinned by test_vision_destringify.py and
    # test_vision_null_extras.py against REAL model output. Batching must not
    # quietly apply them to only the first call.
    vision, _ = _vision(
        [
            _lab("13.5"),
            {
                "facts": '[{"field_path": "wbc", "value": "7.2", "page_no": 21}]',
                "invented_key": None,
            },
        ],
        max_pages=20,
    )

    report = anyio.run(vision.extract, _pages(21), DocumentType.lab_pdf)

    assert [f.value for f in report.facts] == ["13.5", "7.2"], (
        "a stringified facts payload in the SECOND batch must still be recovered"
    )
    assert [f.page_no for f in report.facts] == [None, 21]
