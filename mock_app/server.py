"""Serving helpers: a foreground server for the CLI and a background thread server for tests.

The thread server binds an ephemeral port so parallel test runs never collide.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from flask import Flask
from werkzeug.serving import BaseWSGIServer, make_server

from mock_app.app import CoreLedgerState, create_app, state_of
from mock_app.settings import MockSettings


def serve_forever(settings: MockSettings, host: str = "127.0.0.1", port: int | None = None) -> None:
    app = create_app(settings)
    server = make_server(host, port if port is not None else settings.port, app, threaded=True)
    print(f"CoreLedger listening on http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@dataclass
class RunningMockApp:
    base_url: str
    app: Flask
    server: BaseWSGIServer

    @property
    def state(self) -> CoreLedgerState:
        return state_of(self.app)


@contextmanager
def run_in_thread(settings: MockSettings, host: str = "127.0.0.1") -> Iterator[RunningMockApp]:
    app = create_app(settings)
    server = make_server(host, 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="coreledger", daemon=True)
    thread.start()
    try:
        yield RunningMockApp(f"http://{host}:{server.server_port}", app, server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
