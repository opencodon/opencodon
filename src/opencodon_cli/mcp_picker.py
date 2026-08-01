"""Compat shim — real module: ``opencodon.frontends.cli.mcp_picker`` (restructure Phase 3a).

Aliases the real module object in ``sys.modules`` so old and new import
paths share one module. Deleted in Phase 5.
"""

import sys

import opencodon.frontends.cli.mcp_picker as _real

sys.modules[__name__] = _real
