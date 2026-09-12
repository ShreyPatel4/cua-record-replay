"""Shared fixtures: live CoreLedger on an ephemeral port, fault injection, evidence dirs, Chromium.

Tests get a random operator password per session, so they never touch the developer's .env secret.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from mock_app.injections import DEFAULT, Default
from mock_app.server import RunningMockApp, run_in_thread
from mock_app.settings import MockSettings

from support import Injector


@pytest.fixture(scope="session")
def mock_settings() -> MockSettings:
    return MockSettings(operator_user="teller-0417", operator_password=secrets.token_urlsafe(18))


@pytest.fixture(scope="session")
def _live_server(mock_settings: MockSettings) -> Iterator[RunningMockApp]:
    """Private: tests must go through `coreledger` so state is always reset around them."""
    with run_in_thread(mock_settings) as running:
        yield running


@pytest.fixture
def coreledger(_live_server: RunningMockApp) -> Iterator[RunningMockApp]:
    """The shared server with injections and sub-accounts reset around each test."""
    _live_server.state.faults.reset()
    _live_server.state.ledger.reset()
    yield _live_server
    _live_server.state.faults.reset()
    _live_server.state.ledger.reset()


@pytest.fixture
def inject(coreledger: RunningMockApp) -> Injector:
    def _inject(name: str, times: int | Default | None = DEFAULT, **params: str) -> None:
        coreledger.state.faults.arm(name, times=times, params=params)

    return _inject


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    path = tmp_path / "evidence"
    path.mkdir()
    return path


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    yield context.new_page()
    context.close()
