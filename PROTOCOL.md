# Council protocol

How a design decision goes through the council when several seats take part (for example Claude, Codex, and this tool's judge). The steps come from the October 2026 research (`design-council-research/RESEARCH.md` §3.5). They are a practical reading of the evidence, not a theorem:

- Majority voting explains most of the gain credited to multi-agent debate (Choi, Zhu & Li, NeurIPS 2025).
- Extra discussion rounds hurt (Kaesberg et al., Findings of ACL 2025).
- Models switch from right answers to wrong ones in order to agree (Wynn et al., 2025).
- Social influence raises confidence without improving accuracy (Lorenz et al., PNAS 2011).

## Steps

1. **Write the brief first.** State the decision, the context, and one requirement per bullet under `Hard constraints:`.
   - **Scale.** If the design must survive growth, put the load it must hold at, with numbers, in a hard constraint, not only in the context. `examples/probes/scale-trap` shows why: with growth in the context only, both LLM judges ranked a design that breaks at the stated load second and credited it with meeting the constraint it would break. With the same growth as a constraint, both answered that the design does not hold at that load (p = 0.00), though only one of them then ranked it below both designs that do.
   - **Mandatory checks.** Decide now which constraints are mandatory adoption checks (`--mandatory N ...`), not after seeing the scores.
   - **What leaves the machine.** The brief and the proposals go to the judge's provider (Anthropic for `--judge-cmd claude`, OpenAI for `codex`, TypeSafe for `--judge typesafe`), so never put secrets or customer data in them. Run `show-request` first.
2. **Independent proposals.** Each seat writes its proposal without seeing the others. Each seat gets only the brief and copies of the evidence, in a folder of its own.
3. **Freeze each seat's position.** Before any seat reads another's proposal, record its pick and its reasons in one short file per seat (`positions/<seat>.md`). Nothing in it changes afterwards.
4. **One critique round, anonymised.** Each seat reads the other proposals as `Seat 1`, `Seat 2` and so on, never by model name, and critiques them once. The round's only output is a list of contested claims, each with the probe that would settle it: a test, a measurement, or a doc page to open.
5. **Probes settle disputes.** Run the probes. A seat changes position only by citing a probe result or a source. Log every change in `decisions.md` as either *resolved by evidence* or *converged without evidence*. Keep dissent that no evidence resolved visible, never averaged away.
6. **The judge stays blind.** Give `council.py judge` the proposals, never the critique transcripts or the positions. Use `--out` and `--journal`, so both gates' verdicts are recorded.
   - **Pick the judge's family.** Use a model family that wrote none of the proposals when there is one. When Claude and Codex both wrote proposals, judge with both (`--judge-cmd claude --judge-cmd codex`), so neither family judges only its own work. Record which family wrote which proposal.
7. **Read the verdict, then decide.** The operative verdict decides whether the council recommends or escalates. The shadow-gate line is information for later. You make the decision; a recommendation is advice.
8. **Label it.** Once you have decided, run `council.py label --journal <journal> --brief <brief> --pick <file>` with the proposal you would pick *given what was known then*, or `--pick none`. Record it when you decide, not after the outcome is known.

## What not to count

- **Agreement between Claude, Codex and the judge is not independent votes.** Models from different providers often make the same mistake (Kim et al., ICML 2025). Count agreement as corroboration only for claims a probe checked.
- **Don't let a seat's family judge its own proposal alone.** Judges favour their own outputs (Panickssery et al., NeurIPS 2024). Step 6 says what to do instead.
- **Don't use more rounds.** If one round leaves a dispute open, write a probe for it rather than holding a second round.

## Switching the gate

Run `council.py validate --journal <file>` once there are labelled decisions. Switch from the operative gate to the shadow gate only when both of these hold:

- The procedure (`council.py` hash, rubric and pinned model) was frozen before the decisions it is judged on.
- The shadow gate shows higher coverage without more wrong recommendations.

About 11 recommended decisions with none wrong support "at most 20% wrong" at 90% confidence. Escalations don't count toward that number, so the labelled set has to be larger.

## Check on the protocol

Over the next 10 or more councils, count the position changes after critique that a probe later confirmed. If seats mostly converge without evidence, the critique round is producing conformity rather than information. In that case, cut it back to listing contested claims only.
