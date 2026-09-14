# Evidence

Committed runs, what each demonstrates, and the command that produced it. Every run directory
has a `manifest.json` listing its files with a sha256 and a `sensitive` flag.

| run | demonstrates | command |
|---|---|---|
| `disc_20260914T055730Z_9596/` | The real discovery run (brief section 4: "the discovery run has to be real"). `claude-sonnet-4-6` drove the live CoreLedger mock from the goal to a recorded draft in 7 turns and 26 s: it signed in with credential templates it never saw the values of, looked up the member, declared the balance as an output, and called done on the cell that shows it. The recorder turned the run into `artifacts/coreledger.member.read_savings_balance@1.0.0.capability.json`, proposing `member_number` as an input, which was accepted with `--yes`. | `uv run cua discover --goal "Log in, look up member 10007 and read their current savings balance." --target http://127.0.0.1:5050/ --capability-id coreledger.member.read_savings_balance --name "Read member savings balance" --app-version 4.2.1 --evidence-root evidence --yes` |
| `replay_20260914T074249Z_1cfed6/` | Replay `success`, exit 0 (brief section 3.3). The approved `@1.1.0` capability signs in, looks up 10007, verifies `cp_member_profile`, and returns the balance to the caller. `result.json` keeps it masked; every file from the profile screen on is flagged sensitive. No model anywhere in the run. | `uv run cua replay coreledger.member.read_savings_balance -i member_number=10007 --evidence-root evidence` |
| `replay_20260914T074252Z_97774b/` | `business_outcome` `MEMBER_NOT_FOUND`, exit 0: a member who does not exist is an answer, returned with a field error on `member_number` and the app's own message. | `uv run cua replay coreledger.member.read_savings_balance -i member_number=99999 --evidence-root evidence` |
| `replay_20260914T074255Z_42ba0b/` | `recovered_then_success` through `od_system_notice`: the maintenance notice hides the profile, replay clicks OK once and the checkpoint then verifies. | `uv run cua mock inject interstitial`, then the success command, then `uv run cua mock reset` |
| `replay_20260914T074259Z_6a80cf/` | `recovered_then_success` through `od_slow_member_load`: the profile takes 5 s against s06's 3 s wait, the timeout detector fires once, and its `wait_retry` budget (15 s) sees the profile arrive. | `uv run cua mock inject slow`, then the success command, then `uv run cua mock reset` |
| `replay_20260914T074308Z_ed988e/` | `hard_failure` `PERMISSION_DENIED`, exit 2, for restricted member 10013, with `step_06.png`, `a11y_06.json`, and `trace.zip`. The detector asks for escalation; until the operator channel exists (phase 5) the escalation hook ends the run as a hard failure that keeps the code and says a human is needed. The trace covers sign-in and is scrubbed: no credential, operator id, or session cookie value survives in any member. | `uv run cua replay coreledger.member.read_savings_balance -i member_number=10013 --evidence-root evidence` |
| `replay_20260914T074312Z_992522/` | `success` under `layout_drift`: Find became Search one cell to the right, s06's text rung fails, the same-row anchor rung resolves it, and `warnings` carries a drift signal (recorded rung 0, resolved rung 1, identity changed). | `uv run cua mock inject layout_drift`, then the success command, then `uv run cua mock reset` |
| `stability_20260914T074315Z_4fbced/` | `--repeat 5`: five successes, the same rung for every step in every run, equal output digests, `determinism: deterministic`, durations 2240 to 2347 ms. The five runs sit inside the directory next to `stability.json`. | `uv run cua replay coreledger.member.read_savings_balance -i member_number=10007 --repeat 5 --evidence-root evidence` |

The replay runs need CoreLedger running with the operator credentials in `.env` (`uv run cua mock
serve`); the injections are armed through the mock's control API by `cua mock inject`, never by
replay, which the policy keeps away from `/__control`.

## Reading a replay run

- `run.jsonl`: `run_started` (capability, version, approval status, masked inputs), then per step
  `step_started`, `target_resolved` (rung, strategy, matches per rung, drift), the policy
  `decision`, `act`, and `wait_passed`; `detector_matched`, `recovery_started`,
  `recovery_finished`, and `drift_signal` when they happen; `run_finished` with the status.
- `step_NN.png`: the screen after step NN, or where the run stopped if step NN never finished.
  When the step had already finished, the stopping screen is `step_NN_failure.png`. A failure
  adds `a11y_NN.json`, the accessibility snapshot there, with dollar amounts masked once a
  sensitive output may be on screen.
- `trace.zip`: only on `hard_failure` and `escalated`. Open it with `npx playwright show-trace`.
  It is scrubbed of every secret form and session cookie, but it keeps page DOM and screenshots,
  so the manifest always flags it sensitive.
- `result.json`: the `ReplayResult`, with sensitive outputs masked (the caller gets the real value
  on stdout).

## Reading a discovery run

- `run.jsonl`: one JSON object per event. `model_turn` carries the model's one-sentence rationale,
  the tool it called, and its arguments; `tool_result` carries what the loop told the model, the
  step id and rung when the action became a step, the locator ladder, and whether the target is
  weak; `decision` is the policy gate's answer for each action; `observation` names the screenshot
  and the frame URLs; `checkpoints_derived` lists each step's wait and `success_checkpoint` shows
  that the derived success condition held on the final screen before the draft was saved.
- `step_NN.png`: the screen the model saw at turn NN.
- `artifact.capability.json`: a copy of the draft as saved to the catalog.
- `result.json`: the discovery result (status, stop reason, turns, tokens, parameter decisions).
- `a11y_NN.json`: only on a stopped run, the accessibility snapshot it stopped on.

## What is redacted, and what is not

Every string written to these files goes through the same redactor the model's context does:
credential and operator id values are `[REDACTED]`, member and account numbers keep their last
four digits, and a value declared as an output is masked from the moment it is declared, so the
balance never appears in the log. Screenshots are not image-redacted. The manifest flags the ones
taken on sign-in pages or after a sensitive output was declared; delete flagged files before
sharing a run outside the team. Names are not detected: the synthetic member's name appears in the
model's done summary. All data in the mock app is fake.

This is the fourth real run of this goal. The first was deleted before it was committed: its
rationale was empty because a forced tool choice suppresses the model's text, and the declared
balance reached the log. The second was committed, then replaced after the phase 3 review so the
evidence matches the reviewed recorder (per-rung robustness notes, step ids and rungs in the log,
amounts masked in the model's own words); git history keeps it. The third was discarded
uncommitted to fix the wording of those notes ("below of").
