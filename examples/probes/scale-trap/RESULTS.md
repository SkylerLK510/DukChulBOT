# Scale trap: results (2026-10-15)

**Setup.**
- **Judge.** The cmd judge, with 5 samples per proposal: Claude Sonnet 5.5 (`--judge-cmd claude`) and Codex gpt-6.1-sol (`--judge-cmd codex`).
- **Pre-registration.** Expectations are in [EXPECTED.md](EXPECTED.md) and [holdout/PREREG.md](holdout/PREREG.md), each written before its runs.
- **Raw reports** are in `design-council-research/judge-oct15/`, outside this repo.

## The checkpoint case (written with the headroom question; same author)

Totals are listed highest first, and the trap is in bold.

| run | judge | ranking | verdict | trap's answers |
|---|---|---|---|---|
| A: growth in the context only, default rubric | Claude | named-ceiling 0.985, **full-copy 0.967**, buddy 0.929, ceph 0.552 | escalate: lead 0.018 under `min_margin` | p = 1.00 on "resume from at most 30 minutes old" |
| A | Codex | named-ceiling 0.937, **full-copy 0.908**, buddy 0.881, ceph (vetoed) | escalate: lead 0.028 under `min_margin` | p = 0.60 on the same constraint |
| B: growth as a hard constraint, default rubric | Claude | named-ceiling 0.980, buddy 0.925, **full-copy 0.883**, ceph 0.550 | escalate (confidence on constraint 3) | p = 0.00 on "must hold at 200 workers" |
| B | Codex | named-ceiling 0.950, **full-copy 0.890**, buddy 0.883, ceph (vetoed) | recommend named-ceiling | p = 0.00 on "must hold at 200 workers" |
| C: growth in the context, headroom rubric | Claude | buddy 0.936, named-ceiling 0.909, **full-copy 0.812**, ceph 0.603 | escalate (margin, confidence) | headroom 0.00 in all 5 samples |
| C | Codex | named-ceiling 0.913, buddy 0.836, **full-copy 0.801**, ceph (vetoed) | recommend named-ceiling | headroom 0.00 in all 5 samples |

**Against the expectations:**

| | A Claude | A Codex | B Claude | B Codex | C Claude | C Codex |
|---|---|---|---|---|---|---|
| E1: trap not recommended | pass, by the margin rule only | pass, by the margin rule only | pass | pass | pass | pass |
| E2: trap below both good proposals | **fail** | **fail** | pass | **fail** (by 0.007) | pass | pass |
| E3: ceph not recommended | pass | pass | pass | pass | pass | pass |
| E4: trap p < 0.5 on the 200-worker constraint | | | pass | pass | | |
| E5: trap below named-ceiling on headroom | | | | | pass | pass |

**What the A runs show.** The prediction for run A held: with growth stated only in the context, both judges ranked the trap second and credited it with meeting the 30-minute constraint. Each run escalated only because the trap trailed by less than `min_margin`. The independent P(flip) was 0.39 for Claude and 0.38 for Codex.

**A flaw in this probe.**
- **What happened.** The two good proposals never restate the brief's "no paid services" constraint, and the default rubric counts silence as "not addressed". So the judges gave them p = 0.00 to 0.60 there, against 1.00 for the trap, which says it "adds no new service".
- **Its effect.** It costs `buddy-replication.md` about 0.04 of its total, which is why it trails the trap in run B with Codex (E2 fails by 0.007), and part of why it trails in run A.
- **Why the file stays.** It was pre-registered, so it is left unedited.
- **The clean test** is the holdout below, which an independent author wrote.

## The holdout: a notification dispatcher (written by Codex, blind)

Codex wrote [holdout/](holdout/) in a sealed folder, without seeing this case or `rubric-headroom.json`.
- **The trap:** a fixed pair of workers clears today's 3,000 requests in 15 s. The committed 800 tenants need 120 s against a 55 s constraint.

| run | judge | ranking | trap's answers |
|---|---|---|---|
| D: default rubric | Claude | sized 0.993, ceiling 0.980, **trap 0.917** | p = 0.00 on the 55 s constraint |
| D | Codex | ceiling 1.000, sized 1.000, **trap 0.917** | p = 0.00 on the 55 s constraint |
| H: headroom rubric | Claude | sized 1.000, ceiling 0.988, **trap 0.835** | headroom 0.00; p = 0.60 on the 55 s constraint |
| H | Codex | ceiling 1.000, sized 1.000, **trap 0.797** | headroom 0.00; p = 0.00 on the 55 s constraint |

All four runs escalate, because the two good proposals are within `min_margin` of each other.
- **The prediction that run D fails was wrong.** The default rubric caught this trap: both judges did the arithmetic for the 55 s constraint unprompted.
- **The prediction that run H passes held.** The headroom question widened the gap from 0.06–0.08 to 0.15–0.20.

## Conclusions

1. **The blind spot is real but depends on the case.**
   - **Caught:** when the constraint that breaks is itself a number the judge can check, such as a deadline or a throughput figure, the judges did the arithmetic.
   - **Missed:** when the breakage is indirect (bandwidth to backlog to stale resume points), neither judge connected the growth in the context to the constraint.
2. **Stating the load as a hard constraint fixed it with both judges** (run B: p = 0.00 on the scale constraint). This costs nothing. It is now step 1 of [PROTOCOL.md](../../../PROTOCOL.md).
3. **The headroom question worked in all 4 runs where it was asked, on two cases by two authors and with two judges.**
   - It gave the trap 0.00 every time.
   - It credited the stated ceiling (named-ceiling 0.67 and ceiling 1.00, against 0.00 for the trap).
   - On the worked example in `examples/coordinator-host`, whose brief states no growth, it left the ranking, the vetoes and the verdict unchanged. When every proposal gets the same headroom score it shrinks margins by 13% (its weight, 0.15, against a total weight of 1.15).
4. **Not shown.**
   - **Calibration.** Two cases are a smoke test, not a rate.
   - **Overfitting.** The headroom question was written by the author of the first case.
   - **Coverage.** No case yet has the breaking load stated only in a proposal, nor growth with no number.
