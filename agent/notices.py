"""Structured out-of-band agent notices — the driver-agnostic notice spine.

The agent fires an :class:`AgentNotice` via ``AIAgent.notice_callback`` and
clears it via ``notice_clear_callback``; each driver renders it its own way:

* the Ink TUI as a status-bar override (``tui_gateway/server.py``),
* the CLI REPL as a console line (``opencodon_cli/cli_agent_setup_mixin.py``),
* messaging platforms as a one-shot plaintext push
  (``gateway/run.py::render_notice_line``),
* the desktop app as a toast (``apps/desktop/src/store/agent-notices.ts``).

Consumers duck-type on ``.text`` and ``.key``, so a producer may pass any
object with those attributes; this dataclass is the canonical shape.

``kind``/``ttl_ms`` are kept fully expressive so a future config or slash
command can switch a notice from sticky to TTL without touching producers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentNotice:
    """A structured, driver-agnostic out-of-band notice."""

    text: str
    level: str = "info"            # info | warn | error | success
    kind: str = "sticky"           # sticky | ttl
    ttl_ms: Optional[int] = None   # honored only when kind == "ttl"
    key: Optional[str] = None      # dedupe / fired-once-latch / clear key
    id: Optional[str] = None


__all__ = ["AgentNotice"]
