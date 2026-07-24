"""Resolve OPENCODON_HOME for standalone skill scripts.

Skill scripts may run outside the Hermes process (e.g. system Python,
nix env, CI) where ``opencodon_constants`` is not importable.  This module
provides the same ``get_opencodon_home()`` and ``display_opencodon_home()``
contracts as ``opencodon_constants`` without requiring it on ``sys.path``.

When ``opencodon_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``opencodon_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``OPENCODON_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from opencodon_constants import display_opencodon_home as display_opencodon_home
    from opencodon_constants import get_opencodon_home as get_opencodon_home
except (ModuleNotFoundError, ImportError):

    def get_opencodon_home() -> Path:
        """Return the Hermes home directory (default: ~/.opencodon).

        Mirrors ``opencodon_constants.get_opencodon_home()``."""
        val = os.environ.get("OPENCODON_HOME", "").strip()
        return Path(val) if val else Path.home() / ".opencodon"

    def display_opencodon_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``opencodon_constants.display_opencodon_home()``."""
        home = get_opencodon_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
