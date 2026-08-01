"""Compatibility shim — the real module is ``opencodon.common.timeutils``.

Restructure Phase 1 (docs/plans/2026-08-01-repo-restructure-plan.md) moved
this module into the ``opencodon`` package. This shim keeps the old import
path working during the transition by aliasing the REAL module object in
``sys.modules`` — both paths see one module, so monkeypatching and module
state stay coherent. New code must import from ``opencodon.common.timeutils``.
Deleted in Phase 5.
"""

import sys

import opencodon.common.timeutils as _real

sys.modules[__name__] = _real
