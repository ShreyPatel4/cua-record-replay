# REPORT

## Architecture

Three actors and one object. A model discovers a task once on the live app. A replay engine
executes the recording with no model in the loop. A human can take the live session when neither of
the other two should proceed alone. Between them is the capability artifact: typed inputs and
outputs, ordered steps, checkpoints and outcome detectors, versioned and approved. Everything else
in the repo exists to produce that file, run it, or prove what happened.

The seam I cared about most is perceiving and acting on a surface, kept apart from the recorded
flow. `src/cua/replay/` imports no browser and no model, enforced by a repo test, and the CLI hands
the engine a surface factory. That is why a desktop backend is a next step rather than a rewrite.

I used Playwright directly rather than a computer-use SDK, because an SDK owns exactly the parts I
wanted to own: how an element is addressed, what the policy gate sees, and when a human can take
over. It also gives me request interception, which is where the gate earns its keep: a refused
navigation is answered with a 204 before it loads instead of being noticed afterwards. The cost is
that the accessibility snapshot, the locator ladder and the dialog handling are mine to maintain.

Perception is hybrid. Each turn the model gets a screenshot and a compact accessibility snapshot
with numbered refs, roles, names, text and boxes, frames included. CoreLedger's buttons are spans
and table cells, which the aria snapshot reports as cells, so a DOM pass finds every clickable and
merges it in. The model targets by ref; coordinates alone are a recording error. Screenshot plus
coordinates was the alternative, and a recorded coordinate dies the first time a column moves,
which is exactly what the layout drift injection does.

## Artifact schema

Outcome detectors live in the artifact, not in the engine. What "Access denied" means, which screen
carries it, and whether it is a business answer or a failure is knowledge about the app, so it
belongs to the thing that versions with the app. The engine only knows the four classes: business
outcome, recoverable, hard failure, and escalate. Putting the taxonomy in the engine would have
meant a code change and a redeploy for every new app or every new error page, and two apps'
meanings of the word "denied" fighting over one enum.

Targets are ladders, not selectors: `role_name`, `label_text`, `text_exact`, `anchor_relative`, and
last a normalized bounding box marked fragile, plus the rung used at record time and a note per
rung on why it survived recording. Replay tries every rung top to bottom, requires exactly one
match, and logs which rung won. A rung below the recorded one winning is a drift signal; two
matches is a failure, not a guess. No CSS ids, XPath or test ids, because the brief's environment
does not have them and I wanted the design to prove it does not need them. The layout drift run is
the evidence: Find becomes Search one cell over, the text rung dies, the same-row anchor rung
resolves it, and the run succeeds with a warning.

`status` exists because a recording is a draft until a human says otherwise, and replay refuses a
draft unless `--allow-draft` is passed. Discovery proposes, a person disposes, and the catalog
keeps every version. `@1.0.0` of each capability is what the model recorded; the later versions are
what review made replayable, mostly by adding the outcome detectors one happy run cannot observe.
Each `review_notes` says what changed and why, including the second review of the sub-account
capability, which became a major bump because it renamed an outcome code.

`policy_ref` holds a bare policy id, not a file path: which file the policies live in is deployment
configuration, and an artifact that names a path stops being portable the moment the path moves.

`tenant_overrides` exists now, empty, because adding it later would be a breaking schema change. It
is a per-tenant patch of steps and checkpoints, resolved and validated at load. It may change a
ladder, a text string, a wait or an entry URL. It may never change a step's id, action, risk class,
value, or a target's role and input type, and a field that looks like a credential in the base must
still look like one after the patch. A second tenant of the same app family gets a patch, not a
re-recording.

## Determinism & error handling

Determinism here means: the same artifact plus the same inputs plus the same app state produces the
same step sequence, the same rungs resolved or a logged drift, the same checkpoints asserted, and
the same outputs. `cua replay --repeat 5` proves it rather than asserting it. The committed
stability run is five for five, identical rungs for every step in every run, equal output digests,
and 2150 to 2253 ms end to end. Equal digests only witness determinism for a capability whose
outputs are stable: opening a sub-account mints a new number every time, so the witness there is
the same steps, the same rungs and the same checkpoints, not the same output.

There are no fixed sleeps. Every step waits on a checkpoint, a settle or a URL change, and every
wait is a poll loop: each tick drains surface events, runs the step's in-scope detectors in
artifact order, then checks the wait's own condition. Timeout detectors get their turn at the
deadline, before the wait is declared failed, which is what makes a slow page a recovery rather
than a timeout.

A derived checkpoint can be too specific, and this one bit me. The recorder puts an accepted
input's value into the success checkpoint when it can see that value on the final screen, which is
usually the strongest check available. It is wrong for anything the app reformats: 1000.00 typed
comes back as $1,000.00, the checkpoint fails, and a run that did exactly what it was asked reports
a hard failure a caller would retry into a second real sub-account. Amounts are now left out of
derived checkpoints, and the capability was re-issued without the condition.

The taxonomy is what I would defend hardest. A member who does not exist is a `business_outcome`,
`MEMBER_NOT_FOUND`, with the app's own message, the offending field named, and exit 0. It is an
answer, and a caller that treats it as a crash builds retries around a question already answered.
Hard failures are exit 2 and always carry a screenshot, an accessibility snapshot and a scrubbed
trace. Escalations are exit 3 and carry the intervention path. The one exception: refusals before
the browser opens, such as an unapproved draft or a missing secret, have no screenshot because
nothing was ever on screen. They still write a result and a manifest.

A recoverable condition that exhausts its attempts keeps the detector's own code and never becomes
a generic error. If the maintenance notice will not clear after two clicks, the result says
`INTERSTITIAL`, not `CHECKPOINT_TIMEOUT`, and it ends escalated or as a hard failure according to
that detector's `on_exhausted`. The recovery that acted owns the failure, because
"clicking OK did not work" is the fact worth reporting. Retries are bounded by risk rather than by
a counter: a failed action is retried once only when the effective risk is safe, where effective
risk is the stricter of what the step declares and what the gate rates it. An irreversible step is
never retried, and replay refuses to start at all if a step declares a lower risk than the gate
computes for it.

## Heterogeneity & multi-tenant

The `Surface` protocol is the seam: observe, snapshot, screenshot, act, drain events, frame URL,
text and status, settle, trace, and the human capture hooks. The locator ladder is defined against a
`LocatorBackend` port, so each surface implements rung matching in its own terms. `DesktopSurface`
exists as a stub whose docstring maps the same rungs onto UIA, AX and AT-SPI, and it rejects
navigate steps and URL conditions, which is the honest shape of the difference.

A different legacy web app is the same surface with worse inputs. Weaker accessibility means fewer
targets resolve by role and name, so the recorder leans on `label_text` and `anchor_relative`, and
the ladder is the mechanism that absorbs that. CoreLedger's login page has real labels while its
search and detail screens are role-less spans and cells, the way a real patch history leaves an
app, and the recorded artifact shows the spread: `role_name` on the login controls, `label_text`
and `text_exact` further in.

A desktop surface changes three things and nothing else. Frame paths become window paths. Rung
matching reads role, name and bounds from the OS accessibility API instead of from the DOM. URL
conditions and `url_matches` detectors are replaced by window title and control presence detectors,
which is why conditions are a union in the schema rather than a URL string. The steps, the
checkpoints, the risk classes and the result contract do not move.

Multi-tenant is one artifact per app family plus `tenant_overrides`, resolved and validated at
load. Drift shows up three ways: a lower rung resolving is a signal on every run, a checkpoint text
mismatch is a failure carrying expected and observed, and stability runs compare rung distributions
across repeats. The next step is the one those signals imply: collect them per tenant, and when the
same lower rung wins for the same target N times, open a proposed override for a human to approve.
Both halves already exist. None of it should be automatic, because a target that quietly moved is
also what injected markup looks like.

## Escalation & handoff

This is real, and I would rather describe exactly which half is mocked than claim more.

Control is a state machine with a phase and a controller: running, paused for human, human active,
resuming, finished. It lives in `session_state.json` in the run's evidence directory, written under
a lock with a compare-and-swap, because the paused run and the operator's CLI are different
processes and the file is the only thing they both see. Every act and every read calls a control
hook first, so while a human holds the session automation raises `NotInControl` rather than
clicking underneath them.

A detector asking for a human, an irreversible step under escalate handling, or a recovery out of
attempts all pause the run: it captures the screen, writes `intervention.json` with the reason and
the exact take-control command, and polls the state file without touching the browser. `cua ops
list`, `show`, `take-control`, `hand-back` and `abort` drive it from another terminal. What the
human does is captured to `human_actions.jsonl`, redacted, one event per action rather than per
keystroke: keystroke logging into a banking session is not something I will build.

On hand-back, replay re-runs the outcome detectors and re-verifies the interrupted step's
checkpoint, then carries on. It does not repeat the step. The human was on the same screen and may
have finished it by hand, and repeating an irreversible action because the automation was not
watching is the worst failure mode available here. The cost is that a step which never ran stays
unrun, so the intervention text tells the operator that in as many words. If re-verification fails
the run pauses again with a new request, and after three pauses it stops asking and ends escalated:
a run that keeps coming back is not converging.

Mocked: the operator's view, which is the real browser window on this machine rather than a remote
view with a request queue and per-operator audit. Real: the control transfer, the lock, the
capture, the re-verification, and a result that names who held the session, their note and how many
of their actions were recorded. A committed run shows a pause nobody answers expiring into
`escalated`, exit 3; a browser test drives the whole loop by moving the session file from inside
the paused run's own poll; `evidence/README.md` has the walkthrough for doing it by hand.

Discovery gets an intervention request when it is stuck, but cannot hand over the live session.
Resuming discovery means carrying the model's context across a handoff, a different problem from
re-verifying a checkpoint, and I would rather say so than ship a resume that quietly starts over.

## Safety

One policy file gates every action in both loops. `PolicyGate` is the single choke point and
`GatedSurface` is the only caller of `Surface.act` in the repo, enforced by a test that walks the
syntax tree. The gate sees origins, path allow and deny lists, action kinds, and what counts as
irreversible, including the text of a confirm dialog about to be answered. Irreversible handling is
per policy. The read-only capability uses `escalate`, because a read-only capability that suddenly
wants to commit something is off its script. The sub-account capability uses `confirm`, which means
`--confirm-irreversible` on the command line and nothing implicit. Both refusal paths are tested
against the live app by asserting the ledger is still empty afterwards.

Requests are gated, not just actions. A frame document is judged before it is sent and a refusal is
answered with a 204 so the current page stays put; the first redirect hop is fetched and its
destination judged before the browser follows it; every other request is checked on origin and deny
rules, so a page script cannot reach `/__control` even though the mock leaves it open.

Secrets never reach the model: it types `{{secrets.operator_password}}` and the gate substitutes at
the browser edge, only into a field that looks like a credential field. Redaction covers everything
written anywhere, in raw, URL-encoded, JSON-escaped and base64 forms, which I learned the hard way
when an operator id survived inside a base64 POST body in a trace. Traces are scrubbed member by
member, and one that cannot be scrubbed is dropped while the result stands.
`tests/test_leaks.py` scans every committed file, zip members included, on every commit.

The honest limits: screenshots are not image-redacted, so the manifest flags the ones on a sign-in
page, on a member record, or after a sensitive value could be on screen, and the README says to
delete them before sharing. There is no secrets manager; secrets are environment variables from a
gitignored `.env`, and the allowlist is a file rather than a service. Names and free text are not
detected, so a synthetic member's name appears in the model's own sentences. A trace keeps DOM and
screenshots, so it stays flagged sensitive even after scrubbing.

## Cuts

Image redaction of screenshots. The manifest flags what to delete instead. Next: blur the
accessibility boxes of nodes whose text matched a sensitive pattern at capture time, since the
surface already has both the boxes and the matches.

A remote operator console. Control transfer is real, but the operator has to be at this machine.
Next: a CDP screencast or noVNC view and a queue that assigns requests, keyed on the intervention
id that already exists.

An LLM fallback during replay. Refused on purpose: the artifact's value is that it runs the same
way every time, and a model improvising on a bad day makes the result unauditable. The bounded
version I would build instead is on `TARGET_NOT_FOUND`, ask a model to propose a new ladder for
that one target, then stop and make a human approve it into a new version.

Code generation from an artifact. The artifact is already the executable thing; generated code is a
second source of truth that drifts from it.

A runtime multi-tenant layer. `tenant_overrides` and its merge exist and are validated. Per-tenant
scheduling, credentials and a service around them do not.

The desktop surface. Designed and stubbed. Next: `candidates`, `same_element` and `describe` over
the platform accessibility API, plus window-title detectors in place of URL ones. The engine and
the schema should not need to change, and proving that is what the stub is for.

Screens-as-states instead of ordered steps. Rejected: a runtime path choice makes replay
non-deterministic, which is the property the whole system is selling.
