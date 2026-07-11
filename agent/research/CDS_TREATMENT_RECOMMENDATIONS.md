# Feasibility Brief: A "Recommended Course of Treatment" Panel in AgentForge Clinical Co-Pilot

> **DISCLAIMER — ENGINEERING RESEARCH, NOT LEGAL ADVICE.**
> This document is internal engineering and product research produced to inform
> design decisions. It is **not legal advice, not regulatory advice, and not a
> substitute for review by qualified FDA regulatory counsel and health-care
> attorneys licensed in the relevant jurisdictions.** Statutory and guidance
> interpretations below are the author's reading of secondary sources and, where
> noted, could not be verified against the primary FDA PDF at time of writing
> (the FDA media downloads returned HTTP errors to the research tool). Before any
> "treatment recommendation" feature is shipped, obtain a formal device-status
> determination and liability review. The regulatory landscape is moving fast:
> FDA revised its CDS guidance in **January 2026**, superseding the widely-cited
> 2022 version, and further AI-specific rulemaking is expected.

---

## Executive Summary

Surfacing an AI-generated "recommended course of treatment" is legally and
clinically the single highest-risk thing AgentForge could do, and it moves the
product from clearly-benign chart summarization toward the boundary of an FDA
**medical device**. Under the Cures Act device carve-out for clinical decision
support (§520(o)(1)(E)), a treatment-recommendation function can stay a
*non-device* only if it satisfies four criteria — the load-bearing one being
Criterion 4: the physician must be able to **independently review the basis** of
the recommendation and not rely primarily on the software. An LLM's opaque,
non-deterministic reasoning is in direct tension with that criterion, and recent
evidence shows LLMs readily emit device-like directives even when instructed not
to ([npj Digital Medicine 2025](https://www.nature.com/articles/s41746-025-01544-y)).
The good news: AgentForge's core architecture — every claim tied to a live,
re-fetched FHIR record via a structured `FhirReference`, a deterministic
fail-closed verification gate, provenance chips, and strict read-only access —
is *precisely* the kind of transparency-and-grounding design that best supports
Criterion 4 and mitigates automation bias. The gap is that AgentForge today
verifies only that **stated facts match the record**; it has **no mechanism to
judge whether a recommended action is clinically appropriate**, and it carries
**no medical-advice / decision-support framing** in its UI. Recommendation:
either (a) do **not** ship an autonomous treatment recommendation, and instead
ship a "guideline-linked considerations" panel that surfaces *cited evidence and
options* for the physician to weigh (keeping the feature squarely in the
lower-risk "information to a physician who decides" category), or (b) treat a
true recommendation feature as a regulated-device track with counsel, a
Predetermined Change Control Plan, and HTI-1-style source-attribute
transparency. This brief lays out the legal basis, the UX/safety literature, and
a concrete mapping to AgentForge's existing design.

---

# Part 1 — Legal / Regulatory Landscape (US-focused)

## 1.1 The statutory frame: Cures Act §3060 and the CDS device carve-out

The **21st Century Cures Act (2016), §3060**, amended §520 of the Federal Food,
Drug, and Cosmetic Act (FD&C Act) by adding **§520(o)**, which removes five
categories of software from the statutory definition of a "device." One of those
is **§520(o)(1)(E)** — certain **clinical decision support (CDS)** software.
Software that falls inside the carve-out is *not a device* and is therefore
outside FDA premarket/postmarket device authority; software that falls **outside**
it is a device and (depending on risk) may require clearance/approval and a
quality system.
([21st Century Cures Act overview](https://en.wikipedia.org/wiki/21st_Century_Cures_Act);
[FDLI, "Cures Act Provides (Some) Clarity on FDA's Regulation of Software"](https://www.fdli.org/2017/04/21st-century-cures-act-provides-clarity-fdas-regulation-software/);
[FDA, "Changes to Existing Medical Software Policies Resulting from Section 3060"](https://www.fda.gov/regulatory-information/search-fda-guidance-documents/changes-existing-medical-software-policies-resulting-section-3060-21st-century-cures-act))

### The four criteria (all must be met to be Non-Device CDS)

FDA's interpretation of §520(o)(1)(E) tracks four statutory criteria. As quoted
in a contemporaneous law-firm analysis of the September 2022 final guidance
([Goodwin](https://www.goodwinlaw.com/en/insights/blogs/2022/10/fda-issues-final-clinical-decision-support-software-guidance)):

1. **Criterion 1 — Data type.** The software is *"not intended to acquire,
   process, or analyze a medical image or a signal from an in vitro diagnostic
   device (IVD) or a pattern or signal from a signal acquisition system."*
2. **Criterion 2 — Display of medical information.** It is *"intended for the
   purpose of displaying, analyzing, or printing medical information about a
   patient or other medical information (such as peer-reviewed clinical studies
   and clinical practice guidelines)."*
3. **Criterion 3 — Recommendation to an HCP.** It is *"intended for the purpose
   of supporting or providing recommendations to a health care professional
   (HCP) about prevention, diagnosis, or treatment of a disease or condition."*
4. **Criterion 4 — Independent review of the basis.** It is *"intended for the
   purpose of enabling such HCP to independently review the basis for the
   recommendations that such software presents so that it is not the intent that
   the HCP rely primarily on any of such recommendations to make a clinical
   diagnosis or treatment decision regarding an individual patient."*

Criterion 4 is the crux for any AI treatment-recommendation feature, and the one
most in tension with LLM opacity.

## 1.2 The September 2022 final guidance (the interpretation most literature cites)

On **September 28, 2022**, FDA issued the final guidance *"Clinical Decision
Support Software,"* interpreting the four criteria
([Federal Register notice](https://www.federalregister.gov/documents/2022/09/28/2022-20993/clinical-decision-support-software-guidance-for-industry-and-food-and-drug-administration-staff)).
Two interpretive moves in the 2022 guidance are directly relevant to a treatment
panel:

- **Criterion 3 was narrowed.** FDA said software that provides *"a specific
  preventive, diagnostic, or treatment output or directive"* does **not** meet
  Criterion 3; to stay non-device the output generally had to be *"multiple
  options or a list of recommendations"* for the HCP's consideration. FDA also
  said software *"intended to support time-critical decision-making"* falls
  **outside** the carve-out (i.e., is a device). Both moves rest explicitly on an
  **automation-bias** rationale — the concern that a single directive in a
  time-pressured moment will be followed without independent scrutiny.
  ([Covington, "5 Key Takeaways"](https://www.cov.com/en/news-and-insights/insights/2022/10/5-key-takeaways-from-fdas-final-guidance-on-regulation-of-clinical-decision-support-software-fda-outlines-significant-changes-for-cds))
- **Criterion 4 disclosure expanded.** To let an HCP "independently review the
  basis," the 2022 guidance said developers must disclose *"the inputs used to
  generate the recommendation... [and] the basis for the recommendation,"*
  including for algorithm-driven CDS a description of *"algorithm methods,
  datasets and validation, including a description of the results from clinical
  studies."* The more the logic is a **"black box"** to the HCP, the more likely
  FDA treats it as a device.
  ([Covington](https://www.cov.com/en/news-and-insights/insights/2022/10/5-key-takeaways-from-fdas-final-guidance-on-regulation-of-clinical-decision-support-software-fda-outlines-significant-changes-for-cds))

## 1.3 IMPORTANT — FDA revised the CDS guidance in January 2026 (this supersedes 2022)

Because this brief is written in mid-2026, the **operative** FDA guidance is the
**January 2026 revision** of "Clinical Decision Support Software," which
*supersedes* the 2022 version. Product decisions should be made against the 2026
guidance, not the 2022 text. Key documented changes (triangulated across multiple
law-firm analyses; the primary FDA PDF was not retrievable by the research tool,
so treat verbatim quotes as second-hand):

- **Single recommendations may now qualify (enforcement discretion).** FDA
  reversed the 2022 "must be a list of options" posture. It will exercise
  **enforcement discretion** where the software provides *a single output or
  recommendation in scenarios where only a single option is "clinically
  appropriate."* This is a meaningful loosening — a single treatment suggestion
  is no longer automatically device-triggering.
  ([Latham & Watkins](https://www.lw.com/en/insights/fda-issues-updated-guidance-loosening-regulatory-approach-to-certain-digital-health-tools);
  [Arnold & Porter, Jan 2026](https://www.arnoldporter.com/en/perspectives/advisories/2026/01/fda-cuts-red-tape-on-clinical-decision-support-software);
  [Faegre Drinker, Jan 2026](https://www.faegredrinker.com/en/insights/publications/2026/1/key-updates-in-fdas-2026-general-wellness-and-clinical-decision-support-software-guidance))
- **Automation bias moved to Criterion 4.** FDA relocated its automation-bias
  discussion from Criterion 3 to Criterion 4, reframing it as a *transparency /
  independent-review* problem rather than an *output-format* problem. (Sources
  differ on emphasis: [Cooley](https://www.cooley.com/news/insight/2026/2026-01-20-automation-bias-and-clinical-practice-fda-makes-incremental-updates-to-clinical-decision-support-software-guidance)
  reads it as retained-but-relocated with thin scientific support; [Latham](https://www.lw.com/en/insights/fda-issues-updated-guidance-loosening-regulatory-approach-to-certain-digital-health-tools)
  reads prior automation-bias language as substantially pared back. This
  disagreement itself is worth flagging to counsel.)
- **Greater emphasis on transparency/explainability for AI-driven CDS.** Per
  Faegre Drinker, the 2026 guidance *"places greater emphasis on transparency
  regarding data inputs, underlying logic, and how recommendations are generated
  — particularly for algorithmic or AI-driven CDS,"* and reiterates the black-box
  principle: *"the greater the extent to which the software is a 'black box' to
  HCPs, the greater the risk that FDA will assert the product is a medical
  device."* Criterion 4's touchstone is whether *"the HCP can understand the
  basis of the recommendation," regardless of whether AI/ML generated it*
  ([Latham](https://www.lw.com/en/insights/fda-issues-updated-guidance-loosening-regulatory-approach-to-certain-digital-health-tools)).

**Net effect for AgentForge:** the 2026 loosening is genuinely helpful — a single
grounded treatment suggestion is not per se a device anymore — but it *raises the
bar on transparency/explainability*, which is exactly where LLM opacity is
weakest and where AgentForge's provenance design is strongest.

## 1.4 Does an LLM-generated recommendation jeopardize the carve-out?

**Short answer: yes, materially, unless the transparency problem is engineered
around.** The carve-out lives or dies on Criterion 4 — the physician's ability to
*independently review the basis* and not *rely primarily* on the output. A
transparent rule engine ("recommend anticoagulation because CHA₂DS₂-VASc = 4,
here is the score and its inputs") satisfies this cleanly: the logic is
inspectable and reproducible. An LLM does not, for three reasons:

1. **Opaque, non-deterministic reasoning.** The model's actual basis for a
   recommendation is not the cited records — it is billions of weights. A
   citation shows *what data exists*, not *why the model concluded a given
   treatment follows from that data*. FDA's black-box principle cuts directly
   against this.
2. **Empirical evidence LLMs produce device-like directives even when told not
   to.** A 2025 npj Digital Medicine study tested GPT-4 and Llama-3 against the
   FDA CDS criteria and found that in emergency scenarios **100% of GPT-4 (and
   52% of Llama-3) responses were consistent with device-like decision support**,
   and that *"single- and multi-shot prompts based on the text of FDA guidance
   for CDSS device criteria are insufficient to align LLM output with non-device
   decision support."* The models emitted specific diagnoses/treatment directives
   for time-critical situations and gave *little transparency into their
   reasoning* — "exactly violating the independent review criterion."
   ([npj Digital Medicine, 2025](https://www.nature.com/articles/s41746-025-01544-y))
3. **Automation bias is worst for a single confident directive.** The very output
   shape a "recommended course of treatment" panel produces (one authoritative
   suggestion, at the top of the page) is the shape most likely to be adopted
   without scrutiny — the concern FDA anchored the carve-out on
   ([automation-bias systematic review, PMC3240751](https://pmc.ncbi.nlm.nih.gov/articles/PMC3240751/)).

**Where AgentForge's design *helps* satisfy Criterion 4:** its grounding model
ties every clinical assertion to a structured, re-fetched FHIR record (see Part
3). That gives the physician the *evidentiary substrate* to review — "this
suggestion references your patient's INR of 3.8 on this date, this active
warfarin order, this documented fall." That is exactly the "inputs used to
generate the recommendation" that Criterion 4 wants surfaced, and it is far more
than most LLM products expose.

**Where it *falls short*:** grounding shows the *facts the recommendation draws
on*, not the *clinical logic connecting facts to the recommended action*. A
physician can verify "yes, the INR is 3.8" but still cannot inspect *why the
model recommends holding warfarin vs. a bridge plan vs. vitamin K*. The
model-reasoning gap between "cited facts" and "recommended action" is precisely
the part Criterion 4 asks the HCP to be able to review independently — and it is
the part an LLM cannot expose faithfully. Citing sources satisfies the
*information-provenance* half of transparency but not the *reasoning-provenance*
half.

## 1.5 Practice of medicine, learned intermediary, and liability

- **The physician remains the decision-maker and the "learned intermediary."**
  Even with AI in the loop, *"the physician will almost certainly remain the
  legal learned intermediary, the person the law holds responsible for the final
  clinical judgment."* This keeps a physician-facing suggestion in a familiar
  liability frame — but only if the physician's review is *meaningful*, not
  *nominal*.
  ([Weiss, "When the Machine Speaks"](https://blog.weisspc.com/ai-physician-liability-learned-intermediary/);
  [Winston & Strawn, "A New Intermediary"](https://www.winstontaylor.com/insights/a-new-intermediary-artificial-intelligence-and-the-learned-intermediary-doctrine))
- **Dual liability trap.** Physicians face exposure in *both* directions:
  **over-reliance** (following AI blindly — *"the 'AI told me to do it' defense
  is unlikely to succeed if the physician could not articulate why the
  recommendation made clinical sense"*) and, as tools become validated,
  **under-reliance** (ignoring an accurate alert). Surfacing a prominent
  treatment recommendation *increases* both surfaces of exposure.
  ([Weiss](https://blog.weisspc.com/ai-physician-liability-learned-intermediary/))
- **Standard of care may evolve to include AI.** What counts as "reasonable"
  practice can shift as tools are adopted; a recommendation that is wrong *and*
  followed, or right *and* ignored, both create malpractice questions.
  ([AMA Journal of Ethics](https://journalofethics.ama-assn.org/article/are-current-tort-liability-doctrines-adequate-addressing-injury-caused-ai/2019-02);
  [Medical Economics](https://www.medicaleconomics.com/view/the-new-malpractice-frontier-who-s-liable-when-ai-gets-it-wrong-))
- **Product liability for the software maker.** The learned-intermediary doctrine
  historically shields upstream makers by routing the duty-to-warn through the
  physician — but commentators expect courts to *"carve out exceptions, require
  enhanced warnings, and scrutinize whether physician review is meaningful or
  merely nominal."* A vendor whose UI encourages rubber-stamping (auto-actioned,
  one-click accept, no visible basis) erodes the very doctrine that protects it.
  ([Winston & Strawn](https://www.winstontaylor.com/insights/a-new-intermediary-artificial-intelligence-and-the-learned-intermediary-doctrine))
- **Documentation standard.** A practical safeguard emerging in the literature:
  the chart should record *"what the AI suggested, whether the physician agreed
  or disagreed, and why."* This both supports the "meaningful review" defense and
  is a concrete AgentForge feature to consider (log suggestion + physician
  disposition).
  ([Weiss](https://blog.weisspc.com/ai-physician-liability-learned-intermediary/))
- **Institutional liability.** Hospitals that select, configure, and train on the
  tool bear independent exposure — relevant to how AgentForge is *deployed* and
  documented for customers.
  ([Weiss](https://blog.weisspc.com/ai-physician-liability-learned-intermediary/))

## 1.6 Adjacent regimes (brief)

- **HIPAA.** AgentForge reads PHI via FHIR and must operate under a Business
  Associate Agreement with appropriate safeguards; its append-only PHI-read audit
  trail (see Part 3) maps to the HIPAA Security Rule audit-controls requirement
  (45 CFR §164.312(b)). Sending PHI to a third-party LLM API (Anthropic) requires
  a BAA and data-handling review.
- **ONC information blocking + HTI-1 algorithm transparency.** The **ONC HTI-1
  final rule** (published Jan 8, 2024; effective Feb 8, 2024) renamed the CDS
  certification criterion to **Decision Support Interventions (DSI)** and, for
  **Predictive DSI**, requires certified health IT to surface **31 "source
  attributes"** across nine categories (development details, validation,
  fairness, ongoing maintenance, performance measures, etc.) so users can
  understand how a model was trained and validated. Compliance was required by
  **Dec 31, 2024**. Predictive DSI is defined broadly — *"technology that supports
  decision-making based on algorithms or models that... produce an output that
  results in prediction, classification, recommendation, evaluation, or
  analysis."* An AI treatment recommendation squarely fits that definition. Note
  a nuance: HTI-1 binds ONC-**certified** health IT modules; whether AgentForge
  (a separate service on an OpenEMR fork) is itself "certified health IT" is a
  fact question — but the 31-source-attribute framework is the de facto
  transparency yardstick regulators and buyers will apply, and AgentForge should
  design toward it regardless.
  ([AHIMA, DSI certification criteria](https://www.ahima.org/education-events/artificial-intelligence/artificial-intelligence-regulatory-resource-guide/onc-decision-support-interventions-certification-criteria/);
  [Mintz, HTI-1 transparency](https://www.mintz.com/insights-center/viewpoints/2146/2024-01-08-hhs-onc-hti-1-final-rule-introduces-new-transparency);
  [Akin, ONC predictive DSI](https://www.akingump.com/en/insights/alerts/onc-steps-into-ai-regulation-finalizing-extensive-requirements-for-predictive-decision-support-interventions-and-makes-significant-updates-to-information-blocking-regulations);
  [ONC DSI fact sheet](https://www.healthit.gov/sites/default/files/page/2023-12/HTI-1_DSI_fact%20sheet_508.pdf))
- **State-law variation.** States are legislating AI-in-health directly (e.g.,
  disclosure-to-patient requirements, "corporate practice of medicine" limits,
  and rules on AI in utilization-management/coverage decisions). This is a moving
  target; jurisdiction-specific counsel is required before multi-state
  deployment.
  ([AMA state AI advocacy brief, 2024](https://www.medchi.org/Portals/18/AMA%20ARC%20AI%20Policy%20Issue%20Brief_2024-12.pdf))
- **EU / other (brief).** The **EU AI Act** classifies AI used as a medical-device
  safety component or for medical decision-making as **high-risk**, layering
  conformity-assessment, transparency, human-oversight, and logging obligations
  on top of the EU **MDR** — a stricter regime than the US CDS carve-out. Any EU
  ambition should assume high-risk classification for a treatment-recommendation
  feature.

## 1.7 Risk gradient: information-to-a-physician vs. autonomous / patient-facing

The single most important risk lever is **who decides and who sees it**:

| Configuration | Regulatory / liability posture |
|---|---|
| **Information/options presented to a licensed physician who independently decides** (AgentForge's intended mode) | **Lower risk.** Best fit for the §520(o)(1)(E) non-device carve-out *if* Criterion 4 (independent review) is genuinely met. Physician is the learned intermediary. |
| **Single directive, time-critical, auto-actioned, or one-click accept** | **Higher risk.** Undercuts Criterion 4 and "meaningful review"; automation-bias magnet; edges toward device status even under the looser 2026 guidance. |
| **Patient-facing or autonomous** (no clinician in the loop) | **Highest risk.** Outside the carve-out (carve-out is HCP-facing by its terms); treat as a regulated device and full liability exposure. |

AgentForge must stay firmly in row 1. Everything in Part 2 and Part 3 is about
keeping it there.

---

# Part 2 — UX / Safety Best Practices for Presenting Volatile AI Suggestions

The clinical-informatics and human-factors literature converges on a clear
principle: **the way a suggestion is presented determines whether the physician
critically evaluates it or rubber-stamps it.** Presentation is a safety control,
not cosmetics.

## 2.1 Automation bias and complacency

**Automation bias** is *"the tendency to over-rely on automation... as a heuristic
replacement of vigilant information seeking."* It is measurable and material: a
meta-analysis found erroneous advice was followed **26% more often** in CDS
groups than controls, and negative-consultation rates (clinician flips a *correct*
decision to *incorrect* after bad advice) run **6–11%**. It is worse under **high
workload and time pressure** — i.e., exactly a hospitalist's rounding context.
([Automation-bias systematic review, PMC3240751](https://pmc.ncbi.nlm.nih.gov/articles/PMC3240751/);
[Automation complacency, Springer 2025](https://link.springer.com/article/10.1007/s43681-025-00825-2))

**Evidence-based mitigators (design directly to these):**

- **Emphasize accountability** — remind the user they are responsible for the
  decision; accountability framing reduces automation bias.
- **Position / prominence matters** — *"display prominence increased automation
  bias"*: a prominent incorrect suggestion is more likely to be followed. This is
  a direct caution against putting an unverified treatment directive **at the top
  of the patient page** in an authoritative visual style.
- **Show dynamic, per-item confidence** — updating the confidence attached to each
  piece of advice (rather than a fixed system-wide "trust me") improved
  appropriately-calibrated reliance.
- **Information, not commands** — *"providing supportive information rather than
  commands"* reduces over-reliance. Frame as considerations/evidence, not "do X."
- **Reduce on-screen clutter / detail overload** — less noise, better scrutiny.
  ([PMC3240751](https://pmc.ncbi.nlm.nih.gov/articles/PMC3240751/))

## 2.2 Alert fatigue

Interruptive alerts have **very low acceptance (4–11%)** and drive fatigue,
distrust, and override-by-reflex; alert frequency is itself a risk factor for
error. A treatment-recommendation panel that fires constantly, or that
interrupts, will be tuned out — and tuning-out is indiscriminate (the one alert
that mattered gets dismissed with the rest). Favor **non-interruptive, on-demand,
context-anchored** presentation over modal pop-ups.
([CDS stewardship, PMC9132737](https://pmc.ncbi.nlm.nih.gov/articles/PMC9132737/);
[Alert fatigue & workload, PMC5387195](https://pmc.ncbi.nlm.nih.gov/articles/PMC5387195/))

## 2.3 The "Five Rights" of CDS (Osheroff / AHRQ)

The canonical CDS design framework: deliver the **right information**, to the
**right person**, in the **right format**, through the **right channel**, at the
**right time in the workflow.** For a treatment panel: right information =
evidence-backed and patient-specific; right person = the attending physician;
right format = *options + rationale + citations*, not a bare directive; right
channel = in the chart context, not a page-blocking modal; right time = at review,
not mid-order in a way that forecloses thought.
([AHRQ Digital Healthcare Research, CDS Chapter 1](https://digital.ahrq.gov/ahrq-funded-projects/current-priorities/clinical-decision-support-cds/chapter-1-approaching-clinical-decision);
[AHIMA, "Five Rights of CDS"](https://journal.ahima.org/Portals/0/archives/AHIMA%20files/The%20Five%20Rights%20of%20Clinical%20Decision%20Support_%20CDS%20Tools%20Helpful%20for%20Meeting%20Meaningful%20Use.pdf))

## 2.4 Professional-society guidance: AMA augmented intelligence

The AMA's 2024 principles for **augmented intelligence** (the AMA deliberately
says "augmented," not "artificial") are explicit that AI must **support, not
replace, clinical judgment**: *"Clinical judgment and human review of individual
circumstances should not be replaced by AI systems,"* and organizations *"must
communicate to clinicians and patients how AI-enabled systems... directly impact
medical decision-making and treatment recommendations at the point of care."*
Oversight should be **risk-based** (intended use, evidence of safety/efficacy/
equity, level of automation, transparency, deployment conditions). AMA also
publishes an 8-step governance toolkit for deploying organizations.
([AMA principles PDF](https://www.ama-assn.org/system/files/ama-ai-principles.pdf);
[AMA press release](https://www.ama-assn.org/press-center/ama-press-releases/ama-issues-new-principles-ai-development-deployment-use);
[Healthcare IT News, risk-based framework](https://www.healthcareitnews.com/news/ama-recommends-risk-based-approach-its-new-ai-governance-framework))

## 2.5 Synthesis — how to present a treatment suggestion responsibly

Combining the above into concrete presentation rules:

1. **Non-directive framing.** Label it a *"suggestion for physician review,"* not
   an order. Language like "Consider…" / "Options to weigh…" beats "Recommended
   treatment: X." Never phrase as a command.
2. **Options + rationale + cited evidence, not a lone directive.** Present the
   differential of reasonable options with the patient-specific facts and
   *guideline/source citations* behind each. This aligns with both Criterion 4
   and the automation-bias "information not commands" mitigator. (Even under the
   looser 2026 single-recommendation allowance, options-with-rationale is the
   safer default.)
3. **Never auto-actioned.** No pre-filled orders, no one-click "accept and
   order." The physician must take an independent, deliberate action downstream.
4. **Show uncertainty / per-item confidence, and gaps.** Surface what the model is
   unsure about and what data is *missing* from the chart — uncertainty display
   improves calibrated reliance and combats over-trust.
5. **Transparent reasoning + provenance.** Show the specific records the
   suggestion draws on and, where possible, the guideline it maps to. Be honest
   about the limit: citations show *inputs*, not the model's *reasoning chain*.
6. **De-emphasize, don't dominate.** The human-factors evidence that "prominence
   increases automation bias" argues *against* a bold, authoritative panel at the
   very top of the page. Make it visually secondary to the physician's own view;
   require a deliberate expand/click to see the suggestion.
7. **Easy dismissal + captured disposition.** One-gesture dismiss, and log the
   physician's agree/disagree/modify + (optionally) why — supporting both the
   "meaningful review" liability posture and quality monitoring.
8. **Avoid anchoring.** Consider showing the physician's own assessment or the raw
   data *before* revealing the suggestion, so the AI does not anchor their
   reasoning.
9. **Explicit "for physician review" + accountability reminder.** A persistent,
   plain-language statement that this is decision *support*, the physician is
   responsible, and it is not a substitute for clinical judgment.
10. **Non-interruptive delivery.** On-demand / inline, not modal pop-ups, to avoid
    contributing to alert fatigue.

---

# Part 3 — What This Means for AgentForge (Concrete)

*(Design facts below are drawn from the current codebase; file paths are absolute.)*

## 3.1 How the existing design already maps onto the CDS criteria

AgentForge's spine is **"deterministic core, AI at the edges"**: the LLM only
*proposes*, and deterministic code decides what a clinician ever sees. This maps
unusually well onto the carve-out and the UX literature:

| Requirement | AgentForge mechanism today | File |
|---|---|---|
| **Criterion 4 — reviewable basis / inputs surfaced** | Every clinical assertion is a `Claim` that *cannot exist* without a structured `FhirReference` = `(resource_type, resource_id, field, value, last_updated)`. The LLM only names *which resource* is relevant; deterministic code reads the exact `(field, value)` back out of the fetched resource — the physician sees the verbatim source value. | `agent/copilot/domain/primitives.py` (`FhirReference`), `agent/copilot/domain/contracts.py` (`Claim`), `agent/copilot/agent/grounding.py` |
| **"Not rely primarily" / fail-closed** | A **deterministic, non-promptable verification gate** re-fetches every cited resource **live by ID** and checks (a) attribution — cited resource is present; (b) value match — extracted value equals claimed value verbatim; (c) every numeric literal in the claim text appears as a standalone token in the source. Unsupported claims are **dropped**; if nothing is provable the answer is **withheld**; mixed results **degrade**. No model call is in the gate. | `agent/copilot/verification/core.py`, `agent/copilot/verification/serve.py` |
| **Read-only (no autonomous action)** | FHIR client is **GET-only** (`read`/`search`/`count_since`); scopes are `system/<Type>.read` only; SMART App Launch delegates the physician's own access so OpenEMR enforces which patients are visible. Nothing is written back to OpenEMR. | `agent/copilot/fhir/client.py`, `agent/copilot/fhir/auth.py`, `agent/copilot/fhir/provider.py` |
| **HIPAA audit** | Append-only PHI-read audit trail on both chat and rounds paths (cites HIPAA §164.312(b)). | `agent/copilot/chat/service.py`, `agent/copilot/rounds/service.py` |
| **UX: provenance / "information not commands"** | Each claim renders a green "✓ {ResourceType}" provenance chip; clicking shows "Recorded value" with *"Quoted verbatim from the source record."* Q&A framing: *"Cited from the record, or withheld — never guessed."* Verification outcome is rendered as a visible badge (Verified / Degraded / Withheld). | `agent/copilot/web/... ProvenanceChip.tsx`, `ClaimList.tsx`, `ChatPanel.tsx`, `PatientHero.tsx` |

In carve-out terms: AgentForge already delivers the **information-provenance**
half of Criterion 4 better than most products, is architecturally **read-only**
(no autonomous action, keeping it in the low-risk row of §1.7), and its
**fail-closed withhold-don't-guess** behavior is a strong automation-bias
counter-design.

## 3.2 The specific gaps to close before shipping a treatment-recommendation feature

1. **The gate verifies *facts*, not *clinical appropriateness*.** The verifier
   confirms "the INR value stated matches the record." It has **no mechanism to
   validate that a recommended *action* is clinically sound.** The domain rules
   (`agent/copilot/verification/rules.py`) are a small curated,
   demo-quality guardrail (allergy/med conflict, critical lab, reference range,
   med reconciliation) and are *additive/advisory* — they never gate. A treatment
   recommendation would sail through the current gate as long as its *cited facts*
   are accurate, even if the *recommended treatment* is wrong. **This is the
   single biggest safety and Criterion-4 gap.** Citations ground the premises;
   nothing grounds the conclusion.
2. **Reasoning-provenance is missing.** Criterion 4 wants the physician to review
   the *basis* — the link from facts to recommendation. AgentForge exposes cited
   inputs but not the model's reasoning chain, and an LLM cannot expose it
   faithfully. Consider anchoring recommendations to an **external, inspectable
   source** (a named clinical guideline / rule) so the "basis" is a citable
   artifact, not model introspection.
3. **No medical-advice / decision-support framing in the UI.** A grep found *no*
   "not medical advice," "clinical decision support," "for physician review," or
   accountability-disclaimer strings anywhere in the web app — framing is entirely
   "grounded / cited / withheld." A treatment feature needs explicit
   decision-support framing (§2.5 items 1, 9).
4. **"Top of the patient page, prominent" conflicts with the automation-bias
   evidence.** The proposed placement is the *most* bias-inducing location
   (§2.1). Reconsider prominence and default-collapsed presentation.
5. **Single authoritative directive → automation-bias magnet.** Prefer
   options-with-rationale over a lone "recommended treatment," notwithstanding the
   2026 single-recommendation allowance.
6. **No captured physician disposition.** No agree/disagree/why capture today —
   needed for the "meaningful review" liability posture (§1.5) and quality
   monitoring.
7. **No confidence / uncertainty display, no "missing data" surfacing** at the
   recommendation level.
8. **HTI-1-style transparency not yet modeled.** No structured surfacing of
   model/validation "source attributes" (§1.6). Even if not strictly certified,
   this is the transparency yardstick to design toward.
9. **Third-party LLM data-handling.** PHI is sent to Anthropic's API for
   synthesis/chat; confirm BAA and data-handling posture before a
   treatment-recommendation feature raises the stakes.

## 3.3 Recommended conservative framing of the panel

**Do not ship an autonomous "recommended course of treatment."** Instead ship a
**"Considerations & Cited Evidence"** panel that stays firmly in the low-risk
"information to a physician who decides" category and leans on AgentForge's
existing strengths:

- **Reframe from directive to evidence.** Title it *"Considerations for review"*
  or *"Guideline-linked considerations,"* not "Recommended treatment." Present
  **reasonable options with patient-specific rationale and citations**, not a
  single directive.
- **Anchor every consideration to a citable, inspectable basis** — the relevant
  FHIR records (already supported) **plus** a named clinical guideline reference
  where one exists — so Criterion 4's "basis" is an external artifact the
  physician can independently verify, not the model's opaque reasoning.
- **Route it through the existing fail-closed gate, and extend the gate's
  *scope*.** Keep "withhold rather than guess." Add an appropriateness layer
  (expand the domain rules from demo-quality to a maintained, validated rule set,
  or gate suggestions to guideline-matched scenarios) so the *conclusion* is
  grounded, not just the premises.
- **Present non-directively, non-prominently, non-interruptively.** Default
  collapsed; require a deliberate expand; visually secondary to the physician's
  own view; on-demand, never a modal.
- **Add explicit decision-support framing and an accountability reminder**
  ("Decision support for a licensed physician; not a substitute for clinical
  judgment; you are responsible for the decision"), plus per-item confidence and
  a "what's missing from the chart" note.
- **Capture disposition.** Log the suggestion, the physician's agree/disagree/
  modify, and optionally why — supporting the liability posture and quality
  monitoring. (This can write to AgentForge's own audited store, consistent with
  its read-only-to-OpenEMR design.)
- **Design toward HTI-1's transparency yardstick** even before any certification
  question is settled.
- **Get a formal FDA device-status determination and liability review** against
  the **January 2026** guidance before launch. The 2026 loosening helps, but a
  treatment-recommendation feature is close enough to the device boundary that a
  written determination (and, if pursued as a device, a Predetermined Change
  Control Plan for the AI model) is warranted.

---

## References

**FDA CDS device framework**
- Federal Register — *Clinical Decision Support Software* final guidance notice (Sept 28, 2022): https://www.federalregister.gov/documents/2022/09/28/2022-20993/clinical-decision-support-software-guidance-for-industry-and-food-and-drug-administration-staff
- FDA guidance landing page — *Clinical Decision Support Software*: https://www.fda.gov/regulatory-information/search-fda-guidance-documents/clinical-decision-support-software
- FDA — *Changes to Existing Medical Software Policies Resulting from Section 3060 of the 21st Century Cures Act*: https://www.fda.gov/regulatory-information/search-fda-guidance-documents/changes-existing-medical-software-policies-resulting-section-3060-21st-century-cures-act
- 21st Century Cures Act (overview): https://en.wikipedia.org/wiki/21st_Century_Cures_Act
- FDLI — *Cures Act Provides (Some) Clarity on FDA's Regulation of Software*: https://www.fdli.org/2017/04/21st-century-cures-act-provides-clarity-fdas-regulation-software/
- Goodwin — *FDA Issues Final CDS Software Guidance* (four criteria verbatim): https://www.goodwinlaw.com/en/insights/blogs/2022/10/fda-issues-final-clinical-decision-support-software-guidance
- Covington — *5 Key Takeaways from FDA's Final CDS Guidance*: https://www.cov.com/en/news-and-insights/insights/2022/10/5-key-takeaways-from-fdas-final-guidance-on-regulation-of-clinical-decision-support-software-fda-outlines-significant-changes-for-cds

**FDA January 2026 CDS guidance revision (operative)**
- Cooley — *Automation Bias and Clinical Practice: FDA Makes Incremental Updates to CDS Software Guidance* (Jan 2026): https://www.cooley.com/news/insight/2026/2026-01-20-automation-bias-and-clinical-practice-fda-makes-incremental-updates-to-clinical-decision-support-software-guidance
- Faegre Drinker — *Key Updates in FDA's 2026 General Wellness and CDS Software Guidance*: https://www.faegredrinker.com/en/insights/publications/2026/1/key-updates-in-fdas-2026-general-wellness-and-clinical-decision-support-software-guidance
- Arnold & Porter — *FDA "Cuts Red Tape" on CDS Software and Wearables* (Jan 2026): https://www.arnoldporter.com/en/perspectives/advisories/2026/01/fda-cuts-red-tape-on-clinical-decision-support-software
- Latham & Watkins — *FDA Issues Updated Guidance Loosening Regulatory Approach to Certain Digital Health Tools*: https://www.lw.com/en/insights/fda-issues-updated-guidance-loosening-regulatory-approach-to-certain-digital-health-tools

**LLMs / generative AI and the CDS criteria**
- npj Digital Medicine (2025) — *Unregulated large language models produce medical device-like output*: https://www.nature.com/articles/s41746-025-01544-y
- Bipartisan Policy Center — *FDA Oversight: Understanding the Regulation of Health AI Tools*: https://bipartisanpolicy.org/issue-brief/fda-oversight-understanding-the-regulation-of-health-ai-tools/

**Liability / learned intermediary / standard of care**
- Weiss — *When the Machine Speaks: AI, Physician Liability, and the Future of the Learned Intermediary*: https://blog.weisspc.com/ai-physician-liability-learned-intermediary/
- Winston & Strawn — *A New Intermediary: AI and the Learned Intermediary Doctrine*: https://www.winstontaylor.com/insights/a-new-intermediary-artificial-intelligence-and-the-learned-intermediary-doctrine
- AMA Journal of Ethics — *Are Current Tort Liability Doctrines Adequate for Addressing Injury Caused by AI?*: https://journalofethics.ama-assn.org/article/are-current-tort-liability-doctrines-adequate-addressing-injury-caused-ai/2019-02
- Medical Economics — *The new malpractice frontier: Who's liable when AI gets it wrong?*: https://www.medicaleconomics.com/view/the-new-malpractice-frontier-who-s-liable-when-ai-gets-it-wrong-

**HIPAA / ONC HTI-1 / info-blocking / state law**
- AHIMA — *ONC Decision Support Interventions Certification Criteria*: https://www.ahima.org/education-events/artificial-intelligence/artificial-intelligence-regulatory-resource-guide/onc-decision-support-interventions-certification-criteria/
- Mintz — *ONC HTI-1 Final Rule Introduces New Transparency Requirements for AI in Certified Health IT*: https://www.mintz.com/insights-center/viewpoints/2146/2024-01-08-hhs-onc-hti-1-final-rule-introduces-new-transparency
- Akin — *ONC Steps into AI Regulation: Predictive DSI + Information Blocking*: https://www.akingump.com/en/insights/alerts/onc-steps-into-ai-regulation-finalizing-extensive-requirements-for-predictive-decision-support-interventions-and-makes-significant-updates-to-information-blocking-regulations
- ONC — *Decision Support Interventions (DSI) Fact Sheet*: https://www.healthit.gov/sites/default/files/page/2023-12/HTI-1_DSI_fact%20sheet_508.pdf
- AMA — *AI State Advocacy and Policy Priorities Issue Brief* (2024): https://www.medchi.org/Portals/18/AMA%20ARC%20AI%20Policy%20Issue%20Brief_2024-12.pdf

**UX / human-factors / safety**
- Automation bias systematic review — *frequency, effect mediators, and mitigators* (PMC3240751): https://pmc.ncbi.nlm.nih.gov/articles/PMC3240751/
- *Automation complacency: risks of abdicating medical decision making* (AI & Ethics, 2025): https://link.springer.com/article/10.1007/s43681-025-00825-2
- *CDS Stewardship: Best Practices to Monitor and Improve Interruptive Alerts* (PMC9132737): https://pmc.ncbi.nlm.nih.gov/articles/PMC9132737/
- *Effects of workload, complexity, and repeated alerts on alert fatigue* (PMC5387195): https://pmc.ncbi.nlm.nih.gov/articles/PMC5387195/
- AHRQ Digital Healthcare Research — *Approaching CDS in Medication Management* (Five Rights): https://digital.ahrq.gov/ahrq-funded-projects/current-priorities/clinical-decision-support-cds/chapter-1-approaching-clinical-decision
- AHIMA — *The Five Rights of Clinical Decision Support*: https://journal.ahima.org/Portals/0/archives/AHIMA%20files/The%20Five%20Rights%20of%20Clinical%20Decision%20Support_%20CDS%20Tools%20Helpful%20for%20Meeting%20Meaningful%20Use.pdf

**Professional-society guidance**
- AMA — *Principles for Augmented Intelligence Development, Deployment, and Use* (PDF): https://www.ama-assn.org/system/files/ama-ai-principles.pdf
- AMA — *AMA Issues New Principles for AI Development, Deployment & Use* (press release): https://www.ama-assn.org/press-center/ama-press-releases/ama-issues-new-principles-ai-development-deployment-use
- Healthcare IT News — *AMA recommends a risk-based approach in its new AI governance framework*: https://www.healthcareitnews.com/news/ama-recommends-risk-based-approach-its-new-ai-governance-framework

---

*Prepared as internal engineering research for the AgentForge Clinical Co-Pilot
project. Not legal advice. Obtain qualified FDA regulatory and health-law counsel
before shipping any treatment-recommendation feature.*
