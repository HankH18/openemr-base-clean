"""`${VAR:-}` — the standard compose idiom — must not brick the boot.

Compose hands a container an EMPTY STRING for `${COPILOT_OCR_DPI:-}` when the
operator has not set the var. Pydantic parsed `""` into `int`/`bool` and raised, so
the app did not start. Measured before the fix — 8 of the 9 knobs an operator would
most want to tune::

    COPILOT_OCR_DPI                      FAIL — empty bricks boot
    COPILOT_RASTER_MAX_PAGE_PIXELS       FAIL — empty bricks boot
    COPILOT_RASTER_MAX_PAGES             FAIL — empty bricks boot
    COPILOT_VISION_MAX_PAGES_PER_CALL    FAIL — empty bricks boot
    COPILOT_TLS_VERIFY                   FAIL — empty bricks boot
    COPILOT_CHAT_RETENTION_DAYS          FAIL — empty bricks boot
    COPILOT_SESSION_IDLE_SECONDS         FAIL — empty bricks boot
    COPILOT_SESSION_ABSOLUTE_SECONDS     FAIL — empty bricks boot
    COPILOT_OBSERVABILITY_PSEUDONYM_KEY  OK   — (a str field)

This test exists because the failure mode is "the container does not start", which
no ordinary unit test observes: every test constructs Settings() with a clean env.
It is also how the defect stayed invisible — nothing here ever passed an empty var.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.config import Settings

#: Every non-str knob compose passes with the `${VAR:-}` idiom.
_TYPED_KNOBS = [
    "COPILOT_OCR_DPI",
    "COPILOT_RASTER_MAX_PAGE_PIXELS",
    "COPILOT_RASTER_MAX_PAGES",
    "COPILOT_VISION_MAX_PAGES_PER_CALL",
    "COPILOT_TLS_VERIFY",
    "COPILOT_CHAT_RETENTION_DAYS",
    "COPILOT_SESSION_IDLE_SECONDS",
    "COPILOT_SESSION_ABSOLUTE_SECONDS",
    "COPILOT_AUDIT_RETENTION_YEARS",
]


@pytest.mark.parametrize("var", _TYPED_KNOBS)
def test_an_empty_typed_knob_falls_back_to_its_default(
    var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(var, "")
    settings = Settings()  # must not raise

    field = var.removeprefix("COPILOT_").lower()
    assert getattr(settings, field) == Settings.model_fields[field].default, (
        f"{var}='' must mean 'unset', so the field keeps its default"
    )


def test_a_real_value_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # The guard must not swallow a value the operator actually set — without this,
    # "ignore the env entirely" would pass every test above.
    monkeypatch.setenv("COPILOT_OCR_DPI", "301")
    assert Settings().ocr_dpi == 301


def test_a_malformed_value_still_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only EMPTY means unset. Garbage is still a config error the operator must see;
    # silently defaulting it would hide a typo'd knob.
    monkeypatch.setenv("COPILOT_OCR_DPI", "not-a-number")
    with pytest.raises(ValidationError):
        Settings()


def test_an_empty_string_field_keeps_its_empty_meaning(monkeypatch: pytest.MonkeyPatch) -> None:
    # "" is MEANINGFUL for str settings and must not be swapped for a default: an
    # empty anthropic_api_key selects the keyless stub, and an empty
    # fhir_patient_id_template means "no mapping configured".
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    assert Settings().anthropic_api_key == ""
