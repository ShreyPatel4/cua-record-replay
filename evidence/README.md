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
| `replay_20260916T051839Z_acbcbf/` | `hard_failure` `PERMISSION_DENIED`, exit 2, for restricted member 10013, with `step_06.png`, `a11y_06.json`, and `trace.zip`. The detector asks for a human, and `--escalation-timeout 0` attaches no operator channel, so the run ends as a hard failure that keeps the code and says a human was needed. This is what an unattended caller with nobody on call gets. The trace covers sign-in and is scrubbed: no credential, operator id, or session cookie value survives in any member. | `uv run cua replay coreledger.member.read_savings_balance -i member_number=10013 --escalation-timeout 0 --evidence-root evidence` |
| `replay_20260916T052503Z_5abb6a/` | `escalated`, exit 3, same detector with an operator channel attached (brief section 2.9). The run pauses, writes `intervention.json` with the screen, the reason, and the exact take-control command, and polls `session_state.json`. Nobody comes, so after 20 s the request expires, the session file ends `finished`, and the result carries the request path and an `expired` `HumanIntervention`. | `uv run cua replay coreledger.member.read_savings_balance -i member_number=10013 --escalation-timeout 20 --evidence-root evidence` |
| `replay_20260914T074312Z_992522/` | `success` under `layout_drift`: Find became Search one cell to the right, s06's text rung fails, the same-row anchor rung resolves it, and `warnings` carries a drift signal (recorded rung 0, resolved rung 1, identity changed). | `uv run cua mock inject layout_drift`, then the success command, then `uv run cua mock reset` |
| `disc_20260916T153305Z_4dd9/` | The second real discovery run, and the irreversible one (brief section 4). The same model drove CoreLedger from a goal to a recorded draft in 15 turns and 60 s: signed in, looked up the member, opened the sub-account form, filled three fields, called `request_confirmation`, clicked Create and accepted the app's own confirm dialog, then declared the new sub-account number as the output. The recorder proposed four inputs (member number, account type, nickname, initial deposit), all accepted, and saved `artifacts/coreledger.member.open_subaccount@1.0.0.capability.json` with `s11` recorded as irreversible and carrying the dialog's exact message. | `uv run cua discover --goal "Open a new Holiday Club sub-account for member 10007 with the nickname Vacation fund and an initial deposit of 25.00. Report the new sub-account number." --target http://127.0.0.1:5050/ --capability-id coreledger.member.open_subaccount --name "Open member sub-account" --policy coreledger-subaccount --app-version 4.2.1 --allow-irreversible --evidence-root evidence --yes` |
| `replay_20260916T153554Z_abf6ce/` | The irreversible capability replayed: `success`, exit 0, one sub-account created and its number returned. `--confirm-irreversible` is what allows `s11`; the policy `coreledger-subaccount` rates that click irreversible from its text and from the confirm dialog it answers. `result.json` masks the number, the caller gets it on stdout. | `uv run cua replay coreledger.member.open_subaccount -i member_number=10007 -i "account_type=Holiday Club" -i "nickname=Vacation fund" -i initial_deposit=25.00 --confirm-irreversible --evidence-root evidence` |
| `replay_20260916T153610Z_aff3e0/` | The same command without `--confirm-irreversible`: `hard_failure` `CONFIRMATION_REQUIRED`, exit 2, at `s11`. The gate names the rule that made the click irreversible and the message says which flag would allow it. Nothing is created; a test asserts the ledger is still empty afterwards. | the success command, minus `--confirm-irreversible` |
| `replay_20260916T153615Z_f00741/` | `business_outcome` `VALIDATION_ERROR`, exit 0: CoreLedger refuses a deposit under $5.00. The form comes back with the values still in it, nothing is created, and the result names the input (`initial_deposit`) with the app's own message. An answer, not a failure. | the success command with `-i initial_deposit=1.00` |
| `stability_20260914T074315Z_4fbced/` | `--repeat 5`: five successes, the same rung for every step in every run, equal output digests, `determinism: deterministic`, durations 2240 to 2347 ms. The five runs sit inside the directory next to `stability.json`. | `uv run cua replay coreledger.member.read_savings_balance -i member_number=10007 --repeat 5 --evidence-root evidence` |

The replay runs need CoreLedger running with the operator credentials in `.env` (`uv run cua mock
serve`); the injections are armed through the mock's control API by `cua mock inject`, never by
replay, which the policy keeps away from `/__control`.

## Driving the handoff yourself

The escalated run above is the half nobody answers. This is the other half, and it needs a person
at the keyboard. Two terminals, about a minute.

1. Terminal 1: `uv run cua mock serve`
2. Terminal 2: `uv run cua mock inject app_error --param on=detail --times 1`
3. Terminal 2: start the run headed, so you can see the browser it will hand you:

   ```sh
   uv run cua replay coreledger.member.read_savings_balance -i member_number=10007 \
     --headed --escalation-timeout 300 --evidence-root evidence/_scratch
   ```

4. The run signs in, looks up the member, hits the 500 page, and stops. It prints the pause to
   stderr with the run id and the exact command to take the session. Copy that command.
5. Terminal 3: `uv run cua ops list --evidence-root evidence/_scratch` shows the run as
   `paused_for_human`, then run the take-control command it printed. The browser window comes to
   the front and is yours; replay will not touch it.
6. In the browser, fix what the automation could not: reload the member page (the injection was
   armed once, so the reload succeeds). You are looking at the member profile again.
7. Terminal 3: `uv run cua ops hand-back <run_id> --evidence-root evidence/_scratch --note "reloaded the member page after the 500"`
8. Replay re-runs the outcome detectors, re-verifies `cp_member_profile`, and carries on. It does
   not click Find again: you may already have done the step by hand. The run finishes
   `recovered_then_success`, exit 0, with the balance and a `HumanIntervention` in `recoveries`
   naming you, your note, and how many of your actions were captured.

`human_actions.jsonl` in that run directory holds what you did, redacted. `session_state.json`
holds every transition: `stuck_detected`, `take_control`, `hand_back`, `checkpoint_reverified`,
`finish`. `uv run cua ops abort <run_id>` instead of step 7 ends the run as `escalated`, exit 3.

The same path is covered without a human by
`test_a_human_takes_the_live_session_fixes_the_app_and_hands_it_back`, which drives the session
file from inside the paused run's own poll.

## Reading a replay run

- `run.jsonl`: `run_started` (capability, version, approval status, masked inputs), then per step
  `step_started`, `target_resolved` (rung, strategy, matches per rung, drift), the policy
  `decision`, `act`, and `wait_passed`; `detector_matched`, `recovery_started`,
  `recovery_finished`, and `drift_signal` when they happen; `run_finished` with the status.
- `step_NN.png`: the screen after step NN, or where the run stopped if step NN never finished.
  When the step had already finished, the stopping screen is `step_NN_failure.png`. A failure
  adds `a11y_NN.json`, the accessibility snapshot there, with dollar amounts masked once a
  sensitive output may be on screen.
- On an escalated run the stopping screen is `intervention_NN.png` and `a11y_intervention_NN.json`
  rather than `step_NN_failure.png`: it is captured when the run pauses, while automation still
  holds the session. `run.jsonl` says `capture_skipped` where a step capture would have gone.
- `trace.zip`: only on `hard_failure` and `escalated`. Open it with `npx playwright show-trace`.
  It is scrubbed of every secret form and session cookie, but it keeps page DOM and screenshots,
  so the manifest always flags it sensitive.
- `result.json`: the `ReplayResult`, with sensitive outputs masked (the caller gets the real value
  on stdout).
- `intervention.json`: the open request when a run paused for a human, with the screen it paused
  on (`intervention_NN.png`, `a11y_intervention_NN.json`), why, and the take-control command.
  `interventions.jsonl` keeps every request from a run that paused more than once.
- `session_state.json`: who held the live browser and every transition between them, with the
  operator's own note. It is flagged sensitive: notes are free text, and redaction only masks the
  secrets it knows about.
- `human_actions.jsonl`: what a human did while they held the session, one line per click, per
  field they left, and per navigation. Values are redacted, and anything typed into a field that
  looks like a credential is masked and added to the redactor before the line is written, so it
  cannot survive in the trace either. The file exists only if a human actually did something.

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

The balance run above is the fourth real run of that goal. The first was deleted before it was committed: its
rationale was empty because a forced tool choice suppresses the model's text, and the declared
balance reached the log. The second was committed, then replaced after the phase 3 review so the
evidence matches the reviewed recorder (per-rung robustness notes, step ids and rungs in the log,
amounts masked in the model's own words); git history keeps it. The third was discarded
uncommitted to fix the wording of those notes ("below of").
