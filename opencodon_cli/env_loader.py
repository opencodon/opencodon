"""Compatibility shim — the real module is ``opencodon.config.env_loader``.

Restructure Phase 2 (docs/plans/2026-08-01-repo-restructure-plan.md) moved
this module out of the CLI frontend package. The shim aliases the REAL
module object in ``sys.modules`` so both import paths see one module.
New code must import from ``opencodon.config.env_loader``. Deleted in Phase 5.
"""

import sys

import opencodon.config.env_loader as _real

sys.modules[__name__] = _real
