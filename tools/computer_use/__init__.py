"""Compat shim package: ``tools.computer_use`` -> ``opencodon.tools.computer_use`` (restructure Phase 3a).

Per-module shim files alias each submodule; importing this package pulls in
the real package (preserving its import-time side effects). Deleted in
Phase 5.
"""

import opencodon.tools.computer_use as _real  # noqa: F401
