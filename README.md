# cua-record-replay

Record-once, replay-many computer-use automation for legacy back-office UIs. A model discovers how
to do a task once against a live app. The run becomes a typed, versioned capability artifact. After
that the work is done by a deterministic replay engine with no model in the loop, and a human can
take the live browser when neither of them should proceed alone.

The target is CoreLedger, a mock credit union core that ships in this repo: frames, table layout,
no ids or test hooks, spans with onclick handlers instead of buttons, a native confirm dialog on
the one irreversible action, and eight failure injections.

```
discover (model, once)  ->  artifact (JSON, reviewed, approved)  ->  replay (no model, many times)
                                                                          |
                                                          escalate -> a human takes the session
```

## Setup

```sh
uv sync
uv run playwright install chromium
cp .env.example .env   # set CORELEDGER_OPERATOR_PASSWORD (8+ chars)
```

`ANTHROPIC_API_KEY` is needed only for `cua discover`. Everything below the discovery section runs
without it.

## The path with no LLM

This is the whole demo, and none of it calls a model. Start the app in one terminal:

```sh
uv run cua mock serve                     # CoreLedger on http://127.0.0.1:5050/
```

Then, in another:

```sh
# What has been recorded and approved
uv run cua catalog list

# Read a member's savings balance. Exit 0, the balance on stdout.
uv run cua replay coreledger.member.read_savings_balance -i member_number=10007

# A member who does not exist is an answer, not a crash: business_outcome, exit 0.
uv run cua replay coreledger.member.read_savings_balance -i member_number=99999

# The same flow five times, with a determinism report.
uv run cua replay coreledger.member.read_savings_balance -i member_number=10007 --repeat 5
```

Break the app on purpose and watch replay classify it. Each injection is armed through the mock's
own control API, never by replay:

```sh
uv run cua mock inject interstitial        # a notice covers the profile; replay clicks OK
uv run cua mock inject slow                # the page takes 5 s; a retry budget rides it out
uv run cua mock inject session_expired     # mid-flow bounce to sign-in; replay signs in again
uv run cua mock inject layout_drift        # Find becomes Search, one cell over; a lower rung wins
uv run cua mock inject app_error --param on=detail   # 500 page; escalates to a human
uv run cua mock reset                      # clear everything
```

The irreversible capability needs a human's say-so on the command line:

```sh
# Refused, exit 2, and nothing is created.
uv run cua replay coreledger.member.open_subaccount \
  -i member_number=10007 -i "account_type=Holiday Club" \
  -i "nickname=Vacation fund" -i initial_deposit=25.00

# Allowed, exit 0, one sub-account created and its number returned.
uv run cua replay coreledger.member.open_subaccount \
  -i member_number=10007 -i "account_type=Holiday Club" \
  -i "nickname=Vacation fund" -i initial_deposit=25.00 --confirm-irreversible

# A deposit the app rejects is an answer with the field named, exit 0.
uv run cua replay coreledger.member.open_subaccount \
  -i member_number=10007 -i "account_type=Holiday Club" \
  -i "nickname=Vacation fund" -i initial_deposit=1.00 --confirm-irreversible
```

Exit codes: 0 success or business outcome, 2 hard failure, 3 escalated.

Every run writes `evidence/<run_id>/`: a redacted JSON-lines log, a screenshot per step, the
result, and a manifest with a sha256 and a sensitive flag per file. Failures add an accessibility
snapshot and a scrubbed Playwright trace.

## When a human has to step in

A detector can ask for a person. The run then pauses, writes `intervention.json` with the screen it
stopped on, and waits without touching the browser:

```sh
uv run cua ops list                        # runs waiting for a human
uv run cua ops show <run_id>               # the request, and the screenshot
uv run cua ops take-control <run_id>       # the browser is yours; replay is locked out
uv run cua ops hand-back <run_id> --note "what you did"
uv run cua ops abort <run_id>              # end it as escalated
```

On hand-back replay re-runs the outcome detectors and re-verifies the step's checkpoint. It never
repeats the step you were paused on, because you may have finished it by hand.
`evidence/README.md` has an eight-step walkthrough you can drive yourself.

## Discovery (the one part that uses a model)

```sh
uv run cua discover \
  --goal "Log in, look up member 10007 and read their current savings balance." \
  --target http://127.0.0.1:5050/ \
  --capability-id coreledger.member.read_savings_balance \
  --name "Read member savings balance" --app-version 4.2.1
```

The model sees an accessibility snapshot and a screenshot, acts through the same policy gate replay
uses, and never sees a credential value: it types `{{secrets.operator_password}}` and the gate
substitutes at the browser edge. A recorded run is saved as a **draft**. Replay refuses a draft
unless `--allow-draft` is passed, so a human reviews it, adds the outcome detectors one happy run
cannot observe, and approves it:

```sh
uv run cua catalog show coreledger.member.open_subaccount --version 2.0.0
```

The approval that shipped was `cua catalog approve coreledger.member.open_subaccount --version
2.0.0 --by "Shrey Patel"`. Running it again errors: an approved file is never re-approved, and a
draft is never edited in place. A new review is a new version.

`artifacts/` holds every version of both capabilities, and each one came from a real run.
`@1.0.0` is what discovery recorded. `@1.1.0` is the first review, which added the outcome
detectors. `@2.0.0` is the second, after a review found that the success checkpoint asserted an
amount the app reformats; renaming an outcome code along with it is a contract change for callers,
so the version rule made it a major bump. Every `review_notes` says what changed and why.

## Safety

- One policy file, `policy/allowlist.yaml`, gates every action in discovery and replay: origins,
  paths, action kinds, and what counts as irreversible. Irreversible handling is `block`,
  `confirm` or `escalate` per policy.
- Secrets live only in `.env`. They are never in an artifact, a log, an evidence file, or the
  model's context, in any encoding. `uv run pytest tests/test_leaks.py` checks every committed
  file, zip members included.
- Member and account numbers keep their last four digits. Values declared as outputs are masked
  from the moment they are declared.
- Screenshots are not image-redacted. The manifest flags the ones taken on a sign-in page, on a
  member record, or after a sensitive value could be on screen; delete flagged files before
  sharing a run.
- The mock app holds only synthetic data.

## Gates

```sh
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest                              # CUA_REQUIRE_SECRET_SCAN=1 to fail if .env is absent
uvx pre-commit install                     # ruff plus the leak scan on every commit
```

## License

Copyright 2026 Shrey Patel. Released under the GNU General Public License v3.0; see `LICENSE`.
Use it, change it, run it. If you distribute it or anything built from it, that has to come with
its source under the same license.

## Where to look

| Path | What is there |
|---|---|
| `REPORT.md` | The design decisions, the trade-offs, and what was cut |
| `artifacts/` | Capability artifacts, the product of discovery |
| `evidence/` | Committed runs, each with a README row saying what it demonstrates |
| `src/cua/artifact/` | The schema: steps, checkpoints, outcome detectors, versioning |
| `src/cua/replay/` | The engine. Imports no browser and no model |
| `src/cua/policy/` | The gate and the redactor |
| `src/cua/session/` | Control transfer, intervention requests, human action capture |
| `src/cua/surface/` | The Surface seam: Playwright today, a desktop stub beside it |
| `mock_app/` | CoreLedger and its failure injections |
| `DECISIONS.md` | Every decision, when it was taken, and who approved it |
| `LICENSE` | GPL-3.0 |
| `CLAUDE.md` | The working contract this was built against, kept in the repo on purpose |
