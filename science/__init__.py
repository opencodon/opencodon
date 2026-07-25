"""Science layer — reproducibility tables and accessors for opencodon.

This package owns the execution/provenance data model described in
``implementation-design.md`` (the frame architecture ported onto opencodon):

- ``science.schema`` — DDL for the six science tables that live in state.db
- ``science.store`` — typed accessors (``ScienceStore``) over those tables

The tables live inside state.db (via ``opencodon_state.SessionDB``) rather than
a sidecar database so that a tool-result append and its execution/provenance
rows can commit in a single transaction.
"""

from science.store import ScienceStore

__all__ = ["ScienceStore"]
