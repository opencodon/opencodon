"""Compat shim — real module: ``opencodon.frontends.cli.shell`` (Phase 3a)."""

import opencodon_bootstrap  # noqa: F401  (must run before any I/O)
import sys

import opencodon.frontends.cli.shell as _real

sys.modules[__name__] = _real
