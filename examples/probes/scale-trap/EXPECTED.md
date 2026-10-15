# Scale trap: a design that is right today and wrong by spring

**Pre-registered on 2026-10-15, before any judge saw these files.** Results go in `RESULTS.md`, and this file does not change after the first run.

## The trap

`full-copy.md` is the plausible version, in the sense of the Ponytail benchmark's `bad` references: correct on the happy path, and sound on every point the default rubric asks about.
- It names its failure cases.
- It has concrete tests.
- It labels a measurement.
- It adds no service.
- It is easy to undo.

What it never does is multiply its own numbers by the growth the brief states:

| | today (2 workers) | spring (200 workers) |
|---|---:|---:|
| checkpoint traffic to the desktop (2 GB per worker per 10 min) | 6.7 MB/s | 667 MB/s |
| desktop link, measured | 112 MB/s | 112 MB/s |
| share of the link | 6% | 595% |

From about 35 workers, copies queue faster than they drain. The backlog then grows without limit, and the "resume from at most 30 minutes old" constraint fails for every job. The proposal even states the 6% figure and its "48 hours with both workers" test, both of which are true today.

## The proposals

| file | role | at 200 workers |
|---|---|---|
| `full-copy.md` | trap | breaks the 30-minute constraint and does not say so |
| `buddy-replication.md` | good: sized for spring | holds; the desktop carries metadata only |
| `named-ceiling.md` | good, lazy: the same mechanism as the trap, with its ceiling (about 30 workers) and a switch-over trigger stated | holds if the trigger is honoured |
| `ceph-cluster.md` | over-built decoy | holds, with k3s, Ceph, a mesh and a monitoring stack for 2 workers today |

`full-copy.md` and `named-ceiling.md` share a mechanism. The only difference between them is that one states the future failure, so the gap between them is the cleanest measure of whether the judge sees it.

## Runs

| run | brief | rubric |
|---|---|---|
| A | `brief-implicit.md`: growth appears only in the context paragraph, as a design consult would hear it | default |
| B | `brief-explicit.md`: the same, plus a hard constraint that everything must hold at 200 workers (the control) | default |
| C | `brief-implicit.md` | `rubric-headroom.json`: the default plus one `headroom` question that asks the judge to work the stated growth out from the numbers |

Each run uses the cmd judge with 5 samples, once with `--judge-cmd claude` and once with `--judge-cmd codex`.

## Expectations

| id | run | expectation | why |
|---|---|---|---|
| E1 | A, B, C | The council does **not** recommend `full-copy.md`. | Recommending it is the failure this probe exists to catch. |
| E2 | A, B, C | `full-copy.md` totals below both `buddy-replication.md` and `named-ceiling.md`. | Both good proposals hold at 200 workers; the trap does not. |
| E3 | A, B, C | `ceph-cluster.md` is not recommended. | `simplicity` should catch the over-build. |
| E4 | B | On the 200-worker constraint, `full-copy.md` has p < 0.5. | The control: when growth is stated as a constraint, constraint fit should see it. |
| E5 | C | `full-copy.md` scores below `named-ceiling.md` on `headroom`. | The question rewards stating the ceiling. |

**Prediction, also pre-registered: run A fails E2 on both judges.**
- No question in the default rubric asks about stated growth.
- `constraint_fit` asks whether a proposal *says* what in its design meets each constraint, and `full-copy.md` does.
- `simplicity` favours it over `buddy-replication.md`.

If run A passes anyway, the judge foresaw the failure unprompted, which is better than expected. If run A fails while B and C pass, either fix works: a brief that states scale as a constraint, or a rubric that asks for headroom. Any change to the default rubric must then also be rerun on `examples/coordinator-host` before adoption.

## Commands

```sh
P="examples/probes/scale-trap/full-copy.md examples/probes/scale-trap/buddy-replication.md examples/probes/scale-trap/named-ceiling.md examples/probes/scale-trap/ceph-cluster.md"
python3 council.py judge --judge-cmd claude --brief examples/probes/scale-trap/brief-implicit.md --proposals $P --out A.claude.json                                                     # A
python3 council.py judge --judge-cmd claude --brief examples/probes/scale-trap/brief-explicit.md --proposals $P --out B.claude.json                                                     # B
python3 council.py judge --judge-cmd claude --brief examples/probes/scale-trap/brief-implicit.md --rubric examples/probes/scale-trap/rubric-headroom.json --proposals $P --out C.claude.json  # C
```
