"""Compat shim — real module: ``opencodon.core.learning_mutations`` (restructure Phase 3a).

Aliases the real module object in ``sys.modules`` so old and new import
paths share one module. Deleted in Phase 5.
"""

import sys

import opencodon.core.learning_mutations as _real

sys.modules[__name__] = _real
