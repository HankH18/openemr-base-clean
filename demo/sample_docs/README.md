# Sample demo documents (Week-2 ingestion)

Synthetic, uploadable documents for demoing the Week-2 document-ingestion flow
(upload → strict-schema extraction → citations → guideline evidence). **All data
is fictional — no real PHI.** Same fictional patient (Jordan A. Rivera, DOB
1958-03-11) across both, so the two docs tell one coherent story.

| File | `doc_type` | Contents | Ties into |
|---|---|---|---|
| `sample_lab_report.pdf` | `lab_pdf` | Lactate **4.2 H**, Creatinine **1.8 H**, Na 134 L, CO₂ 18 L, Troponin I 0.09 H, WBC **15.8 H**, Hgb 13.5, Platelets 96 L — each with units, reference range, collection date (2026-07-13), abnormal flag | sepsis + AKI guideline corpus (lactate/WBC → sepsis; creatinine → AKI) |
| `sample_intake_form.pdf` | `intake_form` | Demographics, chief concern (fever/confusion/low urine output), current meds (metformin, lisinopril, warfarin, atorvastatin), allergies (penicillin, sulfa), family history (diabetes, HTN/CKD) | the required intake fields; warfarin → anticoagulation guideline |
| `sample_medication_list.pdf` | `medication_list` | Pharmacy medication-reconciliation printout — 6 meds with dose/route/frequency (metformin, lisinopril, warfarin, atorvastatin, insulin glargine, furosemide) | the **third document type**; each med → `lists type='medication'` and feeds the write-back auto-propose bridge |

## How to use

**In the deployed app:** open a patient, use the document-upload control on the
patient hero, pick the file, and set the type (`lab_pdf` / `intake_form`). Real
Claude-vision extraction runs; each extracted fact gets a citation, and a document
citation opens the scanned page with its bounding box drawn.

**Via API** (see `api-collection/`):
```
POST /v1/documents  (multipart)  file=@sample_lab_report.pdf  patient_id=<id>  clinician_id=<id>  doc_type=lab_pdf
GET  /v1/documents/{id}          # status, extracted facts, citations
GET  /v1/documents/{id}/pages/1  # the rendered page image (bbox-overlay backdrop)
```

The clinical picture (elevated lactate + WBC + creatinine, fever/confusion/low
UOP) is deliberately sepsis-with-AKI, so a follow-up chat question retrieves the
matching guideline evidence from the corpus.
