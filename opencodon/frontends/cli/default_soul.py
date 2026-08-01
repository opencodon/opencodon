"""Compatibility shim — the real module is ``opencodon.config.default_soul``.

Restructure Phase 2 (docs/plans/2026-08-01-repo-restructure-plan.md) moved
this module out of the CLI frontend package. The shim aliases the REAL
module object in ``sys.modules`` so both import paths see one module.
New code must import from ``opencodon.config.default_soul``. Deleted in Phase 5.
"""

import sys

import opencodon.config.default_soul as _real

sys.modules[__name__] = _real
