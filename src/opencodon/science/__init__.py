"""Science layer — reproducibility tables and accessors for opencodon.

This package owns the execution/provenance data model — the frame
architecture:

- ``opencodon.state.science_schema`` — DDL for the six science tables (state owns state.db DDL)
- ``science.store`` — typed accessors (``ScienceStore``) over those tables

The tables live inside state.db (via ``opencodon_state.SessionDB``) rather than
a sidecar database so that a tool-result append and its execution/provenance
rows can commit in a single transaction.
"""

from opencodon.science.store import ScienceStore

__all__ = ["ScienceStore"]
