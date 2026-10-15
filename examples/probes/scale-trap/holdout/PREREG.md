# Holdout runs, pre-registered on 2026-10-15 before any judge call

The case was written by Codex in a sealed folder (`task.md`). Codex never saw `examples/probes/scale-trap` or `rubric-headroom.json`. Its own expectation is `work/EXPECTED.md`: the trap ranks below both `sized.md` and `ceiling.md` and is not recommended.

**Runs.** Each is 5 samples, with proposals `trap.md`, `sized.md` and `ceiling.md`:
- **D:** the default rubric, `--judge-cmd claude`, then `--judge-cmd codex`.
- **H:** `rubric-headroom.json`, `--judge-cmd claude`, then `--judge-cmd codex`.

**Prediction.**
- **D fails** for at least one judge: the trap is not below both good proposals.
- **H passes** for both judges: the trap is below both and scores lowest on headroom.

**What counts as support.** If H passes and D fails, the headroom question generalises beyond the case it was written against. If D also passes, the default rubric already catches this case. If H fails, the question is not ready.
