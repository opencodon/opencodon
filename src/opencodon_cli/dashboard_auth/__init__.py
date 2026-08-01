"""Compat shim package: ``opencodon_cli.dashboard_auth`` -> ``opencodon.frontends.cli.dashboard_auth`` (restructure Phase 3a).

Per-module shim files alias each submodule; importing this package pulls in
the real package (preserving import-time side effects and __init__ API,
re-exported below). Deleted in Phase 5.
"""

from opencodon.frontends.cli.dashboard_auth import *  # noqa: F401,F403
import opencodon.frontends.cli.dashboard_auth as _real  # noqa: F401

def __getattr__(name):
    return getattr(_real, name)
