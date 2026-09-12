"""Runtime settings for the mock app, read from the environment (and .env when present).

Credentials are never defaulted: the app refuses to start without them so nothing is hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_PORT = 5050
DEFAULT_SESSION_TTL_S = 900


class MissingSettingError(RuntimeError):
    """Raised when a required environment variable is absent or blank."""


@dataclass(frozen=True)
class MockSettings:
    operator_user: str
    operator_password: str
    session_ttl_s: int = DEFAULT_SESSION_TTL_S
    port: int = DEFAULT_PORT
    # Query-param injection (?inject=name) is a test convenience; a deployment would disable it.
    allow_query_injection: bool = True

    def __repr__(self) -> str:
        return (
            f"MockSettings(operator_user={self.operator_user!r}, operator_password='[REDACTED]', "
            f"session_ttl_s={self.session_ttl_s}, port={self.port})"
        )

    @classmethod
    def from_env(cls, *, load_dotenv_file: bool = True) -> MockSettings:
        if load_dotenv_file:
            load_dotenv(override=False)
        user = os.environ.get("CORELEDGER_OPERATOR_USER", "").strip()
        password = os.environ.get("CORELEDGER_OPERATOR_PASSWORD", "")
        missing = [
            name
            for name, value in (
                ("CORELEDGER_OPERATOR_USER", user),
                ("CORELEDGER_OPERATOR_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise MissingSettingError(
                f"{', '.join(missing)} not set. Copy .env.example to .env and choose a value."
            )
        return cls(
            operator_user=user,
            operator_password=password,
            session_ttl_s=int(os.environ.get("CORELEDGER_SESSION_TTL_S", DEFAULT_SESSION_TTL_S)),
            port=int(os.environ.get("CORELEDGER_PORT", DEFAULT_PORT)),
            allow_query_injection=os.environ.get("CORELEDGER_ALLOW_QUERY_INJECTION", "1") == "1",
        )
