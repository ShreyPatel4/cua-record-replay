"""CoreLedger: a deliberately hostile legacy credit union core, used as the automation target.

It is a separate top-level package on purpose: the automation under src/cua never imports it.
"""

from mock_app.app import create_app
from mock_app.settings import MockSettings

__all__ = ["MockSettings", "create_app"]
