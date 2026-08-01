"""Compat shim — real module: ``opencodon.tools.osv_check`` (restructure Phase 3a).

Aliases the real module object in ``sys.modules`` so old and new import
paths share one module. Deleted in Phase 5.
"""

import sys

import opencodon.tools.osv_check as _real

sys.modules[__name__] = _real
