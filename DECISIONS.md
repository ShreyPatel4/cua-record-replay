# Decision log

Append-only. One row per decision that changed what the system does, in the order the decisions
were taken. `CLAUDE.md` holds the reasoning and the full contract; this file is the audit trail:
what was decided, when, by whom, and where to read the detail.

Shrey Patel is the approver throughout. "Delegated" means Shrey asked for the call to be made on
his behalf and confirmed it at the phase stop.

| # | Date | Phase | Decision | Approval | Detail |
|---|---|---|---|---|---|
| 1 | 2026-09-12 | 0 | Python with uv, Playwright, Pydantic v2, Typer. No CUA SDK: the artifact is the product, and the Surface seam has to stay ours. | Shrey | CLAUDE.md "Deviations from the kickoff, decided in phase 0", section 2.1 |
| 2 | 2026-09-12 | 0 | CoreLedger mock app, deliberately legacy: frames, a table layout, weak a11y, a maintenance interstitial. Real dev secrets on port 5050 only, tests use ephemeral servers with random passwords. | Shrey | section 2.4 |
| 3 | 2026-09-12 | 0 | Secrets live only in a gitignored `.env`. `tests/test_leaks.py` enforces it over raw, URL-encoded, JSON-escaped and base64 forms, zip members included. | Shrey | section 2.8 |
| 4 | 2026-09-12 | 1 | Ordered steps, not a screens-as-states graph. A runtime path choice would make replay non-deterministic. | Delegated | CLAUDE.md "Deferred from the phase 1 review" |
| 5 | 2026-09-12 | 1 | Outcome detectors live in the artifact, not the engine. They are app knowledge, so they version with the app. | Delegated | section 2.5 |
| 6 | 2026-09-12 | 1 | Capability artifacts are versioned and approved. Replay refuses a draft unless `--allow-draft` is passed. | Delegated | section 2.5 |
| 7 | 2026-09-13 | 2 | A locator ladder with one match required per rung, and a drift signal when a lower rung wins. No CSS or XPath selectors. | Delegated | section 2.6 |
| 8 | 2026-09-13 | 2 | `GatedSurface` is the only caller of `Surface.act`, enforced by an AST test over the repo. | Delegated | CLAUDE.md "Phase 2 decisions" |
| 9 | 2026-09-13 | 2 | Hybrid perception: the accessibility snapshot is the model's primary input, screenshots are evidence. | Delegated | section 2.3 |
| 10 | 2026-09-14 | 3 | The discovery run is real: `claude-sonnet-4-6` drove the live mock and the recorder wrote `@1.0.0`. No hand-written artifact stands in for it. | Shrey | evidence/README.md, `disc_20260914T055730Z_9596` |
| 11 | 2026-09-14 | 3 | The model never sees credential values. It gets templates, and the gate substitutes at the browser edge. | Delegated | section 2.8 |
| 12 | 2026-09-14 | 4 | Replay never imports Playwright or an LLM. The CLI hands it a surface factory. Enforced by a repo test. | Delegated | CLAUDE.md "Phase 4 decisions (replay)" |
| 13 | 2026-09-14 | 4 | Retry only when the effective risk is safe, where effective risk is the stricter of the declared risk and the gate's own rating. Irreversible steps never retry. | Delegated | CLAUDE.md "Phase 4 decisions (replay)" |
| 14 | 2026-09-14 | 4 | Replay refuses to start when a step declares a lower risk than the gate rates it. | Delegated | `src/cua/policy/fit.py` |
| 15 | 2026-09-14 | 4 | Tracing runs from step 1 and is scrubbed before it lands in evidence. A trace that cannot be scrubbed is dropped and the result stands. | Delegated | `src/cua/evidence/trace.py` |
| 16 | 2026-09-14 | 4 | Pre-run refusals stop before a browser opens, so they carry no screenshot, snapshot or trace. Goes in REPORT.md as a stated limit. | Delegated | `src/cua/replay/run.py` |
| 17 | 2026-09-14 | 4 | `@1.1.0` was approved as Shrey Patel under delegation, so the evidence runs pass through the approval gate instead of `--allow-draft`. Both versions stay in the catalog: `@1.0.0` is what discovery produced, `@1.1.0` is what human review made replayable. | Delegated, confirmed 2026-09-15 | `artifacts/`, `review_notes` in the 1.1.0 file |
| 18 | 2026-09-15 | 4 | Phase 4 escalation ends as `hard_failure` with the reason code kept, because there is no operator channel yet. Phase 5 rewires it to `escalated`, exit 3, with an intervention path, and replaces the committed `permission_denied` run. | Shrey | CLAUDE.md "Carried into phases 5 and 6" |
| 19 | 2026-09-15 | 4 | New engine codes: `ACTION_FAILED`, `TARGET_CHANGED`, `CAPABILITY_DEPRECATED`. | Shrey | `src/cua/vocab.py` |
| 20 | 2026-09-15 | 5 | On hand-back, resume re-runs the outcome detectors and re-verifies the interrupted step's checkpoint. It does not repeat the step's action, because the human may already have performed it. | Shrey | section 2.9 |
| 22 | 2026-09-15 | 5 | A step whose wait is a settle or a URL change has no checkpoint to re-verify, so a hand-back there gets one detector pass and is recorded as not re-verified. | Delegated | `src/cua/replay/engine.py` `_reverify` |
| 23 | 2026-09-15 | 5 | A run pauses for a human at most 3 times, then ends escalated with the last request rather than ping-ponging. | Delegated | `MAX_HUMAN_PAUSES` |
| 24 | 2026-09-15 | 5 | Control is the session file in the run's evidence directory. Every act and read goes through `file_control`, so a human holding the session locks automation out. | Delegated | `src/cua/session/state.py` |
| 25 | 2026-09-15 | 5 | Human capture records one event per click, per field left, and per navigation. No keystroke logging, and credential values are added to the redactor before anything is written. | Delegated | `HUMAN_JS`, `OperatorChannel._write_action` |
| 26 | 2026-09-15 | 5 | Stuck discovery runs write an intervention request but cannot hand over the live session. Resuming discovery is a REPORT.md cut. | Delegated | `src/cua/discover/run.py` |
| 27 | 2026-09-15 | 5 | Committed evidence uses a demo operator id, never a real account name, because the id lands in the session file, the request, and the result. | Delegated | evidence/README.md |
| 34 | 2026-09-16 | 5 | An escalated run's stopping screen is the pause capture (`intervention_NN.png`, `a11y_intervention_NN.json`). No screenshot is taken after the session ends, because automation no longer holds it and the control check stays strict. | Delegated | evidence/README.md |
| 29 | 2026-09-16 | 5 | A hand-back counts as a successful recovery even when the step had no checkpoint to re-verify. `reverified` keeps its narrow meaning: the checkpoint passed. | Delegated | phase 5 review, finding 1 |
| 30 | 2026-09-16 | 5 | Taking control restarts the wait, and expiry is checked on every poll, so a paused run can never hold the browser forever. | Delegated | phase 5 review, finding 2 |
| 31 | 2026-09-16 | 5 | Operator notes are redacted before they reach the session file, and the session file is flagged sensitive because free text can carry a secret nobody declared. | Delegated | phase 5 review, finding 3 |
| 32 | 2026-09-16 | 5 | Losing control mid-run ends the run as `OPERATOR_ABORTED` rather than crashing. | Delegated | phase 5 review, finding 5 |
| 33 | 2026-09-16 | 5 | Resume still never repeats the step's action, even when the action never fired. The operator is told that in the request instead. | Delegated | phase 5 review, finding 6 |
| 28 | 2026-09-16 | 6 | `open_subaccount` was discovered by a real model run against the live app, not written by hand. A wrong first goal (an account type the app does not offer) made the model give up, which is itself the stuck path working. | Shrey, key supplied | `evidence/disc_20260916T153305Z_4dd9` |
| 35 | 2026-09-16 | 6 | `open_subaccount@1.1.0` adds ten detectors to the draft and is approved as Shrey Patel under delegation, same pattern as the read capability. | Delegated | `review_notes` in the artifact |
| 36 | 2026-09-16 | 6 | `od_session_expired` is scoped to s06 and s07, never to s11: recovering at the create step would re-submit an irreversible POST after a session bounce, which is a human's call. | Delegated | `review_notes` in the artifact |
| 37 | 2026-09-16 | 6 | Each rejected input gets its own VALIDATION_ERROR detector so the result can name the input the caller got wrong. | Delegated | `artifacts/coreledger.member.open_subaccount@1.1.0.capability.json` |
| 38 | 2026-09-16 | 6 | An error tool result carries text only, with the screen beside it in the same turn. The API rejects an image inside an error result, which killed a real discovery run with a 400. | Delegated | `src/cua/discover/loop.py` |
| 39 | 2026-09-16 | 6 | A derived success checkpoint never asserts an amount: the app reformats it, so the check fails a run that did exactly what it was asked and the caller retries into a second real record. | Delegated | phase 6 review, finding 1 |
| 40 | 2026-09-16 | 6 | `open_subaccount@2.0.0` after the phase 6 review: deposit condition removed, the notice detector renamed to the family's `INTERSTITIAL`, and a create-time expiry detector added. The rename makes it a major bump under the project's own versioning rule. | Delegated | `review_notes` in the artifact |
| 41 | 2026-09-16 | 6 | `policy_ref` is a bare policy id, not a file path: which file the policies live in is deployment configuration. | Delegated | REPORT.md, Artifact schema |
| 42 | 2026-09-16 | 6 | A member record is a sensitive page in its own right, so its screenshots are flagged whatever the running capability reads. Over-flagging in the safe direction: the lookup form matches too. | Delegated | `policy/allowlist.yaml` |
| 43 | 2026-09-16 | 6 | Run ids and evidence paths are never redacted: masking their digits made `result.json` disagree with the manifest beside it. | Delegated | `src/cua/evidence/writer.py` |
| 44 | 2026-09-16 | 6 | Every committed evidence run was regenerated after these changes, so no run predates the code or the policy that produced it. | Delegated | evidence/README.md |

## Still open

- The GitHub repo `cua-record-replay` does not exist yet. `gh repo create` needs a yes first.
- A fresh Anthropic API key for the phase 6 discovery run.
