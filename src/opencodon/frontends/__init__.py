"""Delivery frontends: cli, gateway, tui, acp, mcp.

Each frontend is an independent entry surface over the same core; they must
not import each other (enforced by .importlinter as contracts tighten).
Keep this file empty of imports.
"""
