"""Compat shim package: ``opencodon_cli.proxy.adapters`` -> ``opencodon.frontends.cli.proxy.adapters`` (restructure Phase 3a).

Per-module shim files alias each submodule; importing this package pulls in
the real package (preserving import-time side effects and __init__ API,
re-exported below). Deleted in Phase 5.
"""

from opencodon.frontends.cli.proxy.adapters import *  # noqa: F401,F403
import opencodon.frontends.cli.proxy.adapters as _real  # noqa: F401

def __getattr__(name):
    return getattr(_real, name)
