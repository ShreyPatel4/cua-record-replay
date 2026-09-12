# cua-record-replay

Record-once, replay-many computer-use automation for legacy credit union back-office UIs.
An LLM discovers how to do a task once, the run becomes a typed capability artifact, and
deterministic replay executes it with no model in the loop.

Status: phase 0 (scaffold and mock target). The full demo path lands in later phases.

## Setup

```sh
uv sync
uv run playwright install chromium
cp .env.example .env   # then set CORELEDGER_OPERATOR_PASSWORD (8+ chars)
```

## Mock target

```sh
uv run cua mock serve              # CoreLedger on http://127.0.0.1:5050/
uv run cua mock smoke              # hit every route and failure injection
```

Failure injection is controlled at `http://127.0.0.1:5050/__control`.

## Gates

```sh
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
uvx pre-commit install             # ruff plus the secret leak scan on every commit
```
