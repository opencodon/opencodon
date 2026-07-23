"""Tests for auth-aware retry in Mattermost WS and Matrix sync loops.

Both Mattermost's _ws_loop and Matrix's _sync_loop previously caught all
exceptions with a broad ``except Exception`` and retried forever. Permanent
auth failures (401, 403, M_UNKNOWN_TOKEN) would loop indefinitely instead
of stopping. These tests verify that auth errors now stop the reconnect.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch



# ---------------------------------------------------------------------------
# Mattermost: _ws_loop auth-aware retry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Matrix: _sync_loop auth-aware retry
# ---------------------------------------------------------------------------
