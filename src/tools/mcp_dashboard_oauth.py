"""Compat shim — real module: ``opencodon.tools.mcp_dashboard_oauth`` (restructure Phase 3a).

Aliases the real module object in ``sys.modules`` so old and new import
paths share one module. Deleted in Phase 5.
"""

import sys

import opencodon.tools.mcp_dashboard_oauth as _real

sys.modules[__name__] = _real
