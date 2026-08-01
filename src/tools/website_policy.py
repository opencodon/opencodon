"""Compat shim — real module: ``opencodon.tools.website_policy`` (restructure Phase 3a).

Aliases the real module object in ``sys.modules`` so old and new import
paths share one module. Deleted in Phase 5.
"""

import sys

import opencodon.tools.website_policy as _real

sys.modules[__name__] = _real
