"""Compat shim — real module: ``opencodon.core.run_agent`` (restructure Phase 3a).

Aliases the real module object in ``sys.modules`` so old and new import
paths share one module. Deleted in Phase 5.
"""

import opencodon_bootstrap  # noqa: F401  (must run before any I/O — see tests/test_opencodon_bootstrap.py)
import sys

import opencodon.core.run_agent as _real

sys.modules[__name__] = _real
