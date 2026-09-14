# Evidence

Committed runs, what each demonstrates, and the command that produced it. Every run directory
has a `manifest.json` listing its files with a sha256 and a `sensitive` flag.

| run | demonstrates | command |
|---|---|---|
| `disc_20260914T051827Z_51db/` | The real discovery run (brief section 4: "the discovery run has to be real"). `claude-sonnet-4-6` drove the live CoreLedger mock from the goal to a recorded draft in 7 turns and 26 s: it signed in with credential templates it never saw the values of, looked up the member, declared the balance as an output, and called done on the cell that shows it. The recorder turned the run into `artifacts/coreledger.member.read_savings_balance@1.0.0.capability.json`, proposing `member_number` as an input, which was accepted with `--yes`. | `uv run cua discover --goal "Log in, look up member 10007 and read their current savings balance." --target http://127.0.0.1:5050/ --capability-id coreledger.member.read_savings_balance --name "Read member savings balance" --app-version 4.2.1 --evidence-root evidence --yes` |

## Reading a discovery run

- `run.jsonl`: one JSON object per event. `model_turn` carries the model's one-sentence rationale,
  the tool it called, and its arguments; `tool_result` carries what the loop told the model, the
  locator ladder recorded for the target, and whether the target is weak; `decision` is the policy
  gate's answer for each action; `observation` names the screenshot and the frame URLs.
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
model's rationale. All data in the mock app is fake.

An earlier real run in the same session was deleted rather than committed. It exposed two defects:
the rationale was empty because a forced tool choice suppresses the model's text, and the
declared balance reached the log. Both are fixed and covered by tests, and the run above is the
rerun.
