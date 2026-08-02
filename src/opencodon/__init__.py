"""opencodon — the open-science AI agent.

Canonical package namespace. Keep this file EMPTY of imports: everything
under ``opencodon`` must be importable without side effects, and eager
imports here would tax every entry point's startup.
"""

# Legacy import-name compatibility (agent.*, opencodon_cli.*, cli, ...):
# one meta-path finder instead of per-file shim trees. Cheap (a dict probe
# per failed import) and side-effect-free beyond sys.meta_path.
from opencodon._legacy_aliases import install as _install_legacy_aliases

_install_legacy_aliases()
del _install_legacy_aliases
