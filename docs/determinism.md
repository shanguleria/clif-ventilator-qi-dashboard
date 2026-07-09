# Reproducibility & Determinism — a plain-English methods note

*Applies to every site and every scorecard tile. This note explains, without code, why the QI
dashboard now produces the **same numbers every time it is run**, what could previously make a number
wobble by a hair, and how each cause was fixed. It is a companion to the per-tile methods docs
(`*_METHODS.md`) and the combiner methods doc (`scorecard_methods.md`).*

---

## The problem, in one sentence

Running the same analysis on the same data should always give the same answer — but in a few places it
didn't quite, because the code occasionally had to **break a tie that no one had told it how to break**,
and it fell back on the incidental order the records happened to be in.

## The everyday analogy

Picture two nurses charting on the same patient at the **exact same minute** — one records a ventilator
mode change, the other records a medication event. Ask "which happened first?" and the chart can't say;
they share a timestamp. A clinician shrugs. But the computer has to put them in *some* order to process
them, and it simply used whichever record it happened to read first. Shuffle the records — which happens
for entirely incidental, non-clinical reasons — and it would pick the other one, and a downstream count
could tick up or down by one.

None of this reflected anything about patient care. It was pure bookkeeping.

## Why it started to matter

Two changes stir the order of the records:

- **A shared ventilator timeline.** The tiles that depend on ventilator data were moved onto one shared,
  reconstructed timeline. Same records, but they come out in a different order than before — so a few of
  those "coin flips" landed differently.
- **Incremental refresh (planned).** A design where each refresh appends the newest data and re-sorts.
  That means a number could drift slightly on *every* refresh, with no real change behind it.

For a quality dashboard that is re-run regularly and pooled across hospitals, numbers that jitter for no
clinical reason quietly erode trust. So the ties were worth eliminating.

## Where the ties actually were — in clinical terms

There were about a dozen, falling into four groups:

1. **Which ICU "owns" a patient-day.** A patient can be in the MICU in the morning and the SICU by the
   afternoon, but the dashboard labels each patient-day with one unit. The old rule was "whichever unit
   the patient spent the most minutes in" — which is ambiguous when the time is split evenly.
   **Fix:** the day is attributed to **the unit the patient started the day in**. You are in exactly one
   place at the start of the day, so this can never tie — and it is the clinically apt owner, because
   spontaneous-awakening and spontaneous-breathing trials are morning, nursing-driven events. (This also
   makes the sedation and breathing-trial tiles consistent with how the proning tile already assigns
   units.)

2. **Whether a day was "stable enough" to qualify.** Breathing-trial eligibility depends on a stretch of
   stable oxygenation and low blood-pressure support. When two oxygen-saturation or vasopressor readings
   were charted at the identical instant, the old code arbitrarily used one of them, which could flip a
   borderline day in or out. **Fix:** at a same-instant tie, use the **more conservative** value (the
   lower SpO₂, the higher pressor dose) — so eligibility is never over-called and the result is fixed.

3. **Which record "holds" until the next one.** Timelines are reconstructed by treating each ventilator
   or medication record as being in effect until the next one is charted. When two are charted at the
   same second, "the next one" is ambiguous — and that determined whether the patient looked like they
   were on the ventilator, on sedation, or on a paralytic at that moment. **Fix, by record type:**
   - *Continuous paralytics* — if a "start" and a "stop" are charted at the same instant, the **stop
     wins** (the patient-day is not flagged as paralyzed from that instant onward).
   - *Ventilator and infusion records* where there is no clinically meaningful "winner" — the records
     are put in a **fixed order by their content** (device, then mode, then settings; or medication
     action, then dose), and that order decides which one holds. The specific order is arbitrary; the
     point is only that it is **the same every time**. This affects a rare (~0.1%) situation — two
     records at the exact same second *within one stitched encounter* (i.e. two hospitalizations merged
     into one and briefly overlapping in time), not routine charting.

4. **The sedation dose-resumption comparison** (the Kress "restart at half-dose" figure). We compare the
   last sedation dose *before* an interruption with the first dose *after* it; both could tie on
   timestamp. **Fix:** take the **most recent** qualifying segment before the interruption and the
   **earliest** qualifying one after it, breaking any remaining tie by end-time and then by dose.

## The tie-breaker rules, at a glance

| Where a tie can occur | The rule that resolves it |
|---|---|
| Which ICU "owns" a patient-day | The unit of the **earliest ICU interval that day** (start-of-day). Unique by construction — never ties. |
| An SpO₂ or vasopressor reading charted at the same instant | Use the **more conservative** value — the **lowest** SpO₂, the **highest** pressor dose. |
| Two stability readings for one block-hour (from stitched, briefly-overlapping stays) | The hour counts as stable / assessable **only if every reading agrees** (logical AND). |
| A continuous **paralytic** "start" and "stop" at the same instant | The **stop wins** — the day is not flagged paralyzed from that instant. |
| Two ventilator or infusion records at the same second (no clinical winner) | A **fixed content order** (device → mode → settings; or action → dose) decides which holds — arbitrary but identical every run. |
| Sedation dose **before** vs. **after** an interruption (Kress) | **Most recent** qualifying dose before; **earliest** qualifying dose after (ties broken by end-time, then dose). |

Every rule above is a *total order* — it always lands on one answer — so no result can depend on the
incidental order the records arrived in.

## What was done, and how we know it worked

No clinical definitions were changed. Every tie was simply **given a fixed, documented rule** so the
computer stops flipping coins. The work was done systematically — first cataloguing every place a tie
could occur (so it wouldn't be half-fixed), then pinning each one.

It was then **proven** by deliberately scrambling the data into many random orders and confirming the
results came out identical every time — including a test that shuffled the entire multi-million-row
ventilator timeline and reproduced a byte-for-byte identical dashboard.

## The bottom line

- The effect was **small and one-time**: a handful of patient-days out of tens of thousands settled onto
  their now-fixed values.
- **Site-wide totals did not move** — a few borderline days simply settled into a fixed category instead
  of drifting between refreshes.
- The old numbers were never "more correct" — they were one of several possible coin-flips. There is no
  longer a coin.
- Going forward, **the dashboard reports the same numbers every time it runs, at every site** — the
  foundation for trustworthy cross-site pooling and for an incremental-refresh workflow.
