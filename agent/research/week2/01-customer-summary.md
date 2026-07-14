# Clinical Co-Pilot (Week 2) — Customer & Problem Summary

## Who we're building for

The same person Week 1 was built for: a **hospitalist rounding on roughly a dozen admitted
patients before noon**, with about ninety seconds per patient between rooms. They don't have time
to read a chart cover-to-cover; they need to know what changed, what matters today, and whether they
can trust what they're being told. Week 2 sharpens the moment when that chart is *incomplete in a
specific, common way*: the structured EHR data is only half the story, and the half that would
actually change the plan just arrived as an **outside or transfer document** — a faxed outside lab
report scanned to PDF, or an intake/transfer packet the front desk uploaded — sitting in the chart
as an image nobody has reconciled yet.

## The problem

Today the hospitalist either eyeballs that scanned document under time pressure, or defers it and
risks missing a buried critical value, or burns scarce minutes hand-reconciling a fax against what's
already in the EHR. Each path has a real cost: a missed outside troponin or a stale medication on an
intake form can change management; the manual reconciliation eats the ninety-second budget; and an
AI summary of the document is *worse than nothing* if the clinician can't verify it — a confidently
stated hallucination in a clinical setting can directly harm a patient. The gap isn't "read the
document"; it's "read it, reconcile it against the chart, and prove every statement — fast enough to
use between rooms."

## How this solves it

The co-pilot now **sees** the document. It ingests the lab PDF and the intake form, extracts the
clinically important fields into a strict schema, and — critically — links **every extracted fact
back to the exact spot on the page it came from**, with a bounding box you can click to. It surfaces
what's *new or conflicting* versus the existing chart rather than re-transcribing, and when it offers
supporting evidence it draws from a small corpus of the guidelines your service actually follows,
keeping that guideline evidence **visibly separate from patient facts**. Underneath, a small,
inspectable graph of workers handles extraction and evidence retrieval, and a verification layer plus
a critic **refuse to state anything that can't be traced to a source** — a scanned-page region, a
record in the chart, or a cited guideline. Nothing reaches the physician as fact unless it survives
that check. Everything the physician sees is one click from its proof.

## Why it matters

For this user, the difference between a demo and a tool they'd rely on is exactly this: can they
trust it in ninety seconds without leaving the room to double-check. By making the messy input
(scanned, imperfect, incomplete) first-class, keeping the architecture small enough to reason about,
and proving quality with an automated gate that blocks regressions before they ship, the co-pilot
becomes something a hospitalist would actually choose over flipping through the fax themselves — and
something a hospital's CTO could defend putting in front of physicians. When it works, the outside
lab that would have been missed gets flagged, the reconciliation that ate five minutes takes ten
seconds, and every claim comes with a receipt.
