# REPORT

## Architecture

I built this around three actors and one object. A model discovers a task once, on the live app. A
replay engine executes the recording with no model anywhere in the loop. A human can take the live
session when neither of the other two should proceed alone. The object between them is the
capability artifact: typed inputs, typed outputs, ordered steps, checkpoints, and outcome
detectors, versioned and approved. Everything else in the repo exists to produce that file, to run
it, or to prove what happened.

The seam I cared most about is perceiving and acting on a surface, kept separate from the recorded
flow. `src/cua/replay/` imports no browser and no model, enforced by a repo test that walks the
import graph; the CLI hands the engine a surface factory. That is why a desktop backend is a
credible next step rather than a rewrite.

I used Playwright directly instead of a computer-use SDK or a hosted agent runtime. The point of
the exercise is the seam and the artifact, not the driver, and an SDK owns exactly the part I
wanted to own: how an element is addressed, what a policy gate sees, and when a human can take
over. Playwright also gives me request interception, which is where the gate actually earns its
keep: a refused navigation is answered with a 204 before it loads, rather than noticed afterwards.
The cost is that I wrote the accessibility snapshot, the locator ladder, and the dialog handling
myself, which is most of `src/cua/surface/`.

Perception is hybrid. Each turn the model gets a screenshot and a compact accessibility snapshot
with numbered refs, roles, names, text and bounding boxes, including inside frames. CoreLedger is
deliberately hostile: its buttons are spans and table cells with onclick handlers, and Playwright's
aria snapshot reports them as cells, not buttons. So a DOM pass finds every clickable and merges it
into the snapshot. The model targets by ref only; coordinates alone are a recording error. The
alternative, pure screenshot plus coordinates, was rejected because a recorded coordinate is dead
the first time a column moves, which is precisely what the layout drift injection does.

## Artifact schema

Outcome detectors live in the artifact, not in the engine. What "Access denied" means, which screen
carries it, and whether it is a business answer or a failure is knowledge about the app, so it
belongs to the thing that versions with the app. The engine only knows the four classes: business
outcome, recoverable, hard failure, and escalate. Putting the taxonomy in the engine would have
meant a code change and a redeploy for every new app or every new error page, and two apps'
meanings of the word "denied" fighting over one enum.

Targets are ladders, not selectors. Each target carries rungs in order, `role_name`, `label_text`,
`text_exact`, `anchor_relative`, and last a normalized bounding box marked fragile, plus the rung
that was used at record time and a note per rung explaining why it survived recording. Replay tries
every rung top to bottom, requires exactly one match, and logs the rung it used. If a rung below
the recorded one wins, that is a drift signal in the result and in the log. Two matches is a
failure, not a guess. I did not record CSS ids, XPath or test ids, not because they are bad but
because the brief's environment does not have them and I wanted the design to prove it does not
need them. The layout drift run is the evidence: Find becomes Search one cell to the right, the
text rung dies, the same-row anchor rung resolves it, and the run still succeeds with a warning.

`status` exists because a recording is a draft until a human says otherwise. Replay refuses a draft
unless `--allow-draft` is passed. That is the cheap version of the confidence and approval idea:
discovery proposes, a person disposes, and the catalog keeps both. Both capabilities here ship as
two files for that reason. `@1.0.0` is what the model recorded. `@1.1.0` is what I made replayable
by hand, mostly by adding the outcome detectors that one happy run cannot possibly observe, and by
cutting one wait from ten seconds to three so a slow page trips the timeout detector instead of
being absorbed silently. Each artifact's `review_notes` says exactly that.

`tenant_overrides` exists now, empty, because adding it later would be a breaking schema change. It
is a per-tenant patch of steps and checkpoints, resolved and validated at load. It may change a
ladder, a text string, a wait or an entry URL. It may never change a step's id, action, risk class,
value, or a target's role and input type, and a field that looks like a credential in the base must
still look like one after the patch. A second tenant of the same app family gets a patch, not a
re-recording.

## Determinism and error handling

Determinism here means: the same artifact plus the same inputs plus the same app state produces the
same step sequence, the same rungs resolved or a logged drift, the same checkpoints asserted, and
the same outputs. `cua replay --repeat 5` proves it rather than asserting it. The committed
stability run is five for five, identical rungs for every step in every run, equal output digests,
and 2240 to 2347 ms end to end.

There are no fixed sleeps in replay. Every step waits on a checkpoint, a settle, or a URL change,
and every wait is a poll loop: each tick drains surface events, evaluates the step's in-scope
detectors in artifact order, then checks the wait's own condition. Timeout detectors get their turn
at the deadline, before the wait is declared failed. That ordering is what makes a slow page a
recovery rather than a timeout.

The taxonomy is the part I would defend hardest. A member who does not exist is a `business_outcome`
with code `MEMBER_NOT_FOUND`, the app's own message, the offending field named, and exit code 0. It
is an answer. A caller that treats it as a crash will build retries around a question that has
already been answered. Hard failures are exit 2 and always carry a screenshot, an accessibility
snapshot and a scrubbed trace. Escalations are exit 3 and carry the intervention request path. The
one honest exception: refusals that happen before the browser opens, such as an unapproved draft, a
missing secret or an input that fails its pattern, have no screenshot or trace because nothing was
ever on screen. They still write a result and a manifest.

A recoverable condition that exhausts its attempts becomes a hard failure with the original code,
never a generic error. If the maintenance notice will not clear after two clicks, the result says
`SYSTEM_NOTICE`, not `CHECKPOINT_TIMEOUT`. The recovery that acted owns the failure, because
"clicking OK did not work" is the fact worth reporting. Retries are bounded by risk rather than by
a counter: a failed action is retried once only when the effective risk is safe, where effective
risk is the stricter of what the step declares and what the gate rates it. An irreversible step is
never retried, and replay refuses to start at all if a step declares a lower risk than the gate
computes for it.

## Heterogeneity and multi-tenant

The `Surface` protocol is the seam: observe, snapshot, screenshot, act, drain events, frame URL,
text and status, settle, trace, and the human capture hooks. The locator ladder is defined against a
`LocatorBackend` port, so each surface implements rung matching in its own terms. `DesktopSurface`
exists as a stub whose docstring maps the same rungs onto UIA, AX and AT-SPI, and it rejects
navigate steps and URL conditions, which is the honest shape of the difference.

A different legacy web app is the same surface with worse inputs. Weaker accessibility means fewer
targets resolve by role and name, so the recorder leans harder on `label_text` and
`anchor_relative`, and the ladder is exactly the mechanism that absorbs that. In CoreLedger the
login page has real labels and a submit input, while the search and detail screens are role-less
spans and cells, which is what a real vendor patch history looks like, and the recorded artifact
shows the spread: `role_name` on the login controls, `label_text` and `text_exact` further in.

A desktop surface changes three things and nothing else. Frame paths become window paths. Rung
matching reads role, name and bounds from the OS accessibility API instead of from the DOM. URL
conditions and `url_matches` detectors are replaced by window title and control presence detectors,
which is why conditions are a union in the schema rather than a URL string. The steps, the
checkpoints, the risk classes and the result contract do not move.

Multi-tenant is one artifact per app family plus `tenant_overrides`, resolved at load and validated
like any other artifact. Drift is detected three ways: a rung below the recorded one resolving is a
drift signal on every run, a checkpoint text mismatch is a failure with the expected and observed
strings, and stability runs compare rung distributions across repeats. The path I would build next
is the one the signals already imply: collect drift signals per tenant, and when the same lower
rung wins for the same target for N runs, open a proposed override for a human to approve. The
approval gate already exists, and the override merge function already exists. Nothing about that
loop should be automatic without a person, because a target that quietly moved is also what an
attacker's injected markup looks like.

## Escalation and handoff

This is real, and I would rather describe exactly which half is mocked than claim more.

Control is a small state machine with a phase and a controller: running, paused for human, human
active, resuming, finished. It lives in `session_state.json` inside the run's evidence directory,
written under a lock with a compare-and-swap, because the paused run and the operator's CLI are
different processes and a shared file is the only thing they both see. Every act and every read
through the surface calls a control hook first, so while a human holds the session, automation
raises `NotInControl` rather than clicking underneath them.

When a detector asks for a human, or a step is irreversible under escalate handling, or a recovery
runs out of attempts, the run captures the screen, writes `intervention.json` with the reason, the
screenshot, the accessibility snapshot and the exact take-control command, and then polls the state
file without touching the browser. `cua ops list`, `show`, `take-control`, `hand-back` and `abort`
drive it from another terminal. While the human holds the session, their clicks, the fields they
leave, and their navigations are captured to `human_actions.jsonl`, redacted. One event per action,
not per keystroke: logging keystrokes into a banking session is not a thing I am willing to build.

On hand-back, replay re-runs the outcome detectors and re-verifies the interrupted step's
checkpoint, then carries on. It does not repeat the step. The human was on the same screen and may
have finished it by hand, and repeating an irreversible action because the automation was not
watching is the worst failure mode available here. The cost is that a step which never ran stays
unrun, so the intervention text tells the operator that in as many words. If re-verification fails
the run pauses again with a new request, and after three pauses it stops asking and ends escalated:
a run that keeps coming back is not converging.

What is mocked: the operator's view. They look at the real browser window on this machine. In
production that is a remote view, a queue of requests with assignment and SLAs, and an audit trail
per operator. What is real: the control transfer, the lock that keeps automation out, the capture,
the re-verification on resume, and the result record naming who held the session, their note, and
how many of their actions were captured. One committed run shows a pause nobody answers expiring
into `escalated`, exit 3. `evidence/README.md` has the eight-step walkthrough for driving the other
half by hand, and a browser test drives the whole loop without a human by moving the session file
from inside the paused run's own poll.

Discovery gets an intervention request when it is stuck, with the screen attached, but it cannot
hand over the live session. Resuming discovery means carrying the model's context across a handoff,
which is a different problem from re-verifying a checkpoint, and I would rather say that than ship a
resume that silently starts over.

## Safety

One policy file gates every action in both loops. `PolicyGate` is the single choke point, and
`GatedSurface` is the only caller of `Surface.act` in the repo, enforced by a test that walks the
syntax tree. The gate sees origins, path allow and deny lists, action kinds, and rules for what
counts as irreversible, including the text of a confirm dialog about to be answered. Irreversible
handling is per policy: `block`, `confirm`, or `escalate`. The read-only capability runs under
`escalate`, because a read-only capability that suddenly wants to commit something is off its
script and a human should look. The sub-account capability runs under `confirm`, which means
`--confirm-irreversible` on the command line and nothing implicit. Both refusal paths are tested
against the live app by asserting the ledger is still empty afterwards.

Requests are gated, not just actions. Frame documents are judged before they are sent and a refusal
is answered with a 204 so the current page stays put; the first redirect hop is fetched and its
destination judged before the browser follows it; every other request is checked against origin and
deny rules, so a page script cannot call `/__control` even though the mock leaves it open.

Secrets never reach the model. It types `{{secrets.operator_password}}` and the gate substitutes at
the browser edge, and a credential template is only accepted into a field that looks like a
credential field. Redaction runs over everything written anywhere: logs, evidence, artifacts, the
model's context, intervention requests, and a human's captured actions. It covers raw,
URL-encoded, JSON-escaped and base64 forms, which I learned the hard way when an operator id
survived inside a base64-encoded POST body in a Playwright trace. Traces are scrubbed member by
member before they land in evidence, and one that cannot be scrubbed is dropped while the result
still stands. `tests/test_leaks.py` scans every committed file, zip members included, and runs on
every commit.

The honest limits: screenshots are not image-redacted, so the manifest flags the ones taken on a
sign-in page or after a sensitive value could be on screen, and the README says to delete them
before sharing. There is no secrets manager; secrets are environment variables from a gitignored
`.env`. The allowlist is a file in the repo rather than a service. Names and free text are not
detected, so a synthetic member's name appears in the model's own sentences. And a trace keeps DOM
and screenshots, so it is always flagged sensitive even after scrubbing.

## Cuts

Image redaction of screenshots. The manifest flags what to delete instead. Next step: run the
accessibility boxes of any node whose text matched a sensitive pattern through a blur pass at
capture time, since the surface already has both the boxes and the matches.

A remote operator console. The control transfer is real but the operator has to be at this machine.
Next step: a CDP screencast or noVNC view plus a queue that assigns requests, keyed on the
intervention id that already exists.

An LLM fallback during replay. Tempting and deliberately refused: the value of the artifact is that
it runs the same way every time, and a model that improvises on a bad day makes the result
unauditable. Next step, if wanted, is a strictly bounded one: on `TARGET_NOT_FOUND`, ask a model to
propose a new ladder for that one target, then stop and require a human to approve it into the
artifact as a new version.

Code generation from an artifact. The artifact is already the executable thing, and generated code
is a second source of truth that drifts from it.

A runtime multi-tenant layer. `tenant_overrides` and its merge exist and are validated; what is not
built is per-tenant scheduling, credentials, or a service around them.

The desktop surface. Designed and stubbed, not implemented. Next step is `candidates`,
`same_element` and `describe` over the platform accessibility API, plus window-title detectors in
place of URL ones. The engine and the schema should not need to change, and that claim is the
strongest thing the stub is for.

Screens-as-states instead of ordered steps. Rejected: a runtime path choice makes replay
non-deterministic, which is the property the whole system is selling.
