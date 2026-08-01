"""Tests for the on_session_switch hook and session_id propagation.

Covers #6672: memory providers must be notified when AIAgent.session_id
rotates mid-process (via /resume, /branch, /reset, /new, or context
compression). Without the notification, providers that cache per-session
state in initialize() (Hindsight, and any plugin that stores session_id
for scoped writes) keep writing into the old session's record.
"""


import pytest

from opencodon.core.memory_manager import MemoryManager
from opencodon.core.memory_provider import MemoryProvider


class _RecordingProvider(MemoryProvider):
    """Provider that records every lifecycle call for assertion."""

    def __init__(self, name="rec"):
        self._name = name
        self.switch_calls: list[dict] = []
        self.sync_calls: list[dict] = []
        self.queue_calls: list[dict] = []
        self.initialize_calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:  # pragma: no cover - unused
        return True

    def initialize(self, session_id, **kwargs):
        self.initialize_calls.append({"session_id": session_id, **kwargs})

    def get_tool_schemas(self):
        return []

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        self.sync_calls.append(
            {"user": user_content, "asst": assistant_content, "session_id": session_id}
        )

    def queue_prefetch(self, query, *, session_id=""):
        self.queue_calls.append({"query": query, "session_id": session_id})

    def on_session_switch(
        self,
        new_session_id,
        *,
        parent_session_id="",
        reset=False,
        **kwargs,
    ):
        self.switch_calls.append(
            {
                "new": new_session_id,
                "parent": parent_session_id,
                "reset": reset,
                "extra": kwargs,
            }
        )


# ---------------------------------------------------------------------------
# MemoryProvider ABC — default on_session_switch is a no-op
# ---------------------------------------------------------------------------


class _MinimalProvider(MemoryProvider):
    """Provider that does NOT override on_session_switch — ABC default must no-op."""

    @property
    def name(self) -> str:
        return "minimal"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id, **kwargs):  # pragma: no cover - unused
        pass

    def get_tool_schemas(self):
        return []


def test_abc_default_on_session_switch_is_noop():
    """Providers that don't override the hook must not raise."""
    p = _MinimalProvider()
    # All three call styles must be accepted without raising
    p.on_session_switch("new-id")
    p.on_session_switch("new-id", parent_session_id="old-id")
    p.on_session_switch("new-id", parent_session_id="old-id", reset=True)
    p.on_session_switch("new-id", parent_session_id="old-id", reset=True, reason="new_session")


# ---------------------------------------------------------------------------
# MemoryManager.on_session_switch — fan-out
# ---------------------------------------------------------------------------


def test_manager_fans_out_to_all_providers():
    mm = MemoryManager()
    # Only one external provider is allowed; use the builtin slot for p1.
    p1 = _RecordingProvider(name="builtin")
    p2 = _RecordingProvider(name="hindsight")
    mm.add_provider(p1)
    mm.add_provider(p2)

    mm.on_session_switch("new-sid", parent_session_id="old-sid", reset=False, reason="resume")

    assert len(p1.switch_calls) == 1
    assert len(p2.switch_calls) == 1
    for call in (p1.switch_calls[0], p2.switch_calls[0]):
        assert call["new"] == "new-sid"
        assert call["parent"] == "old-sid"
        assert call["reset"] is False
        assert call["extra"] == {"reason": "resume"}


def test_manager_ignores_empty_session_id():
    """Empty string session_id must not trigger provider hooks.

    Prevents accidental fires during shutdown when self.session_id may be
    cleared. Providers expect a meaningful id to switch TO.
    """
    mm = MemoryManager()
    p = _RecordingProvider()
    mm.add_provider(p)
    mm.on_session_switch("")
    mm.on_session_switch(None)  # type: ignore[arg-type]
    assert p.switch_calls == []


def test_manager_isolates_provider_failures():
    """A provider that raises must not block other providers."""

    class _Broken(_RecordingProvider):
        def on_session_switch(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("boom")

    mm = MemoryManager()
    # MemoryManager rejects a second external provider, so pair broken
    # (builtin slot) with a good external one.
    broken = _Broken(name="builtin")
    good = _RecordingProvider(name="good")
    mm.add_provider(broken)
    mm.add_provider(good)

    # Must not raise — exceptions in one provider are swallowed + logged
    mm.on_session_switch("new-sid", parent_session_id="old-sid")
    assert len(good.switch_calls) == 1
    assert good.switch_calls[0]["new"] == "new-sid"


def test_manager_reset_flag_preserved():
    mm = MemoryManager()
    p = _RecordingProvider()
    mm.add_provider(p)
    mm.on_session_switch("new-sid", reset=True, reason="new_session")
    assert p.switch_calls[0]["reset"] is True
    assert p.switch_calls[0]["extra"] == {"reason": "new_session"}


# ---------------------------------------------------------------------------
# MemoryManager.sync_all / queue_prefetch_all — session_id propagation
# ---------------------------------------------------------------------------


def test_sync_all_propagates_session_id_to_providers():
    """run_agent.py's sync_all call must pass session_id through to providers.

    Without this, a provider that updates _session_id defensively in
    sync_turn (as Hindsight does at hindsight/__init__.py:1199) never
    sees the new id and keeps writing under the old one.
    """
    mm = MemoryManager()
    p = _RecordingProvider()
    mm.add_provider(p)
    mm.sync_all("hello", "world", session_id="sess-42")
    mm.flush_pending(timeout=5)
    assert p.sync_calls == [
        {"user": "hello", "asst": "world", "session_id": "sess-42"}
    ]


def test_queue_prefetch_all_propagates_session_id_to_providers():
    mm = MemoryManager()
    p = _RecordingProvider()
    mm.add_provider(p)
    mm.queue_prefetch_all("next query", session_id="sess-42")
    mm.flush_pending(timeout=5)
    assert p.queue_calls == [{"query": "next query", "session_id": "sess-42"}]
