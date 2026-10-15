# Design Council

A personal experiment: use a council to advise on system and API design decisions.

Modelled on Karpathy's [llm-council](https://github.com/karpathy/llm-council) (independent answers, anonymised peer review, chairman synthesis) with one change of shape. Proposals come from anywhere as text files. A judge scores each one against an explicit rubric with typed questions. **Code is the chairman**: weights, vetoes and a confidence gate decide between *recommend* and *escalate to a human*, and the report says why. It advises. Nothing acts on its output.

The default judge is [TypeSafe's System One API](https://docs.typesafe.ai/api): `Score` questions for most rubric dimensions, one `Noul` per hard constraint in the brief for constraint fit, and `Noul` questions for vetoes, with probabilities and confidence. Standard library only, Python 3.11+.

## Use

```sh
# see exactly what would be sent, without sending anything
python3 council.py show-request --brief examples/coordinator-host/brief.md --proposals examples/coordinator-host/*.md

# judge with TypeSafe (needs TYPESAFE_API_KEY in the environment)
python3 council.py judge --brief BRIEF.md --proposals a.md b.md c.md --out run.report.json

# pin a model version instead of the moving jev-latest alias
python3 council.py judge --typesafe-model jev-1.13.0 --brief BRIEF.md --proposals a.md b.md c.md

# offline judge: a local Ollama model, nothing leaves the machine. It reports no confidence,
# so the council escalates unless the run is marked advisory.
python3 council.py judge --judge ollama --allow-missing-confidence --brief BRIEF.md --proposals a.md b.md c.md

# change weights or thresholds later without asking the judge again
python3 council.py rescore --report run.report.json --rubric my_rubric.json

# mark hard constraints 2 and 6 (numbered as printed) as mandatory adoption checks for the shadow gate
python3 council.py rescore --report run.report.json --rubric rubrics/system_design.json --mandatory 2 6

# keep a decision journal, record what you would pick, and see how each gate does
python3 council.py judge --brief BRIEF.md --proposals a.md b.md --out run.report.json --journal ~/council/decisions.jsonl
python3 council.py label --journal ~/council/decisions.jsonl --brief BRIEF.md --pick b.md
python3 council.py validate --journal ~/council/decisions.jsonl

# try rescore with no key, on a saved live report of the worked example
python3 council.py rescore --report examples/coordinator-host/live-run.jev-1.13.0.json --rubric rubrics/system_design.json
```

A glob over a folder may include the brief itself. It is left out of the proposals, with a note on stderr.

Exit code 0 means *recommend*, 1 means *escalate* (for `judge` and `rescore` alike), and 2 means the council could not run: an input it cannot read (a missing file, bad JSON, a rubric it rejects, a rescore whose questions changed), a judge that cannot start, a judge that answered none of the proposals, or a refused API key.

With `--out`, `judge` rewrites the report after every proposal (through a temp file, so a crash never leaves half a file). A run that stopped part-way leaves a report marked `"complete": false`, which `rescore` refuses; judge again.

## Writing a brief

State the decision, the context, and the hard constraints as a bullet list under a line that says `Hard constraints:` (a Markdown heading works too). Write **one requirement per bullet**, phrased as a requirement rather than a fact. The council asks one question per bullet, so a bullet that bundles three unknowns can only get one blended answer, and a bullet that states a fact ("so far it has only been tested on macOS") gives the judge nothing to check against. In the worked example, splitting one bullet into separate WSL, CUDA and network bullets is what exposed that one proposal assumes the LAN works and another never mentions CUDA. The list is read the way Markdown reads one: a wrapped line belongs to its bullet even if it is not indented, nested bullets are part of the bullet above, and the list ends at a blank line followed by ordinary text, a heading or a horizontal rule. Headings such as `## Hard constraints`, `**Hard constraints:**` or `Hard constraints (non-negotiable):` all work, and every such section is read. `judge` and `show-request` print how many constraints they read, so a list that was cut short is visible; `show-request` shows each one. A brief with no such list cannot be judged with the default rubric; the council says so and exits with code 2.

## How a decision is made

1. Proposals are shuffled and relabelled `Proposal A, B, ...`. The judge sees only the brief and the proposal text. The label-to-file map stays in the report.
2. Each proposal is judged **independently** with the same questions over the same brief, one request per proposal, so scores are comparable. Because no proposal is judged next to another, presentation order cannot bias the judge.
3. Each `Score` is normalised to 0..1. A `per_constraint` dimension such as `constraint_fit` asks one `Noul` per hard constraint (does the proposal say what in its chosen design or plan meets it?) and scores the mean probability, the expected share of constraints it deals with. The dimensions are combined with the rubric weights. A `Noul` veto at or above its threshold removes a proposal regardless of its score.
4. The leader is recommended only if it beats the runner-up by `min_margin`, scores at least the floor `min_total` (a sole survivor too), the judge reported a confidence on every dimension and the lowest is at least `min_confidence`, and every proposal was judged. Otherwise the council escalates and lists the reasons. For a per-constraint answer, confidence is `|2p − 1|`, the formula TypeSafe's confidence docs give for a Noul: 0.5 means `p` outside 0.25 to 0.75. An escalation lists every constraint the judge was unsure about, least certain first. A judge that reports no confidence (the Ollama judge) escalates unless the run passes `--allow-missing-confidence`, which marks it advisory.

Every answer is checked before it counts: a Score outside its levels, a missing or malformed probability distribution, a bool where a number belongs, or a NaN or infinite value makes that proposal an error rather than being clamped or skipped. A NaN veto used to let its proposal through, because NaN >= 0.7 is false.

Transient failures are retried the way TypeSafe's own Python SDK does: HTTP 408, 429 and 5xx, connection errors and timeouts, at most 3 attempts, backoff from 0.5 s to 5 s with jitter, and a numeric `Retry-After` honoured. One 60 s deadline covers every attempt, wait and read for a proposal. A refused key (401 or 403) stops the run instead of sending every remaining proposal, and an error's body (a 422 names the offending field) is kept in the message with the key scrubbed.

## The shadow gate

The confidence gate above answers one question with the leader's single least certain answer. The October 2026 research (`design-council-research/RESEARCH.md` §3.1, outside this repo) found that it mixes two questions. On the saved live run it escalates on an answer that can move the leader's total by 0.019 against a 0.168 lead. The proposed replacement asks three questions:

- **Ranking: is the leader really ahead?** The margin, and the exact probability that the order flips under the judge's own distributions (Score levels and constraint answers as independent outcomes, vetoes held fixed, a tie counted as a flip). It also prints a stress case in which each proposal's answers move together, and the expected loss of committing to the leader now. A judge without distributions gets "unavailable", never a made-up number.
- **Eligibility: could a veto change the pick?** It flags a leader whose veto answer is within `veto_band` of its threshold, and a vetoed proposal that would win if answers within the band went the other way.
- **Acceptability: is the leader fit to adopt?** It checks the floor `min_total` and every constraint you mark with `--mandatory`. A mandatory constraint needs a confident yes, `p >= (1 + min_confidence) / 2`.

It is **reported, not operative**. Every report prints what the shadow gate would do under the verdict, and the journal records both. Switch only when `validate` shows, on decisions labelled after the procedure was frozen, that it escalates less without more wrong recommendations. `max_flip` (0.10) and `veto_band` (0.06) are placeholders: the band is the largest repeat-run movement seen, not a bound. On the live run the shadow gate would recommend Proposal A at P(flip) 0.070, or 0.266 in the stress case; with `--mandatory 6` it escalates on the checkpoint-storage constraint, which is the adoption question the old gate was pointing at.

## Decision journal and validation

`--journal FILE` (with `--out`) appends one line per run: the report's hash, the brief's hash, the rubric and tool hashes, the models that answered, and both verdicts. `label` appends the proposal you would pick *given what was known at the time*, which keeps hindsight out of the record. `validate` takes the latest run and label of each brief and prints, for each gate, how many decisions it recommended, how many of those you would not have picked, and an exact binomial upper bound on its wrong-recommendation rate. Escalations count as uncovered, never as wrong. With no wrong ones, 11 recommended decisions support "at most 20% wrong" at 90% confidence, and 29 support "at most 10%" at 95%. That holds only for a procedure fixed in advance and run on decisions it was not tuned on, and `validate` warns when the runs it compares used different tools, rubrics or models. The journal holds hashes and file names, not proposal text. `*.jsonl` is git-ignored.

Reports carry provenance (schema 3): when they were made, the sha256 of `council.py`, the Python version, the judge's URL and requested model, and per proposal its path, the sha256 of its text and of the request sent, the time taken and the attempts.

How the council is run with other seats (Claude, Codex) is in [PROTOCOL.md](PROTOCOL.md).

The report header names the model version that answered and the input tokens used. `jev-latest` is an alias that moves when TypeSafe ships a release, so once `min_confidence` and the veto thresholds have been tuned against real decisions, pin that version with `--typesafe-model` and move on your own schedule.

`rubrics/system_design.json` scores constraint fit (per constraint), failure handling, verifiability, simplicity, reversibility and evidence, and vetoes proposals that depend on something the brief rules out or that leave the decision unanswered (a rule that says which option to pick once named facts are checked counts as an answer). Its weights and thresholds are starting points, not findings. `rescore` only accepts a rubric that asks exactly the same questions: reworded instructions or levels need a new judge run.

## What is and is not validated

Offline:

- 61 tests: request shape against the documented API contract, parsing the brief's constraints (heading styles, numbering, wrapped and nested bullets, several sections, where the list ends), per-constraint scoring and the named weakest constraint, a brief with no constraints refusing to start, retry on 408, 429 and 5xx (honouring a numeric `Retry-After`), no retry on 401, 403 or 422, the API key never appearing in errors, model pinning, a proposals glob that also matches the brief, vetoes, margin and confidence gates, judge failures, `rescore` making no judge call and refusing reworded questions, and `rescore` of the saved live run exiting like `judge`. Also: every answer and rubric value checked (NaN, infinity, bool, out-of-range scores, missing distributions, an Ollama reply that omits a veto), retries within the deadline (5xx, dropped connections, a body cut short, a `Retry-After` past the deadline), a refused key stopping the run, checkpoints, code fences in a brief, exit codes for bad input, `python -OO`, the shadow gate (the live run's flip probability matches an independent exact enumeration, 0.069923845), the journal and the binomial bounds, and a golden copy of the live run's printed report.

Live, against `jev-1.13.0` on 2026-09-23:

- **The worked example** (3 requests, about 8.5k input tokens in total, 0.2 to 0.7 s per request). `wsl2.md` is vetoed at 0.98 for depending on SSH, which the brief forbids. `measure-then-decide.md` leads `native-windows.md` by 0.17 to 0.18 in both runs. It still escalates, and now says why in terms a person can check: it is unclear whether the leader deals with "Checkpoint files must end up on the desktop's permanent storage" (p = 0.49 to 0.52), which is the same gap the blind graders found. The report is saved as `examples/coordinator-host/live-run.jev-1.13.0.json`.
- **Repeat runs are close but not identical.** Across identical requests, totals moved by up to 0.018, a Noul by up to 0.06 and a Score's confidence by up to 0.17. Keep `min_margin` and veto thresholds well clear of that.
- **Per-constraint wording.** Three wordings were asked side by side on the three proposals and both decoys, and graded blind: "names it or states how", "states how its design meets it" and "takes it into account". Only the second ranked both decoys below every real proposal. The first gave a proposal that recommends nothing credit for listing trade-offs, and the third gave the off-topic decoy 0.35 to 0.50 on constraints it never touches. The adopted version rewords the question to "say what in its chosen design or plan meets" the constraint and takes stricter criteria from the adjudicator: silence does not count, a bare assertion does not count, trade-off talk without a chosen design does not count, and a concrete step counts even if it does not name the constraint. Together (they were not tested separately) these took the no-recommendation decoy from 0.28 to 0.08. An example sentence in those criteria was dropped because it was lifted from one of the test proposals and raised that proposal's answer from 0.70 to 0.85.
- **One gray area remains.** `native-windows.md` never mentions remote access, and Jev still gives 0.63 to 0.64 on "Nothing may depend on SSH" because nothing in the design needs it; the graders gave 0.05. At that value (confidence 0.26) the gate would list it among the constraints it is unsure about rather than trust it.
- **Three other rubric questions were reworded after live A/B tests.** Each variant was asked next to the original in the same request.
  - `simplicity` mixed two things: adding parts the goal does not need, and naming the moving parts. On the measured-decision proposal Jev split between them with confidence 0.00. It now asks only what a proposal adds, and says that a step checking something the brief calls unknown counts as needed (confidence 0.43 to 0.46). Removing the second half without that sentence left confidence at 0.00.
  - The old veto "answers a different question" gave the measured-decision proposal 0.62 to 0.65, just under its 0.7 threshold, and gave a proposal that recommends nothing only 0.55. Its replacement, `leaves_decision_unanswered`, gives 0.21 and 0.92 to 0.95 on the two decoys in `examples/probes/`, which answer a different decision and make no recommendation.
- **Blind review.** For each real proposal, two graders who never saw Jev's answers placed it on every question, and an adjudicator compared them with Jev. The graders and adjudicator are Claude subagents, so they are a second opinion, not ground truth. Jev agreed with them on all six veto answers. Of the 18 score placements, 11 were defensible, 3 were rubric ambiguity and 4 were Jev over-crediting a level that requires several conditions at once, such as "addresses every constraint" or "cites or measures the facts". That finding is why `constraint_fit` is now asked one constraint at a time, which is what TypeSafe's docs recommend for "every X" judgments. Scored on the graders' levels, the ranking is the same: 0.83 against 0.60, with the SSH proposal vetoed.
- **Open design question.** Whether the gate should use the lowest confidence on any question or the chance that the ranking flips. The shadow gate now computes the second next to the first, and the journal collects the evidence to decide.
- The offline Ollama judge (`qwen2.5:7b`) was only run on the earlier rubric. It vetoed the SSH proposal correctly, and also vetoed it for "answering a different question", which was a false positive.

What that does not show:

- Taste. One author wrote the rubric and all the proposals, and the rewordings (including the example brief's constraint bullets) were tested on three proposals and two decoys, so they may fit this example better than the next one.
- Before trusting it, replay real past decisions whose outcome you know and compare. A council tends toward the consensus answer; whether that matches your judgement is an empirical question.

## Limits

- Text only. It can judge written designs, not diagrams or mockups.
- One judge is one opinion, however many times it is asked. Diversity has to come from the proposals and the rubric. A second judge seat is a natural next step.
- A proposal can reveal its author in its own text. Anonymisation covers file names only.
- A proposal can argue for itself ("this design meets every constraint"). TypeSafe lists adversarial or self-describing content among Jev 1.13's known weak spots, so the rubric scores what a proposal shows, not what it claims, and a persuasive proposal still deserves a human read.
- With the TypeSafe judge, the brief and every proposal are sent to a third-party API. Use `show-request` first, and do not put anything in a brief that you would not send.
