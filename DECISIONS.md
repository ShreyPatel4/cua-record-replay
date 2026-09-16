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
| 28 | 2026-09-15 | 6 | `open_subaccount` discovery needs a fresh API key. The phase 3 key expired on 2026-09-14. | Shrey, pending key | phase 6 |

## Still open

- The GitHub repo `cua-record-replay` does not exist yet. `gh repo create` needs a yes first.
- A fresh Anthropic API key for the phase 6 discovery run.
