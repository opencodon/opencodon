"""Compat shim — real module: ``opencodon.frontends.gateway.platforms.webhook`` (restructure Phase 3a).

Aliases the real module object in ``sys.modules`` so old and new import
paths share one module. Deleted in Phase 5.
"""

import sys

import opencodon.frontends.gateway.platforms.webhook as _real

sys.modules[__name__] = _real
