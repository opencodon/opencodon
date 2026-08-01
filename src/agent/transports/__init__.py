"""Compat shim package: ``agent.transports`` -> ``opencodon.core.transports`` (restructure Phase 3a).

Per-module shim files alias each submodule; importing this package pulls in
the real package (preserving its import-time side effects). Deleted in
Phase 5.
"""

import opencodon.core.transports as _real  # noqa: F401
