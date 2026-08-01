"""Compat shim — real package: ``opencodon.providers`` (restructure Phase 3a).

The provider registry's API lives on the package itself, so this shim
aliases the whole package object. ``providers.base`` resolves through the
real package's __path__. Deleted in Phase 5.
"""

import sys

import opencodon.providers as _real

sys.modules[__name__] = _real
