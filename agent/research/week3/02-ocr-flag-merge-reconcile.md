# Week 3 · Item 02 — OCR flag-merge reconcile recovery

> **Planning only** (see [README](README.md)). Root-caused 2026-07-19 from the live deploy's stored
> OCR; **deferred from the Week-2 final by user decision** — the current behaviour is SAFE (it never
> invents provenance), so this is a quality/UX improvement, not a correctness fix.

## Problem

A meaningful fraction of correctly-extracted document values render as **"NOT FOUND ON PAGE"** (the
no-invention gate's unsupported state) even though the value is plainly printed. Observed on a real
`lab_pdf`: 8 of 37 facts, including `BUN 38`, `Anion Gap 20`, `Creatinine 1.8`, `Lactate 4.2` (plus
multi-word values: a date `03/11/1958`, `68 / M`, two long comment sentences).

## Root cause (evidence)

The value IS extracted correctly by the vision model, but **Tesseract merges the adjacent
abnormal-flag letter into the numeric value token**, and the no-invention gate's two-sided coverage
(`reconcile._coverage_ok`) then correctly refuses the match. From the deploy's stored
`document_page.ocr_tokens`:

| Fact value (VLM) | OCR token | conf | Reconcile |
|---|---|---|---|
| `168` (glucose) | `168` | 0.90 | ✅ found (clean token) |
| `38` (BUN) | `38H` | 0.78 | ❌ value covers only 67% of `38H` |
| `20` (anion gap) | `20H` / `20-32` | 0.80 | ❌ same, plus a ref-range token |
| `1.8` (creatinine) | `1.88` | 0.41 | ❌ genuine OCR misread |
| `4.2` (lactate) | `4.24` | 0.20 | ❌ genuine OCR misread |

The two-sided coverage guard is CORRECT and must stay — it is what stops `20` matching `20-32` or
`2024`, i.e. it prevents fabricated provenance. Two distinct sub-cases:

- **Flag-merge** (`38H`, `20H`): the value is truly present; only a trailing 1–2 letter lab flag
  (`H`/`L`/`A`/`HH`/`LL`) is glued on. **Safe to recover.**
- **Misread** (`1.88`, `4.24`): the OCR text genuinely differs from the value; the gate rejecting is
  **correct** — do NOT "recover" these (that would be inventing provenance). Better OCR is the only
  real fix, and it's out of scope here.

## Proposed fix (recover the safe case only)

At OCR post-process (`copilot/documents/ocr.py`), when a token matches
`^(\d+(?:\.\d+)?)([HLA]{1,2})$` (a number immediately followed by 1–2 known lab-flag letters),
**split it into two tokens** — the numeric core and the flag — apportioning the bbox by character
width. Reconcile then matches the clean numeric token; the flag becomes its own (also-present)
token. This touches ONLY OCR normalization, not the coverage / no-invention logic, so the safety
gate is unchanged.

*Alternative considered + rejected for now:* relaxing `_coverage_ok` to ignore a trailing flag —
rejected because it edits the core safety gate directly (higher blast radius).

Multi-word values (dates, `68 / M`, long comments) are a separate, softer sub-problem — the gate's
span-coverage over an OCR that splits/reorders punctuation. Not addressed by the flag-split; track
separately if it matters after the flag-merge win.

## Constraints / test gates

- Must NOT regress the **frozen ingestion acceptance** (`.swarm-loop/acceptance/run.py --feature
  ingestion` = 8) or the reconcile/bbox unit + render-fixture tests.
- Must NOT recover genuine misreads — add a case pinning that `4.24` stays unsupported for value `4.2`.
- Regenerate the field-render + Twig fixtures if the supported/unsupported set shifts
  (`openemr-cmd update-layout-field-fixtures`, `composer update-twig-fixtures`).

## Not in scope

A different/dual-pass OCR engine — larger effort, its own item.
