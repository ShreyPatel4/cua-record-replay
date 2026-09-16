# interface-cua: session context

Take-home for interface.ai. `docs/brief.pdf` (gitignored, local only) is the source of truth for
requirements, deliverable paths, and the seven REPORT.md headings. The sections below are copied
from the kickoff (its sections 1, 2, and 6) so every session builds to the same decisions.

Gates, all must be green before any commit:

```sh
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
```

Git: SSH remote only, identity from global config, no Co-Authored-By or Claude attribution lines.

## Decision log

`DECISIONS.md` is the append-only audit trail: one row per decision that changed what the system
does, with the date and who approved it. Add a row whenever a decision is taken or confirmed, in
the same commit as the change. This file keeps the reasoning; that one keeps the record.

## Deviations from the kickoff, decided in phase 0

- `docs/brief.pdf` is gitignored. It is interface.ai's document; the public repo should not
  republish it.
- Member 10013 (restricted) returns 403 on member detail as well as on sub-account create. The
  kickoff only named create, but then `read_savings_balance` could never produce
  `PERMISSION_DENIED` for a restricted member. `permission_denied` injection takes `on=create`
  (default) or `on=detail` to force it on any member.
- Injections are armed with an optional use count (`times`, None means until cleared) and string
  params, all validated at arm time (bad input is a 400, never a 500 or a silent no-op).
  `session_expired` defaults to one use. `slow` takes `ms` (default 5000, inside the kickoff's
  4 to 8 s band). Because the default checkpoint timeout is 10 s, the member-detail step must carry
  a shorter per-step checkpoint timeout (3 s) so `slow` actually trips `checkpoint_timeout` and the
  `wait_retry` recovery (budget 15 s) is exercised rather than silently absorbed.
- `layout_drift` inserts a status cell and renames Find to Search in the same row. Text rungs and
  the bbox rung break; a same-row anchor rung (right of the textbox) survives. Exactly one rung
  degrades, so the drift signal is meaningful. The search form has `onsubmit="return false"` so
  pressing Enter cannot bypass the drifted control.
- `interstitial` hides the member card (`visibility:hidden`) until OK, so an extraction that ignores
  the notice fails instead of passing for the wrong reason. `sticky=1` never reveals it.
- Login `next` drops `inject*` params (no redirect loop) and only accepts same-origin paths.
- `CORELEDGER_ALLOW_QUERY_INJECTION=0` disables `?inject=`. `/__control` is unauthenticated on
  purpose (local mock only); the policy denies it to the agent.
- Login page has real wrapped `<label>`s and an `<input type=submit>` ("a later vendor patch").
  Search, detail, and sub-account pages keep role-less spans and tds. Realistic legacy apps are
  inconsistent, and it gives the ladder a real spread of rungs.
- `.env.example` leaves `CORELEDGER_OPERATOR_PASSWORD` blank; the app refuses to start without it.
  No committed file holds a working credential.

## Decisions confirmed at the phase 1 stop (delegated by Shrey on 2026-09-12)

- `artifacts/example.capability.json` as of commit 51ab5b1 is signed off as the target shape.
- Screens-as-states redesign declined; it is a REPORT.md cut.
- The no-em-dash rule covers artifacts. The recorder normalizes authored prose (descriptions,
  notes); CoreLedger pages contain no em dashes, so captured text is never altered in practice.
- Dev operator id is `teller-0417`, not a dictionary word.
- Phase 0 deviations stand: restricted member 403 on detail, `slow` 5 s against a 3 s step wait,
  `docs/brief.pdf` gitignored.
- Model stays `claude-sonnet-4-6` (kickoff choice, verified with the project key).
- Discovery runs on the current project key right after phase 2; ask for a fresh key only if it
  has expired.

## Settled in phase 1 (the phase 0 review's open items)

- Repeat runs vs self-clearing faults: `IterationSummary.conditions` records the fault set the
  harness re-armed before each iteration; `StabilityReport.determinism` compares only iterations
  with equal conditions and says `insufficient_repeats` when a group has one run.
- Slow-path timing is pinned by `test_slow_path_timing_makes_the_recovery_reachable`.
- `select_option` exists; `ClickStep.dialog` declares confirm/alert and the answer.
- URL conditions and waits carry `frame_path`.
- Irreversibility keys on the accepted dialog (gate `phase="dialog"`), not on the POST target.
- Perception facts from the probe stand: `aria_snapshot(mode="ai", boxes=True)` walks frames, refs
  are reassigned every snapshot and never enter an artifact, and a narrow DOM pass flags `onclick`
  spans and tds because ai-mode marks them inconsistently.

## Phase 1 contract decisions (pushbacks on the kickoff sketch)

- `tenant_overrides` is top level, keyed by tenant id, each with its own `entry_url`, `steps` and
  `checkpoints` patches. Objects deep-merge, lists replace whole (a ladder is one unit). Patches may
  change targets and waits but never `id`, `action`, `risk`, `on_fail`, `value`, `option_label`,
  `url`, `key`, `dialog`, `target.fingerprint.role` or `target.fingerprint.input_type`. A target that
  looks like a credential field in the base must still look like one after the patch. Every tenant
  is resolved and validated at load, and with a policy file its `entry_url` must pass the policy.
- `secrets` are `{{secrets.name}}` templates backed by env vars, each with a `kind`. A `credential`
  (password, PIN) is typed only into a target that looks like a credential field (name, label,
  anchor, text rung, or `input_type=password`) and is redacted anywhere, including every base64
  alignment. An `identity` (operator id) may go into any field and is redacted as a whole token, so
  the dev operator id is a non-word (`teller-0417`). Secrets and sensitive inputs must be a whole
  typed value, never in URLs, conditions, or option labels, and their targets never record text.
- Conditions may template non-sensitive inputs (`cp_member_loaded` checks the requested member
  number is on screen); URL patterns render them regex-escaped.
- Detectors have an optional `scope`; it is required for `run_steps` recoveries and
  `checkpoint_timeout` triggers, and a timeout trigger's scoped steps must wait on that checkpoint.
  Detector codes may not reuse engine codes, and one code has one outcome type.
- `run_steps` re-runs earlier steps, each through the gate and its own wait, except the last: its
  wait is replaced by re-verifying the interrupted step's checkpoint. Verified against the mock:
  signing in again after expiry redirects back to `/members/10007`, so s04's own `cp_search_ready`
  can never hold during recovery. Re-run steps come before the scope, in flow order, and are never
  irreversible or dialog-answering. `od_session_expired` is scoped to s06, the only step whose page
  checks the session mid-flow.
- `Recoverable.on_exhausted` (hard_failure or escalate) decides what an exhausted recovery becomes;
  the code stays the detector's. `AutomaticRecovery.succeeded` records the exhausted ones.
- Accepting a `confirm` requires the step to declare `risk: irreversible`.
- `BusinessOutcome.field` must be a declared input and needs `message_from`; replay returns it in
  `ReplayResult.field_errors`. `step_reached` is null only for pre-run codes (`INPUT_INVALID`,
  `DRAFT_NOT_APPROVED`, `TENANT_UNKNOWN`, `SECRET_MISSING`, `POLICY_MISMATCH`).
  `ReplayResult.contract_problems(capability)` checks codes, output types and required outputs;
  `for_evidence` masks sensitive inputs and outputs. Output digests are HMAC with a per-report key.
- `HumanIntervention.outcome` is handed_back, aborted or expired, so an abort after take-control is
  representable. Session state records `operator_id` (only the holder may hand back or abort) and
  `owner_pid`; abort is legal from running, paused, human_active and resuming; expire from paused
  and human_active; a pause that ends the run keeps its intervention id.
- Semver: major for inputs (shape, removal, new required), outputs (removal, type, sensitivity),
  outcome codes (removal, reclassification), secrets (names, kinds, env vars), surface kind, and
  `policy_ref`. Minor for any flow change, new optional inputs, new outputs, new codes. Patch for
  wording only.
- Catalog: only drafts are saved, approved files are never overwritten, `get` without a version
  returns the latest approved (drafts only with `include_drafts`), and a bad file is named in the
  error.
- Policy gate: `ProposedAction.phase` is action, navigation (a document request the page started,
  intercepted before sending) or dialog (the real message, about to be answered). Paths are decoded
  once, slashes collapsed and dot segments resolved before matching; double encoding, backslashes,
  `;` path parameters, control characters, userinfo and bad ports are blocked; query names match
  case-insensitively; IPv6 origins are bracketed.
- Redaction keeps `$` amounts, comma and decimal amounts, and compact run-id timestamps; any other
  run of five or more digits keeps its last four, including in comma lists and next to letters.
  Apply it to string values, not rendered JSON numbers.
- `Strict` models refuse attribute assignment but nested lists and dicts are not deep-frozen; build
  changed copies through `model_dump` and `model_validate`.
- `surface` is a discriminated union `web | desktop`. Desktop is designed, not built, and rejects
  navigate steps and URL/status conditions.
- Targets carry a `fingerprint` (role, name, redacted text, input type at record time) and `notes`
  explaining the ladder, which is the brief's "reasoning about robustness" per target.
- `127.0.0.1` everywhere instead of `localhost` (macOS resolves localhost to ::1 first).
- `artifacts/example.capability.json` is the hand-written reference; the catalog does not list it
  but it replays by path. Catalog files are `<id>@<version>.capability.json`. Artifacts follow the
  no-em-dash rule; only `evidence/` is exempt.

## Phase 2 decisions (surface and locators, revised after the phase 2 review)

- Contracts: `surface/base.py` holds the `Surface` protocol (observe, snapshot, screenshot,
  `act(action, dialog_guard)`, `install_request_guard`, `drain_events`, `element_for_ref`,
  `frame_url`/`frame_text`/`frame_status`, `settle`). `locate/backend.py` holds `LocatorBackend`
  (`candidates`, `same_element`, `describe`): the ladder defines the port, each surface implements
  it. Frames are the web's containers; a desktop surface maps frame paths onto window paths.
  `DesktopSurface` is a stub whose docstring maps UIA, AX, and AT-SPI onto the same rungs.
- `PlaywrightSurface.launch(browser, control=...)` owns its context, created with
  `accept_downloads=False` and `service_workers="block"`. `control` is required and raises
  `NotInControl` unless automation holds the session; every act and every read calls it.
- Perception: the page-root `aria_snapshot(mode="ai", boxes=True)` is parsed into nodes. Boxes inside
  a frame are frame-relative in that text, so the parser adds the frame's content offset (box plus
  border). Playwright single-quotes lines whose text contains `: ` or ` #`; the parser unwraps them.
  Input values never become node text: textbox-like nodes keep only `value_present` and a salted
  in-process digest (excluded from serialization) so the snapshot digest still notices typing.
  `A11ySnapshot.redacted(redactor)` masks names, text, and URLs for the model and evidence.
- Click handlers: ai-mode marks them inconsistently, so a DOM pass finds every clickable. A handler
  marks the smallest node containing it (ties to the deeper node) when that node's label is the
  handler's text; otherwise (a span inside a paragraph) it becomes its own `clickable` node with a
  `c<N>` ref the surface resolves.
- A wrapper around exactly one control with the same visible text stands for that control: the
  aria `cell "Find"` and the span that owns the handler are one target.
- Rung matching lives in `surface/locator.js`, one script for recorder and resolver. Text is dash-
  and whitespace-normalized and case-folded. Visible means rendered, not visibility:hidden, not
  under opacity:0, and not entirely off the page. `label_text` `same_row` is the first matching
  control after the label in its table row, or off tables the nearest one in its vertical band.
  Position rules (`anchor_relative`, `below` labels, off-table rows) return every candidate tied
  for the chosen slot, so ties fail the one-match rule instead of picking by DOM order. `bbox`
  never returns frame or body elements. Accessible names approximate WAI-ARIA accname
  (labelledby, aria-label, label without its controls' text, placeholder, title, image alt).
- Resolver: a candidate counts only if its kind, role, and input type match the fingerprint
  (`Fingerprint.kind` is new: input, select, checkbox, clickable, text). A fallback therefore
  cannot land on an empty cell where a button was. A renamed winner still resolves (layout drift
  turns Find into Search) but sets `identity_changed`, which replay treats with caution. A bbox
  rung needs a fingerprint role or name (schema-enforced). Ambiguous rungs are skipped;
  `TARGET_AMBIGUOUS` only when nothing won and a rung was ambiguous.
- Recorder: every candidate rung is resolved on the live page and kept only if it finds exactly
  this element. `role_name` is skipped for non-semantic roles; the browser's own role and name from
  the snapshot node are tried first. Text that looks like data (amounts, 3+ digit runs, dates) is
  never used in any rung or anchor. `sensitive` or `volatile_text` drops text rungs and fingerprint
  name and text for content-named elements; form controls keep their label-derived name. A weak
  target is flagged; coordinates alone are a `RecordingError`.
- Gate wiring: `GatedSurface` (`policy/enforce.py`) is the only caller of `Surface.act`, and code
  outside `surface/` may not touch element handles; a repo test walks the syntax tree to enforce
  both. The request guard installs once. Every request leaving the page goes through a
  context-level route (popups included):
  - frame documents are judged before sending; a refused one is answered with HTTP 204, which
    cancels the navigation and keeps the current document (an abort would show the browser error
    page);
  - the first redirect hop is fetched by the surface (`route.fetch(max_redirects=0)`) and its
    Location judged before the browser follows it, so a sign-in redirect to a denied path never
    loads; later hops of a chain are followed without routing and are detected, not prevented;
  - every other request (fetch, XHR, images) is judged on origin, `paths_deny`, and `query_deny`
    only (new `subresource` phase), so a page script cannot call a denied endpoint or another
    origin while same-origin assets still load;
  - popups are closed; a popup whose first request was refused never loads, and Playwright never
    surfaces it to close, so the guarantee is that it cannot reach the denied page;
  - download links never reach routing or request events, so an init script cancels them in the
    page and reinstalls itself after `document.open()` rewrites (a page rewritten from an isolated
    world, like Playwright's own `set_content`, is not covered);
  - `data:`, `blob:`, and other non-http documents cannot be prevented by routing and are reported
    as `navigation_off_policy`; from such a document the frame's own navigations fail the gate, so
    recovery is a top-level navigation.
- Dialogs: only answered while an act is in flight, and only the expected one (type and message
  pattern), after the gate. A click that expects a dialog waits for it or fails with "expected
  dialog did not appear", and the expectation is cleared when the act returns, so a later confirm
  is never auto-accepted. Anything else is dismissed and reported.
- Events carry the id of the act they belong to (the latest act when they arrived). An act's result
  holds only its own events; late arrivals and strays stay in `drain_events` with their id.
- Human control: the request guard and the dialog handler step aside, so the human's navigation is
  not gated and their confirm is theirs to answer. Automation reads and acts both raise
  `NotInControl`. Control is checked before an act, not during a long one; phase 5 takes control
  only between acts.
- `settle` means no document, XHR, or fetch request in flight plus a DOM mutation quiet period,
  measured by an init script in every frame and polled every 50 ms.
- `confirm_irreversible` satisfies `confirm` handling only; `escalate` always refuses. Enter on a
  target is judged like a click, and URL risk rules in the navigation phase also match the
  destination.
- Every target in `artifacts/example.capability.json` resolves at its recorded rung on the live mock
  with no identity change (`test_the_hand_written_example_resolves_on_the_live_app`). The example
  gained `fingerprint.kind` on its targets (additive, still 1.0.0 draft).

## Phase 3 decisions (discovery)

- Loop per turn: observe (screenshot plus the snapshot passed through the redactor) -> model ->
  parse -> GatedSurface -> settle -> observe. Only the latest screen is sent in full; older ones are
  elided. `tool_choice` is `auto` with parallel tool use off: `any` prefills the reply, so the model
  never writes the one-sentence rationale the run log keeps. A reply with no tool call is answered
  with "Call exactly one tool."
- Tools are Pydantic models that also generate the schemas. A malformed call becomes a tool error the
  model reads; it never crashes a run. Beyond the kickoff's ten: `select_option(ref, option)`,
  because the sub-account form has a native select, and `click.dialog` (accept or dismiss), allowed
  only after an earlier click on the same element reported the dialog's message (the surface
  dismisses unexpected dialogs). The recorded message pattern is that exact message, anchored.
- Secrets: the model types `{{secrets.name}}` templates and never sees values. A credential goes
  only into a field that looks like a credential field, and a literal typed into a credential
  field is refused.
- Recording: a target's ladder is recorded before the action, on the screen the model saw, and
  its notes give a reason per kept rung. A literal the goal names becomes a proposed input when the
  run typed or chose it (name from the field's label) or put it in a navigated URL (segments and
  query values with a digit; name from the preceding segment, `members/10007` -> `member_id`).
  Digit patterns are fixed length only when the field's maxlength enforces it, else `^[0-9]+$`. A
  human confirms at the end, by prompt or `--yes`; a declined literal stays literal. Same-origin
  URLs are rewritten under `{{surface.entry_url}}`, and a URL still holding a 5+ digit number fails
  the build. Dialog message patterns keep the wording and generalize digit runs.
- Preflight, before any model call: the capability id shape, and that `<id>@1.0.0` is not already
  in the catalog. `cua discover` also refuses to start without a terminal or `--yes`.
- Waits: type and select steps settle. Other steps wait on a derived checkpoint: the first new
  interface text in the next target's frame (not data, not a control's label, not a typed literal,
  and not a value cell sitting just right of a label cell, such as a member's name) plus the next
  target present. A done target in a value cell is volatile too. The last step waits on the success checkpoint: that text, each
  accepted input's template when its value is on screen, and the output and done targets present.
  The success checkpoint must hold on the final screen or the draft is not saved.
- Drafts have no outcome detectors. One happy run cannot observe not-found, interstitial, slow,
  expiry, or app errors, so review adds them and tunes timeouts; `review_notes` says so.
- Outputs are recorded sensitive. From `declare_output` on, the value (and its bare number forms)
  is added to the redactor as a whole token, so logs and the model's later screens mask it;
  `read_text` contents are never logged; screenshots from that screen on are flagged sensitive.
  Model-written text (rationale, summary, reasons, tool arguments) is logged after the tool runs and
  through `Redactor.free_text`, which also masks amounts, because the model may quote a value
  before declaring it or in a run that never declares one. The prompt asks it not to.
- Stuck: the same call three times running (refs compared by frame, role, and name), or two acting
  turns running with no snapshot digest change (Tab and scroll do not count, they can be legit
  no-ops), plus max steps, timeout, and give_up. Until phase 5 a stuck run stops with status
  `stopped`, exit 2, and keeps its last accessibility snapshot. The timeout is checked between
  turns, so a turn in flight finishes first: at most two model calls of 90 s plus a 10 s settle.
- A reply cut off at `max_tokens` (now 2048) is never executed; the model is told to shorten.
- The SDK's own retries are off; the loop retries a transient error (connection, timeout, 408, 409,
  429, 529, 5xx) exactly once and fails the run on the second.
- `request_confirmation` is denied unless `--allow-irreversible`; with it, it covers exactly the
  next tool call, whatever it is, via `GatedSurface.perform(confirmed=True)`. So a dialog flow is:
  confirm, click (the dialog is reported and dismissed), confirm, click with dialog accept.
  `escalate` handling is never satisfied, and the model is told to give up rather than work around
  it. Screen text is declared untrusted data in the prompt; the gate bounds what injection can do.
- Late side effects: an act that can start a navigation (click, key, navigate) stays open until no
  request has started for 150 ms (capped at 1 s), so the navigation is routed and judged inside
  that act and a confirmation still covers it. After every settle the loop drains stray events; a
  refused navigation or an unexpected dialog there marks the action failed and drops its step.
- `settle` counts its own call as activity. Found by probe: an observation right after a submit
  could still show the sign-in page because the request event arrived after the first idle check.
- Evidence: `evidence/<run_id>/` with `run.jsonl`, `step_NN.png`, `result.json`, `manifest.json`,
  `artifact.capability.json`, and `a11y_NN.json` when stopped (`a11y_final.json` when done was
  reached but no draft was saved). A crash still writes `result.json` and the last snapshot.
  `tool_result` events carry `step_id` and the recorded rung; `checkpoints_derived` and
  `success_checkpoint` events show the waits and the final check. Redaction skips `ts`, `digest`,
  and `sha256`, and keeps URL ports. `--evidence-root` defaults to `evidence/_scratch`
  (gitignored). No Playwright trace in discovery: the phase 4 trace writer must first keep the
  sign-in POST out of traces, then discovery can reuse it.
- Limit for REPORT.md: names and other free text are not detected, so the synthetic member's name
  appears in the model's rationale.
- The first real run was deleted (empty rationale, balance in the log). Both were fixed with tests
  and the run was redone. After the phase 3 review (1 blocking, 2 high, 8 lower findings, all
  fixed) the run was redone as `evidence/disc_20260914T055730Z_9596/` so the committed evidence
  matches the reviewed recorder.

## Phase 4 decisions (replay)

- `replay/engine.py` sees only the surface, locator, and gate contracts, and nothing under `replay/`
  imports Playwright (repo test). `replay/run.py` takes a surface factory; the CLI builds it.
- Artifact `coreledger.member.read_savings_balance@1.1.0` is the hand review of the 1.0.0 draft:
  the example's seven detectors ported and renamed (`member_number`, `cp_member_profile`), s06's
  wait cut to 3 s. Written by hand, not by a tool; `check_version` confirms a minor bump.
- Poll loop: each tick drains surface events (a refused request is POLICY_BLOCKED, an unexpected
  dialog UNEXPECTED_DIALOG), runs in-scope detectors, then the check. Target resolution polls the
  same way. `checkpoint_timeout` detectors run once per deadline; a wait_retry window sets its own
  trigger aside, so it cannot re-trigger itself. Click and run_steps recoveries settle 300 ms (so a
  sign-in still redirecting is not read as a second expiry) and restart the interrupted wait with
  its full timeout. Settle waits poll detectors between settle attempts of quiet_ms plus 250 ms.
- New engine codes: `ACTION_FAILED` (a failed act, retried within the step timeout unless the step
  is irreversible or answers a dialog) and `TARGET_CHANGED` (a renamed target on an irreversible
  step; on a safe step it is a drift warning). `DriftWarning.identity_changed` is new.
- Escalation passes through one hook. Until the session controller exists, `no_operator` turns it
  into `hard_failure` with the escalating code and a message that a human is needed, so
  permission_denied and app_error return exit 2 in phase 4. Phase 5 rewires the hook.
- Pre-run refusals, in order: tenant, approval, policy (load and fit), inputs, secrets. Each is a
  hard_failure with no step_reached, writes result.json and a manifest, and opens no browser.
- Evidence: `step_NN.png` after every step, `a11y_NN.json` on failure, `trace.zip` on hard_failure
  and escalated only. Tracing runs from step 1, so a failure during sign-in still has a trace
  (the advisor suggested starting after sign-in; that would leave early failures without one).
  The raw archive lives in a temp dir; `evidence/trace.py` writes a copy with every secret form
  replaced in every member and cookie and auth header values blanked, flagged sensitive. Found on
  the first real trace: `route.fetch` keeps the sign-in POST body base64-encoded, and identities
  had no base64 forms, so the operator id survived. Identities now carry base64 forms.
- Sensitive outputs: the read text and its bare number forms go into the redactor before parsing;
  result.json goes through `for_evidence`; screenshots from the success step on are flagged. A
  failure trace on the profile page can hold the balance in its DOM snapshots; traces are flagged.
- `cua mock inject|clear|reset` drive the control API so evidence runs are reproducible commands.
  Replay itself never touches `/__control`; the policy denies it.
- `cua replay --repeat N` writes `stability_<ts>/` with each run and `stability.json` (passes, rung
  distribution, duration spread, determinism, HMAC output digests under an in-memory key). It
  exits 0 only when every run passed and the runs are not nondeterministic.

### Carried into phases 5 and 6 from the phase 4 close

- REPORT.md, Determinism and error handling: pre-run refusals (tenant, deprecated, draft, policy,
  inputs, secrets) stop before a browser opens, so they have no screenshot, snapshot, or trace.
  The DoD row "hard_failure always has a screenshot, a11y snapshot, and trace" holds for runs
  that reached the browser only.
- REPORT.md: `catalog list` shows `@1.0.0` (the discovered draft) and `@1.1.0` (the reviewed,
  detector-carrying version). Explain that this is discovery then human review, not clutter.
- `@1.1.0` was approved as Shrey Patel under delegation; re-approve before submission if wanted.
- `UNEXPECTED_DIALOG` raised from the event drain and a `SETTLE` wait timing out are covered by
  scripted-surface unit tests only, not live runs.
- Phase 5 resume re-verifies the interrupted step's checkpoint and re-runs outcome detectors
  (brief section 2.9); it does not re-run the step's action. `_StepInProgress` carries the step.
- Phase 5 rewires `no_operator`: permission_denied becomes `escalated` exit 3 with an
  intervention path, and its committed phase 4 evidence run is replaced.

### Fixed after the phase 4 review (3 blocking, 6 high, 7 medium, lows and nits)

- Session cookies survived the trace scrub in Playwright's log records (`set-cookie: ...` text).
  The scrubber now blanks header values in every shape (header objects, cookie arrays, header
  text in log messages), learns the cookie values it saw plus the live jar from
  `Surface.session_tokens()`, and replaces those values in every member and encoding. The browser
  tests scan every trace member for any surviving cookie value.
- Optional outputs swallowed any stop, including policy blocks and escalations, and could crash
  the result validator. Only an `OUTPUT_EXTRACTION_FAILED` with no failed recovery is skipped now,
  and surface events are drained once more after the last output is read.
- A failed act was retried by declared risk; a step declared safe that the gate rates irreversible
  could submit twice. Retry and `TARGET_CHANGED` use the higher of declared and gate risk
  (`GatedSurface.judge` answers without acting), and policy fit refuses a step whose declared risk
  is below what the gate computes from the artifact alone.
- A click or run_steps recovery whose restarted wait never verifies now owns the failure and its
  code (SESSION_EXPIRED, not CHECKPOINT_TIMEOUT or SLOW_LOAD); a re-run step that cannot resolve,
  act, or settle is reported as that recovery failing. Attempts count per step and detector across
  every wait of the step. Several wait_retry windows can be open at once, each trigger set aside.
- Escalation handlers return the stop: `no_operator` gives a hard failure, and an operator channel
  returns `escalated` with its `intervention_path` (exit 3, tested with a stub). Resume after a
  hand-back is phase 5: it turns the engine's stop into a loop in one place.
- Evidence: failure snapshots mask dollar amounts from the success step on; a failure keeps the
  step screenshot and adds `step_NN_failure.png`; the unfinished step gets a `passed=false` record;
  a trace that cannot be scrubbed is dropped and the result stands; a crash still writes
  `result.json` with status `crashed`.
- Pre-run: `CAPABILITY_DEPRECATED` is its own code; a secret shorter than four characters and an
  optional input the flow uses are refusals, not crashes; undeclared input values are masked.
- Redactor: dollar amounts are masked at any length, number tokens never match inside a longer
  number (4.0.00), short credentials carry base64 forms, identities carry JSON-escaped forms.
- Found while producing the evidence: a detector's description is the caller-facing message, so
  review rationale (why two detectors are unscoped) belongs in `review_notes`, not descriptions;
  and a hard-failure detector's `expected` is now what the step was waiting for.
- `@1.1.0` was approved as "Shrey Patel" under the delegated decisions, after the review fixes and
  the full gate; the committed replay evidence ran against the approved file without
  `--allow-draft`.
- Left as documented limits: pre-run refusals have no screenshot, snapshot, or trace (no browser
  opened, by design); `--repeat` from the CLI records no harness conditions, so a self-clearing
  injection reads as nondeterministic there; the 3 s profile wait held under CPU load in the
  review but is not proven on a slower CI machine.

## Phase 5 decisions (escalation and handoff)

- The escalation hook returns one of three things: a hard failure (`no_operator`, nobody on call),
  an escalated stop carrying `intervention_path`, or a `Handback` when an operator took the live
  session and gave it back. A hand-back unwinds to the step loop through `_HandedBack`, so the
  re-verification lives in one place instead of at every escalation site.
- Resume re-runs the step's in-scope detectors and re-verifies its checkpoint. It never repeats
  the action: the human works on the same screen and may already have done it by hand. A step
  whose wait is a settle or a URL change gets one detector pass instead, and its
  `HumanIntervention.reverified` is false, because there is nothing to check again.
- A stop raised while re-verifying belongs to that hand-back: it is reported as
  `POST_HANDOFF_CHECKPOINT_FAILED` with the underlying message, and it moves the session
  `resuming -> paused_for_human` with a new intervention, which is the brief's own arrow.
- `MAX_HUMAN_PAUSES` is 3. A run that keeps coming back to a human is not converging, so it stops
  asking and ends escalated with the last request. The cap is logged as `escalation_capped`.
- `cua replay --escalation-timeout` is the wait in seconds (default 300). 0 attaches no operator
  channel at all, which is the phase 4 behaviour and what an unattended caller with nobody on call
  wants. A pause nobody answers expires, and the run ends escalated, exit 3.
- Control is a file, not a flag: `session_state.json` in the run's evidence directory. The surface
  calls `file_control(path)` before every act and every read, so while a human holds the session
  automation raises `NotInControl`. No file means no pause has ever happened and automation owns
  the session. The ops CLI is a separate process and only ever writes that file.
- Human action capture is one event per click, per field left, per navigation, never per
  keystroke. Values reach the sink raw, so the sink redacts: a value typed into a field that looks
  like a credential is added to the redactor before anything is written, and
  `human_actions.jsonl` is flagged sensitive.
- The paused run polls the state file and prints the pause banner to stderr, so stdout stays the
  caller's JSON result.
- A stuck discovery run writes an `intervention.json` (kind `discovery`) with the screen attached,
  but the live session is not handed over. Resuming discovery means carrying the model's context
  across the handoff, which is a different problem from replay's re-verification. That is a
  REPORT.md cut with one line on what it would take.
- The operator id comes from `--operator`, `$CUA_OPERATOR`, or `$USER`. It lands in the session
  file, the request, and the result, so committed evidence uses a demo id, not a real account.

### Fixed after the phase 5 review (3 blocking, 4 high, 7 medium, lows and nits)

- A hand-back on a step whose wait is a settle crashed the run: the result validator counted only
  `reverified` hand-backs as successful recoveries, and a settle step has no checkpoint to
  re-verify. A `handed_back` intervention now counts; `reverified` still means the checkpoint
  passed.
- An operator who took the session and walked away held the browser forever: the expiry branch
  was unreachable once the phase was `human_active`. Expiry is checked every poll, and taking
  control restarts the clock so an operator who takes it at the last second gets the full window.
- An operator note reached `session_state.json` unredacted through the ops CLI. Notes are
  redacted against the environment's secrets before the transition, and the session file and its
  lock are flagged sensitive, because redaction cannot know a credential an operator invents.
- Losing control mid-run was a traceback. `NotInControl` becomes an `OPERATOR_ABORTED` stop that
  keeps the run's intervention path, and the channel's own end-of-life transitions never raise:
  an aborted session is already over.
- The pause's ending is read from its own transitions, so an abort on a run nobody took is no
  longer recorded as an expiry, and an operator who answered an earlier pause is not blamed for a
  later one.
- A recovery in flight when the escalation happened vanished from `recoveries`: `_HandedBack` now
  closes the open recoveries on its way out, like `RunStopped` does.
- The escalation text promised something resume does not do. It now says replay will not repeat
  the step, so a step that never happened has to be done by hand before the hand-back.
- Capture starts at the pause, not at take-control: the window is live and clickable in between.
  The action log is created by the first captured action, so a pause nobody answered leaves no
  empty file. A value too short to redact no longer throws inside the page binding.
- The capture script's credential-field rule is built from `vocab.SENSITIVE_FIELD_RE`, so an OTP
  or SSN field is masked the same way a password is.
- `channel.close` moved into the `finally`, so a crashed run never leaves a session that looks
  live. `ops list` marks a session whose process is gone as dead and refuses `take-control` on it.
- The channel's transitions carry the version they read, so an expiry cannot kill a session an
  operator took between the read and the write.
- `ops show` opens the screenshot by default, as the brief says. Both copies of a request go
  through the evidence writer. The writer's redactor stayed private; `run_replay` passes it to the
  channel factory.
- Skipped, with the reason: re-running the step's action after a hand-back when the action never
  fired. `_StepInProgress.acted` makes it possible, but decision 20 says resume re-verifies and
  does not act, so the text tells the operator instead. Revisit only if the demo asks for it.
- For REPORT.md: `take-control` does not bring the window forward itself. The paused run does,
  within one poll, because only the run's process owns the browser.
- An escalated run's stopping screen is `intervention_NN.png` and `a11y_intervention_NN.json`,
  captured at the pause. After the pause the session is finished and belongs to nobody, so the
  step capture is skipped rather than forced: the control check is the safety story and it stays
  strict. `run.jsonl` logs `capture_skipped`, which is not the same event as `capture_failed`.
- `--escalation-timeout` is inert under `--repeat`: a stability batch is unattended by
  definition, so no operator channel is attached to its runs.

## Phase 6 decisions (the irreversible capability and the write-up)

- `coreledger.member.open_subaccount` was discovered by a real run, not written by hand. The first
  attempt named an account type CoreLedger does not offer, and the model gave up rather than
  improvising, which is the stuck path doing its job. The second attempt recorded 11 steps in 15
  turns, with `s11` irreversible and carrying the confirm dialog's exact message.
- Found by that run and fixed: an error tool result may not carry an image, so the screen now
  rides beside it in the same turn. The API answers a 400 otherwise, which ended a real run.
- `@1.1.0` adds ten detectors by hand: session expiry, app error, access denied, member not found,
  member number rejected, three separate VALIDATION_ERROR detectors (deposit, account type,
  nickname) so each names its own input, the maintenance notice, and the slow member page. s06's
  wait is cut to 3 s for the same reason as the read capability.
- `od_session_expired` is scoped to s06 and s07 only. Recovering at s11 would re-submit the
  irreversible create after a session bounce, and that is a decision for a human.
- Three replay evidence runs: success with `--confirm-irreversible`, `CONFIRMATION_REQUIRED`
  without it, and `VALIDATION_ERROR` for a deposit under the minimum. Both refusal paths are
  pinned by tests that assert the ledger is still empty afterwards.
- README leads with the path that uses no model at all, because that is the claim being made.
  REPORT.md uses the brief's seven headings and states the cuts with what I would build next.
- The final leak sweep covers every tracked file plus every zip member. A new test pins that the
  operator id, an identity rather than a credential, never survives redaction into an artifact or
  an evidence file in any encoding.

## Runtime semantics the schema now pins (field descriptions are the spec)

- Every wait is a poll loop (about 250 ms). Each tick evaluates the step's in-scope detectors in
  artifact order, then the wait condition; the first detector match ends the wait. At a checkpoint
  wait's deadline, `checkpoint_timeout` detectors get their turn, else `CHECKPOINT_TIMEOUT`.
- Target resolution retries each tick until the step's wait timeout, then `TARGET_NOT_FOUND` or
  `TARGET_AMBIGUOUS`. Every rung is tried top to bottom on every replay; a later rung than
  `recorded_rung` raises a drift warning, an earlier one does not.
- Visible text is the element's own innerText, whitespace-collapsed; hidden elements never match.
  `text_present` is a case-sensitive substring of the frame body's innerText.
- `anchor_relative`: direction means starting past the anchor's edge and overlapping it on the other
  axis, ordered by distance; `same_row` is the anchor's nearest `tr`; `clickable` is a, button,
  submit or button input, role button or link, or any element with `onclick`; `nth` is 1-based.
- `status_code` is the most recent document response committed in the frame.
- Click recovery restarts the interrupted wait with its full timeout; wait_retry keeps polling the
  same wait (detectors included) for `max_total_ms`.
- Messages are extracted first and redacted before they enter a result or evidence.

## Deferred from the phase 1 review, with reasons

- Screens-as-states graph: declined. The brief asks for ordered steps, a runtime path choice makes
  replay non-deterministic, and the findings it targeted are fixed inside the linear model. It goes
  in REPORT.md cuts as the next design step.
- Separate per-tenant implementation files: `tenant_overrides` resolved at load covers the need
  today. REPORT.md cuts.
- Overrides swapping union variants or patching detectors, `option_map` for enum inputs, and
  `skip_if` preconditions: add only if `open_subaccount` needs them, otherwise REPORT.md cuts.
- Fake-clock test of the poll loop: the loop is phase 4 engine code; the test lands with it.
- Gate interception (phase 2 obligation): `PlaywrightSurface` routes frame document requests
  through the gate with `phase="navigation"` and calls it from the dialog handler with
  `phase="dialog"` before answering. Until then the gate only sees what the caller hands it.
- Heartbeat lease beyond `owner_pid`: phase 5, if the handoff demo needs it.
- The evidence writer must use `ReplayResult.for_evidence` and the redactor on string values only
  (phase 4).
- A text rung reading like a credential ("Forgot password") forbids recording that target's text.
  Over-strict on purpose; discovery omits the text.

---

## 1. The mental model

There are three actors and one object.

- The **model** discovers. It sees a screen, decides, acts. It only runs in discovery.
- The **artifact** is the product of discovery. It is a capability an AI agent can call: typed inputs, typed outputs, ordered steps, checkpoints, outcome detectors. It never contains the model transcript.
- The **replay engine** executes the artifact with no model in the loop and returns a structured result.
- The **human** is a third actor who can take the live session when the other two cannot safely proceed.

The seam that matters most: **perceiving and acting on a surface** is one layer; **the recorded flow** is another. The flow must not know it is running on Playwright. If I swap in a desktop surface, the artifact and replay engine should not change, only the surface adapter and the locator resolvers.

## 2. Decisions already made

### 2.1 Stack

Python 3.11+. `uv` for env and dependency management. Playwright with Chromium as the surface. Pydantic v2 for every schema and every result type. Typer for the CLI. pytest for tests, ruff for lint and format, mypy in strict mode on `src/`. Structured logging through `structlog` writing JSON lines. Nothing else unless you can defend it in one sentence.

Layout:

```
src/cua/
  surface/        Surface protocol, PlaywrightSurface, DesktopSurface stub, a11y snapshot, screenshot
  locate/         locator ladder: models, recorder (ref -> ladder), resolver (ladder -> handle)
  policy/         PolicyGate, allowlist loader, risk classes, Redactor
  artifact/       Capability schema, versioning, validation, catalog (load/save/list)
  discover/       agent loop, tools, prompt, recorder that turns a run into an artifact
  replay/         engine, outcome detectors, recovery actions, ReplayResult
  session/        SessionController (control lock), intervention, operator CLI, human action capture
  evidence/       run logger, evidence writer, manifest
  cli.py          typer app: discover, replay, ops, catalog, mock
mock_app/         Flask legacy credit union core, failure injection
policy/allowlist.yaml
artifacts/        saved capabilities (JSON)
evidence/         committed runs (see section 9)
tests/
docs/brief.pdf
README.md REPORT.md CLAUDE.md .env.example
```

### 2.2 LLM

Anthropic API, model `claude-sonnet-4-6`, official `anthropic` Python SDK, native tool use. Key from `ANTHROPIC_API_KEY` in `.env`, loaded with `python-dotenv`, never committed. Ship `.env.example`.

Tools exposed to the model, and nothing else:

| tool | args | notes |
|---|---|---|
| `click` | `ref` | ref is a numbered node from the a11y snapshot |
| `type_text` | `ref`, `text`, `clear_first` | text may be a literal or `{{inputs.name}}`; the loop substitutes before acting, the model never sees sensitive input values (see 2.8) |
| `press_key` | `key` | Enter, Tab, Escape only |
| `scroll` | `direction`, `amount` | |
| `navigate` | `url` | gated by allowlist |
| `read_text` | `ref` | returns visible text of the node; used to declare an output |
| `declare_output` | `name`, `ref`, `type` | marks a node as an extraction target |
| `request_confirmation` | `reason` | must be called before any action classified `irreversible` |
| `done` | `summary`, `checkpoint_ref` | goal met, checkpoint_ref is the node that proves it |
| `give_up` | `reason` | model cannot proceed; triggers escalation |

The system prompt tells the model: it is operating a legacy banking UI, it must target by ref only, it must not guess coordinates, it must call `request_confirmation` before anything that creates or changes a record, it must call `declare_output` for any value the goal asks it to read, and it must stop with `done` only after it can see the evidence on screen. Keep the prompt under 60 lines and check it in as a file, not a string literal buried in code.

Loop shape per turn: observe (screenshot + a11y snapshot with refs) -> model call -> parse tool call -> PolicyGate check -> act via Surface -> wait for settle -> log. Stopping conditions: `done`, `give_up`, `max_steps` (default 25), `timeout` (default 180s), or stuck detection (section 2.7). Retry the model call once on transient API error, then fail the run.

### 2.3 Perception

Hybrid, and this is a deliberate stance on the "no clean DOM" requirement. Each turn the model receives:

1. A full page screenshot (PNG, scaled to max 1280 wide).
2. A compact accessibility snapshot: a numbered list of interactive and text-bearing nodes with `ref`, `role`, `name`, visible text (truncated to 80 chars), and bounding box. Include nodes inside iframes and frames, prefixed with a frame path. Generate it from Playwright's accessibility snapshot plus a fallback DOM walk for nodes the a11y tree misses (legacy apps put click handlers on divs and spans; those need to show up).

The model targets by ref. The recorder converts the ref into a locator ladder at record time. This is the same mental model as screenshot-plus-coordinates but with a stable handle, and it is the model that transfers to desktop: OS accessibility APIs expose the same role/name/bounds triple.

Phase 0 note (verified on Playwright 1.62): the mock app's span and td controls do appear in the aria snapshot, but only as table cells (`cell "Find"`, `cell "OK"`), never as buttons, and the search textbox has no accessible name. So `role_name` cannot tell interactive cells from data cells, and the recorder leans on `label_text`, `text_exact`, and `anchor_relative` outside the login page. Pinned by `tests/test_mock_app_browser.py`. `page.accessibility` no longer exists in 1.62.

### 2.4 Target application

Build `mock_app/` as a local Flask app called "CoreLedger" that imitates a legacy credit union core. Deliberately hostile:

- Top-level frameset or at least one iframe wrapping the main work area.
- Table-based layout, nested tables for the member detail card.
- No `id`, no `data-testid`, class names like `c1`, `row`, `pnl`.
- Buttons are `<span onclick=...>` or `<td onclick=...>`, not `<button>`.
- Inline JS `confirm()` on the sub-account submit.
- Login page with fake credentials `operator / <password>` from `.env`, never hardcoded in the app or the artifact.
- Session cookie with a configurable TTL.
- Seed data: 15 synthetic members with fake names, member IDs `10001..10015`, savings and checking balances, one flagged "restricted" member for the permission-denied path. No real names, no real anything.

Flow: login -> member search (text field + search span) -> member detail (shows savings balance in a nested table) -> "Open sub-account" link -> form (account type select, nickname text, initial deposit) -> confirm dialog -> confirmation screen with a generated sub-account number.

Failure injection, all deterministic, toggled by a control page at `/__control` and by query param `?inject=<name>` for tests:

| injection | trigger | correct classification |
|---|---|---|
| `not_found` | search for member ID not in seed | `business_outcome` code `MEMBER_NOT_FOUND` |
| `validation_error` | initial deposit below 5.00 | `business_outcome` code `VALIDATION_ERROR` with field and message |
| `interstitial` | member detail shows a "System notice" modal with an OK span | `recoverable`, recovery `dismiss_modal` |
| `slow` | member detail renders after 4 to 8 s | `recoverable`, recovery `wait_and_retry` up to 15 s |
| `session_expired` | mid-flow redirect to login | `recoverable` once via `reauthenticate` step, then `hard_failure` if it recurs |
| `permission_denied` | sub-account creation on a restricted member returns a 403 page | `hard_failure` code `PERMISSION_DENIED`, escalate |
| `app_error` | member detail returns a 500 page | `hard_failure` code `APP_ERROR`, escalate |
| `layout_drift` | search button label changes from "Find" to "Search" and moves to a different table cell | replay must still succeed by falling to a lower ladder rung; record the rung |

Ship `uv run cua mock serve` to start it and a smoke test that hits every injection route.

### 2.5 Artifact schema

This is the focal point of the evaluation. Design it before the agent loop exists. Pydantic models in `artifact/schema.py`, serialized to JSON. Every field has a description. Here is the shape I want, and you should push back if something is wrong rather than silently changing it:

```json
{
  "schema_version": "1.0",
  "capability": {
    "id": "coreledger.member.read_savings_balance",
    "version": "1.0.0",
    "status": "draft",
    "name": "Read member savings balance",
    "description": "Log in, look up a member by ID, return their current savings balance.",
    "created_from_run_id": "disc_2026...",
    "created_at": "...",
    "policy_ref": "policy/allowlist.yaml#coreledger-readonly"
  },
  "surface": {
    "kind": "web",
    "entry_url": "http://localhost:5050/",
    "app_family": "coreledger",
    "app_version_hint": "legacy-1",
    "tenant_overrides": {}
  },
  "inputs": [
    {"name": "member_id", "type": "string", "required": true, "pattern": "^[0-9]{5}$", "sensitive": false, "description": "Five digit member number"}
  ],
  "outputs": [
    {"name": "savings_balance", "type": "money", "description": "Current savings balance as shown on the member card", "extract": {"target": "<locator ladder>", "parse": "money_usd"}}
  ],
  "steps": [
    {
      "id": "s01",
      "action": "navigate",
      "value": "{{surface.entry_url}}",
      "risk": "safe",
      "wait_for": {"kind": "checkpoint", "ref": "cp_login_visible"},
      "on_fail": "hard_failure"
    },
    {
      "id": "s03",
      "action": "type_text",
      "target": {
        "ladder": [
          {"strategy": "role_name", "role": "textbox", "name": "Member number", "confidence": 0.9},
          {"strategy": "label_text", "text": "Member number", "relation": "nearest_input"},
          {"strategy": "anchor_relative", "anchor_text": "Find", "direction": "left", "role": "textbox"},
          {"strategy": "bbox", "x": 0.31, "y": 0.22, "w": 0.18, "h": 0.03, "fragile": true}
        ],
        "recorded_rung": 0,
        "frame_path": ["main"]
      },
      "value": "{{inputs.member_id}}",
      "risk": "safe",
      "wait_for": {"kind": "settle", "ms": 300}
    }
  ],
  "checkpoints": {
    "cp_login_visible": {"all": [{"text_present": "Operator sign-in"}]},
    "cp_member_loaded": {"all": [{"text_present": "Member profile"}, {"element_present": "<ladder for savings cell>"}]},
    "cp_done": {"ref": "cp_member_loaded"}
  },
  "outcome_detectors": [
    {"id": "od_not_found", "when": {"text_present": "No member found"}, "class": "business_outcome", "code": "MEMBER_NOT_FOUND", "message_from": {"text_of": "<ladder>"}},
    {"id": "od_interstitial", "when": {"element_present": "<ladder for System notice OK>"}, "class": "recoverable", "recovery": {"kind": "click", "target": "<ladder>", "max_attempts": 2}},
    {"id": "od_slow", "when": {"checkpoint_timeout": "cp_member_loaded"}, "class": "recoverable", "recovery": {"kind": "wait_retry", "max_total_ms": 15000}},
    {"id": "od_session", "when": {"url_matches": "/login"}, "class": "recoverable", "recovery": {"kind": "run_steps", "step_ids": ["s01","s02"], "max_attempts": 1}},
    {"id": "od_403", "when": {"text_present": "Access denied"}, "class": "hard_failure", "code": "PERMISSION_DENIED", "escalate": true},
    {"id": "od_500", "when": {"any": [{"text_present": "Internal Server Error"}, {"status_code": 500}]}, "class": "hard_failure", "code": "APP_ERROR", "escalate": true}
  ],
  "success": {"checkpoint": "cp_done", "requires_outputs": ["savings_balance"]},
  "provenance": {"recorded_by": "discover", "model": "claude-sonnet-4-6", "evidence_run": "evidence/disc_.../", "review_notes": ""}
}
```

Rules on the schema:

- `id` is dotted and stable across versions; `version` is semver. Bump minor for step changes that keep the contract, major for input/output contract changes.
- `status` is `draft` after recording and must be flipped to `approved` by a human (`cua catalog approve <id>`) before unattended replay runs without `--allow-draft`. This is the "confidence and approval" stretch goal folded in cheaply.
- Values reference inputs by `{{inputs.x}}`. The recorder is responsible for spotting the literal the model typed and replacing it with the parameter. It asks me to confirm the mapping at the end of discovery.
- Outcome detectors are evaluated after every step, in order, before the step's own checkpoint. The first match wins.
- `tenant_overrides` exists and is empty. It is a map of `step_id -> partial step` and `checkpoint_id -> partial checkpoint` so a second tenant on the same `app_family` can override a ladder or a text string without re-recording. Document in REPORT.md how drift detection would populate it. Do not build the multi-tenant layer beyond this field and its merge function.
- The artifact must be readable by a human in a text editor. Keep the JSON tidy and stable in key order.

### 2.6 Locator ladder and determinism

Recorder: given a ref from the a11y snapshot, produce the ladder in this order and record which rung was used: `role_name`, `label_text`, `text_exact` (normalized whitespace and case), `anchor_relative`, `bbox` (normalized to viewport, flagged fragile). Each rung carries a confidence. If the node has no accessible name and no text, `anchor_relative` becomes rung one and the recorder logs a warning that this target is weak. Never record a CSS id, XPath, or test id as a rung. Not because they are always bad, but because the brief says the real environment does not have them and I want the design to prove it does not need them.

Resolver on replay: try rungs in order, require exactly one match, log the rung used. If a lower rung than recorded resolved, emit a `drift_signal` in the run log and in `ReplayResult.warnings`. Two or more matches is a resolution failure, not a guess. Zero matches on all rungs is `hard_failure` code `TARGET_NOT_FOUND` with the ladder and a screenshot in evidence.

Waiting: no fixed sleeps in replay. Every step has a `wait_for` that is either a checkpoint, a settle (network idle plus DOM mutation quiet for N ms), or a URL change. Checkpoint timeout default 10 s, overridable per step. Recoverable detectors can extend it.

Determinism means: same artifact plus same inputs plus same app state produces the same step sequence, same rungs resolved (or a logged drift), same checkpoints asserted, same outputs. Write this sentence into REPORT.md and make the `stability` command prove it: `cua replay --repeat 5` runs the artifact five times and reports pass count, rung distribution, and timing spread.

### 2.7 Replay result contract and error taxonomy

One Pydantic model, `ReplayResult`:

```
status: Literal["success", "business_outcome", "recovered_then_success", "hard_failure", "escalated"]
capability_id, capability_version, run_id, duration_ms
outputs: dict[str, Any]               # present on success and recovered_then_success
outcome_code: str | None              # MEMBER_NOT_FOUND, VALIDATION_ERROR, PERMISSION_DENIED, APP_ERROR, TARGET_NOT_FOUND, CHECKPOINT_TIMEOUT, POLICY_BLOCKED, ...
message: str | None                   # human readable, redacted
step_reached: str                     # step id
expected: str                         # what the checkpoint or detector expected
observed: str                         # what was actually on screen, redacted
recoveries: list[RecoveryRecord]      # what recovered, how many attempts
warnings: list[str]                   # drift signals, weak targets
evidence_dir: str
```

Non-negotiables:
- `MEMBER_NOT_FOUND` and `VALIDATION_ERROR` return as `business_outcome` with exit code 0. They are answers, not crashes. There is a test that asserts this and the test name says why.
- `hard_failure` returns exit code 2 and always has a screenshot, an a11y snapshot, and a Playwright trace zip in evidence.
- `escalated` returns exit code 3 and includes the intervention request path.
- Recoverable conditions that exhaust their attempts become `hard_failure` with the original code, not a generic error.
- Policy blocks during replay are `hard_failure` code `POLICY_BLOCKED` and never silently skip a step.

### 2.8 Safety and redaction

`policy/allowlist.yaml`:

```yaml
policies:
  coreledger-readonly:
    origins: ["http://localhost:5050"]
    paths_allow: ["/", "/login", "/members/search", "/members/*"]
    paths_deny: ["/admin/*", "/__control*"]
    actions_allow: ["navigate", "click", "type_text", "press_key", "scroll", "read_text"]
    risk:
      irreversible_when:
        - {action: "click", target_text_matches: "(?i)(create|submit|open account|confirm)"}
        - {url_matches: "/members/*/subaccounts/create"}
      reversible_when:
        - {action: "type_text"}
    irreversible_handling: "escalate"     # block | confirm | escalate
  coreledger-subaccount:
    extends: coreledger-readonly
    paths_allow: ["/members/*/subaccounts/*"]
    irreversible_handling: "confirm"
```

`PolicyGate.check(action) -> Allow | Block(reason) | RequiresConfirmation(reason)` is the single choke point. Discovery and replay both call it. In discovery, `RequiresConfirmation` is auto-denied unless `--allow-irreversible` was passed, and the model is told so in the tool result so it can call `give_up` or find another route. In replay, an irreversible step under `escalate` handling pauses and raises an intervention; under `confirm` it requires `--confirm-irreversible` on the CLI; under `block` it is `POLICY_BLOCKED`.

Redaction (`policy/redact.py`), applied before any write to logs, artifacts, screenshots filenames, intervention requests, and the model context:
- Credentials: any value from `.env` and anything typed into a field whose accessible name matches `(?i)(pass|pin|secret|token)` becomes `[REDACTED]`.
- Account and member numbers: keep last four, mask the rest.
- Inputs marked `sensitive: true`: never appear in the artifact, only the parameter name does; in logs they are masked.
- Page text sent to the model: run the same masking on the a11y snapshot text. The model never needs the real password to click a login button.
- Screenshots: keep them, but the evidence manifest marks any screenshot taken on a page whose URL matches a `sensitive_pages` list, and README says how to purge them. Do not build image redaction; say it is a cut.

Add a test that greps every file under `artifacts/` and `evidence/` for the demo password and fails if it appears. Run it in CI and pre-commit.

### 2.9 Escalation and handoff

This must be real, not a TODO. The operator UI can be a CLI.

**Control model.** `SessionController` owns the one Playwright browser context (headed for the escalation demo, headless otherwise). It has a state machine:

```
controller: automation | human | nobody
state:      running | paused_for_human | human_active | resuming | finished

running --(stuck detected)--> paused_for_human      controller: nobody, intervention written
paused_for_human --(ops take-control)--> human_active   controller: human, action capture on
human_active --(ops hand-back)--> resuming          controller: automation, capture off
resuming --(checkpoint re-verified)--> running
resuming --(checkpoint fails)--> paused_for_human   new intervention, reason "post-handoff checkpoint failed"
```

Every `Surface.act` call checks `controller == automation` and raises `NotInControl` otherwise. This is the enforcement, and there is a unit test for it. The state is persisted to `evidence/<run_id>/session_state.json` so the ops CLI and the paused run agree on who is in control. The paused run polls that file (or a local socket, your call) and never touches the browser while `controller != automation`.

**Stuck detection**, any of:
- max steps or timeout hit in discovery
- same tool call with same args three times in a row
- an action produced no change in the a11y snapshot hash two turns running
- `give_up` called
- replay detector classified `hard_failure` with `escalate: true`
- replay reached an `irreversible` step under `escalate` handling
- recoverable recovery exhausted its attempts

**InterventionRequest** written to `evidence/<run_id>/intervention.json`:

```
run_id, kind (discovery | replay), capability_id, goal, step_id, reason_code, reason_text,
screenshot_path, a11y_snapshot_path, current_url (redacted), suggested_actions (free text),
requested_at, expires_at
```

Printed to stdout with the exact resume command.

**Operator CLI** (`cua ops`):
- `cua ops list` shows paused runs
- `cua ops show <run_id>` prints the request and opens the screenshot
- `cua ops take-control <run_id>` flips controller to human, brings the headed browser window to front, starts capturing human actions
- `cua ops hand-back <run_id> [--note "..."]` flips controller to automation, stops capture, writes the note
- `cua ops abort <run_id>` finishes the run as `escalated` with reason "operator aborted"

**Human action capture.** While `controller == human`, attach page-level listeners (Playwright `page.on("framenavigated")`, plus an injected `addInitScript` that reports click, input, and change events with the target's role, name, and text through `page.expose_function`). Write to `evidence/<run_id>/human_actions.jsonl`, redacted. On hand-back, the resume logic re-runs the outcome detectors and re-verifies the current step's checkpoint before continuing. The final result carries `recoveries` with a `HumanIntervention` record including the note.

**What is mocked and why**, stated in REPORT.md: the operator sees the real browser window on the same machine. In production this would be a remote view (VNC, CDP screencast, or a browser-in-container with noVNC) and a queue of intervention requests with assignment. The control lock, the state file, the capture, and the resume re-verification are real and are the part that matters.

### 2.10 Evidence

`evidence/<run_id>/`:
- `run.jsonl`: one line per event: ts, actor (`model` | `replay` | `human` | `policy`), step_id, action, target summary, policy decision, rung used, checkpoint result, detector match, model rationale (discovery only, redacted), duration_ms
- `step_NN.png` per step
- `a11y_NN.json` on failure and on escalation
- `trace.zip` (Playwright trace) on `hard_failure` and `escalated`
- `intervention.json`, `human_actions.jsonl`, `session_state.json` when relevant
- `result.json`: the `ReplayResult` or discovery summary
- `manifest.json`: list of files with sha256 and a `sensitive` flag

`evidence/README.md` at the top level lists every committed run, what it demonstrates, and the command that produced it.

Phase 0 note: Playwright trace zips record network bodies, including the sign-in POST, where the password is stored form-encoded in `resources/*.dat`. The leak scan in `tests/test_leaks.py` opens zip members and matches raw, URL-encoded, JSON-escaped, and base64 forms for that reason; the trace writer in phase 4 must keep credentials out of them (for example by excluding the sign-in step from tracing or stripping network resources).

## 6. REPORT.md guidance

Seven headings, exact wording and order from the brief. First person, plain prose, 1 to 3 pages, no bullet spam, no em dashes anywhere. Each section states the decision, the alternative I rejected, and why. Things I want covered:

- **Architecture**: the three-actor model, single process, why Playwright over a CUA SDK (control over the seam, cheaper, the artifact is the product not the SDK), why hybrid perception.
- **Artifact schema**: why outcome detectors live in the artifact and not the engine (they are app knowledge), why ladders not selectors, why `status` and `tenant_overrides` exist now.
- **Determinism & error handling**: the determinism sentence, no fixed sleeps, one-match rule, drift signal, the taxonomy with the not_found example, what happens when recovery exhausts.
- **Heterogeneity & multi-tenant**: the Surface seam; how a legacy web app is the same surface with weaker a11y and heavier reliance on `anchor_relative`; how a desktop surface maps role/name/bounds from OS accessibility APIs and drops `url_matches` detectors for window-title detectors; how one artifact per `app_family` plus `tenant_overrides` avoids re-recording; how drift is detected (rung fallbacks, checkpoint text mismatches, stability runs) and promoted into overrides with a human approving.
- **Escalation & handoff**: the state machine, the lock, what is mocked (operator view) versus real (control transfer, capture, resume).
- **Safety**: the gate, risk classes, why `escalate` is the default for irreversible in read-only capabilities, redaction, and the honest limits (no image redaction, no secrets manager, allowlist is a file).
- **Cuts**: image redaction, remote operator console, assisted LLM fallback on replay, code generation, multi-tenant runtime, desktop implementation. For each, one line on what I would build next and roughly how.
