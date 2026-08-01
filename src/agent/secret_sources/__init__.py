"""Compat shim package: ``agent.secret_sources`` -> ``opencodon.core.secret_sources`` (restructure Phase 3a).

Per-module shim files alias each submodule; importing this package pulls in
the real package (preserving its import-time side effects). Deleted in
Phase 5.
"""

import opencodon.core.secret_sources as _real  # noqa: F401
